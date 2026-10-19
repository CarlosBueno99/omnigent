"""Tests for the per-session environment a launching CLI hands its runner."""

from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path

import click
import pytest

from omnigent._platform import IS_WINDOWS
from omnigent.host.session_env import (
    SESSION_ENV_PASSTHROUGH_ENV_VAR,
    consume_session_env_handoff,
    requested_session_env,
    session_env_handoff,
)

_POSIX_ONLY = pytest.mark.skipif(IS_WINDOWS, reason="POSIX file ownership and modes")


@pytest.fixture(autouse=True)
def _data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data_dir = tmp_path / "data"
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(data_dir))
    return data_dir


def _handoff_files(data_dir: Path) -> list[Path]:
    directory = data_dir / "session-env"
    return sorted(directory.iterdir()) if directory.exists() else []


def _plant_handoff(data_dir: Path, session_id: str, payload: object, mode: int = 0o600) -> Path:
    with session_env_handoff(session_id, {"PLACEHOLDER": "x"}):
        (path,) = _handoff_files(data_dir)
    path.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(path, mode)
    return path


def test_requested_session_env_reads_listed_names_and_skips_unset_ones() -> None:
    source = {
        SESSION_ENV_PASSTHROUGH_ENV_VAR: (
            " ARCATERM_CAPABILITY , MACTERM_SOCKET,,ARCATERM_PROJECTS_ROOT"
        ),
        "ARCATERM_CAPABILITY": "capability-for-pane-1",
        "MACTERM_SOCKET": "",
        "UNLISTED": "nope",
    }

    assert requested_session_env(source) == {
        "ARCATERM_CAPABILITY": "capability-for-pane-1",
        "MACTERM_SOCKET": "",
    }


def test_requested_session_env_is_empty_without_a_list() -> None:
    assert requested_session_env({"ARCATERM_CAPABILITY": "capability-for-pane-1"}) == {}


@pytest.mark.parametrize(
    "name",
    ["OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN", "omnigent_data_dir", "RUNNER_SERVER_URL", "BAD-NAME"],
)
def test_requested_session_env_rejects_runner_wiring_and_malformed_names(name: str) -> None:
    source = {SESSION_ENV_PASSTHROUGH_ENV_VAR: f"ARCATERM_CAPABILITY,{name}", name: "value-secret"}

    with pytest.raises(click.ClickException) as excinfo:
        requested_session_env(source)

    assert name in excinfo.value.message
    assert "value-secret" not in excinfo.value.message


def test_daemon_consumes_the_handoff_once(_data_dir: Path) -> None:
    values = {"ARCATERM_CAPABILITY": "capability-for-pane-1"}

    with session_env_handoff("conv_a", values):
        assert consume_session_env_handoff("conv_b") == {}
        assert consume_session_env_handoff("conv_a") == values
        assert consume_session_env_handoff("conv_a") == {}
        assert _handoff_files(_data_dir) == []


@_POSIX_ONLY
def test_handoff_is_owner_only_and_removed_when_the_launch_fails(_data_dir: Path) -> None:
    with pytest.raises(RuntimeError):
        with session_env_handoff("conv_a", {"ARCATERM_CAPABILITY": "capability-for-pane-1"}):
            (path,) = _handoff_files(_data_dir)
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
            assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
            assert "conv_a" not in path.name
            raise RuntimeError("launch failed")

    assert _handoff_files(_data_dir) == []


def test_no_values_writes_no_handoff(_data_dir: Path) -> None:
    with session_env_handoff("conv_a", {}):
        assert not (_data_dir / "session-env").exists()


def test_handoff_replaced_by_a_concurrent_launch_is_left_for_it(_data_dir: Path) -> None:
    first = session_env_handoff("conv_a", {"ARCATERM_CAPABILITY": "first"})
    second = session_env_handoff("conv_a", {"ARCATERM_CAPABILITY": "second"})
    first.__enter__()
    second.__enter__()

    first.__exit__(None, None, None)

    assert consume_session_env_handoff("conv_a") == {"ARCATERM_CAPABILITY": "second"}
    second.__exit__(None, None, None)
    assert _handoff_files(_data_dir) == []


@_POSIX_ONLY
def test_consume_ignores_and_removes_a_handoff_other_users_can_read(
    _data_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _plant_handoff(_data_dir, "conv_a", {"ARCATERM_CAPABILITY": "leaked"}, mode=0o644)

    assert consume_session_env_handoff("conv_a") == {}
    assert _handoff_files(_data_dir) == []
    assert "readable by other users" in caplog.text
    assert "leaked" not in caplog.text


@_POSIX_ONLY
def test_consume_ignores_a_symlinked_handoff(_data_dir: Path, tmp_path: Path) -> None:
    path = _plant_handoff(_data_dir, "conv_a", {})
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps({"ARCATERM_CAPABILITY": "redirected"}), encoding="utf-8")
    os.chmod(target, 0o600)
    path.unlink()
    path.symlink_to(target)

    assert consume_session_env_handoff("conv_a") == {}
    assert _handoff_files(_data_dir) == []
    assert target.exists()


def test_consume_ignores_a_handoff_left_by_an_earlier_cli(_data_dir: Path) -> None:
    path = _plant_handoff(_data_dir, "conv_a", {"ARCATERM_CAPABILITY": "old-pane"})
    old = time.time() - 3600
    os.utime(path, (old, old))

    assert consume_session_env_handoff("conv_a") == {}
    assert _handoff_files(_data_dir) == []


def test_consume_keeps_only_forwardable_string_values(_data_dir: Path) -> None:
    _plant_handoff(
        _data_dir,
        "conv_a",
        {
            "ARCATERM_CAPABILITY": "capability-for-pane-1",
            "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": "forged",
            "RUNNER_SERVER_URL": "http://elsewhere",
            "MACTERM_SESSION": 7,
        },
    )

    assert consume_session_env_handoff("conv_a") == {
        "ARCATERM_CAPABILITY": "capability-for-pane-1"
    }


def test_consume_ignores_a_malformed_handoff(_data_dir: Path) -> None:
    path = _plant_handoff(_data_dir, "conv_a", {})
    path.write_text("{not json", encoding="utf-8")

    assert consume_session_env_handoff("conv_a") == {}
    assert _handoff_files(_data_dir) == []
