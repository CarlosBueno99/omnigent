"""Host-side bookkeeping for settings synced through the server."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from omnigent.host.config_sync import (
    ConfigSyncState,
    apply_config_update,
    build_sync_frame,
    load_sync_state,
    save_sync_state,
    sync_state_path,
)
from omnigent.host.frames import HostConfigSyncFrame, HostConfigUpdateFrame

_SERVER = "https://omnigent.example.test"
_LOCAL = {
    "providers": {"work": {"kind": "key", "openai": {"api_key": "SECRET"}}},
    "host": {"host_id": "abc", "name": "machine-a"},
    "model": "mock-model-from-machine-a",
    "harness": {"codex-native": {"command": "/opt/codex", "args": ["--profile", "machine-a"]}},
}
_PORTABLE = {
    "model": "mock-model-from-machine-a",
    "harness": {"codex-native": {"args": ["--profile", "machine-a"]}},
}


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    return tmp_path / ".omnigent" / "config.yaml"


def _write(config_path: Path, cfg: dict[str, object]) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.safe_dump(cfg))


def test_build_sync_frame_without_any_local_state(config_path: Path) -> None:
    assert build_sync_frame(_SERVER, config_path) == HostConfigSyncFrame(0, {}, False)


def test_build_sync_frame_counts_never_synced_settings_as_a_change(config_path: Path) -> None:
    _write(config_path, _LOCAL)
    assert build_sync_frame(_SERVER, config_path) == HostConfigSyncFrame(0, _PORTABLE, True)


def test_build_sync_frame_compares_against_the_last_sync(config_path: Path) -> None:
    _write(config_path, _LOCAL)
    save_sync_state(ConfigSyncState(_SERVER, 4, _PORTABLE), config_path)
    assert build_sync_frame(_SERVER, config_path) == HostConfigSyncFrame(4, _PORTABLE, False)

    _write(config_path, {**_LOCAL, "model": "edited-locally"})
    frame = build_sync_frame(_SERVER, config_path)
    assert frame == HostConfigSyncFrame(4, {**_PORTABLE, "model": "edited-locally"}, True)


def test_sync_state_for_another_server_or_corrupt_file_is_ignored(config_path: Path) -> None:
    _write(config_path, _LOCAL)
    save_sync_state(ConfigSyncState("https://other.example.test", 9, _PORTABLE), config_path)
    assert load_sync_state(_SERVER, config_path) is None
    assert build_sync_frame(_SERVER, config_path).revision == 0

    sync_state_path(config_path).write_text("{not json")
    assert load_sync_state(_SERVER, config_path) is None
    sync_state_path(config_path).write_text(
        json.dumps({"server_url": _SERVER, "revision": -1, "settings": {}})
    )
    assert load_sync_state(_SERVER, config_path) is None
    sync_state_path(config_path).write_text(
        json.dumps({"server_url": _SERVER, "revision": 1, "settings": {"providers": {}}})
    )
    assert load_sync_state(_SERVER, config_path) is None


def test_apply_config_update_writes_portable_keys_and_records_the_revision(
    config_path: Path,
) -> None:
    _write(config_path, {**_LOCAL, "auto_open_conversation": True})
    update = HostConfigUpdateFrame(
        7, {"model": "from-machine-b", "harness": {"codex-native": {"args": ["--profile", "b"]}}}
    )

    assert apply_config_update(update, _SERVER, config_path) is True

    assert yaml.safe_load(config_path.read_text()) == {
        "providers": _LOCAL["providers"],
        "host": _LOCAL["host"],
        "model": "from-machine-b",
        "harness": {"codex-native": {"command": "/opt/codex", "args": ["--profile", "b"]}},
    }
    assert load_sync_state(_SERVER, config_path) == ConfigSyncState(_SERVER, 7, update.settings)
    assert build_sync_frame(_SERVER, config_path) == HostConfigSyncFrame(7, update.settings, False)
    # The same revision again changes nothing.
    assert apply_config_update(update, _SERVER, config_path) is False


def test_apply_config_update_creates_a_missing_config(config_path: Path) -> None:
    assert apply_config_update(
        HostConfigUpdateFrame(
            1,
            {"model": "m"},
        ),
        _SERVER,
        config_path,
    )
    assert yaml.safe_load(config_path.read_text()) == {"model": "m"}
    assert load_sync_state(_SERVER, config_path) == ConfigSyncState(_SERVER, 1, {"model": "m"})


def test_apply_config_update_rejects_non_portable_settings(config_path: Path) -> None:
    _write(config_path, _LOCAL)
    with pytest.raises(ValueError):
        apply_config_update(HostConfigUpdateFrame(2, {"providers": {}}), _SERVER, config_path)
    assert yaml.safe_load(config_path.read_text()) == _LOCAL
    assert not sync_state_path(config_path).exists()


def test_apply_config_update_keeps_an_unreported_local_edit(config_path: Path) -> None:
    _write(config_path, {**_LOCAL, "model": "edited-locally"})
    save_sync_state(ConfigSyncState(_SERVER, 4, _PORTABLE), config_path)
    edited = {**_PORTABLE, "model": "edited-locally"}

    # An acknowledgement of the earlier report (or a peer's update) must not erase the edit.
    assert apply_config_update(HostConfigUpdateFrame(5, _PORTABLE), _SERVER, config_path) is False
    assert yaml.safe_load(config_path.read_text())["model"] == "edited-locally"
    assert load_sync_state(_SERVER, config_path) == ConfigSyncState(_SERVER, 5, _PORTABLE)
    assert build_sync_frame(_SERVER, config_path) == HostConfigSyncFrame(5, edited, True)

    # Once the server adopts the edit, the host is in sync again.
    assert apply_config_update(HostConfigUpdateFrame(6, edited), _SERVER, config_path) is False
    assert yaml.safe_load(config_path.read_text())["model"] == "edited-locally"
    assert build_sync_frame(_SERVER, config_path) == HostConfigSyncFrame(6, edited, False)


def test_apply_config_update_on_a_never_synced_host_adopts_the_servers_settings(
    config_path: Path,
) -> None:
    _write(config_path, {**_LOCAL, "model": "stale-local-default"})
    update = HostConfigUpdateFrame(3, _PORTABLE)

    assert apply_config_update(update, _SERVER, config_path) is True

    assert yaml.safe_load(config_path.read_text())["model"] == _PORTABLE["model"]
    assert build_sync_frame(_SERVER, config_path) == HostConfigSyncFrame(3, _PORTABLE, False)
