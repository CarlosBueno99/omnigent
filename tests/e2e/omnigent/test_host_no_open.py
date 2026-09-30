"""Exercise host browser preferences through the real CLI and a local server."""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

import pexpect
import pytest

from tests.e2e.omnigent.test_host_ctrl_c_stop_server import (
    _BOOT_TIMEOUT,
    _EXIT_TIMEOUT,
    _PROMPT_MARKER,
    _PROMPT_TIMEOUT,
    _boot_connect_and_get_server,
    _connect_env,
    _force_stop_server,
    _read_local_server_record,
)


@pytest.mark.parametrize(
    ("extra_args", "extra_env", "expect_open"),
    [
        pytest.param([], {}, "http://127.0.0.1:{port}", id="default-opens"),
        pytest.param(["--no-open"], {}, None, id="flag-suppresses"),
        pytest.param([], {"OMNIGENT_HOST_NO_OPEN": "1"}, None, id="env-suppresses"),
        pytest.param(["--open-in-desktop"], {}, "omnigent://127.0.0.1:{port}/", id="flag-desktop"),
        pytest.param(
            [],
            {"OMNIGENT_HOST_OPEN_IN_DESKTOP": "1"},
            "omnigent://127.0.0.1:{port}/",
            id="env-desktop",
        ),
    ],
)
def test_host_browser_preference(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    tmp_path: Path,
    extra_args: list[str],
    extra_env: dict[str, str],
    expect_open: str | None,
) -> None:
    """A host comes online with no tab, a browser tab, or a desktop deep link."""
    home = tmp_path / "home"
    env = _connect_env(mock_credentials_env, home)
    env["OMNIGENT_CONFIG_HOME"] = str(tmp_path / "config")
    env.pop("OMNIGENT_HOST_NO_OPEN", None)
    env.pop("OMNIGENT_HOST_OPEN_IN_DESKTOP", None)
    env.update(extra_env)

    # Record browser launches without opening a real browser on macOS or Linux.
    browser_log = tmp_path / "browser.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    opener = bin_dir / "open"
    opener.write_text('#!/bin/sh\nprintf "%s\\n" "$1" >> "$OMNIGENT_TEST_BROWSER_LOG"\n')
    opener.chmod(0o755)
    env["OMNIGENT_TEST_BROWSER_LOG"] = str(browser_log)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["BROWSER"] = f"{opener} %s"

    child = pexpect.spawn(
        str(omnigent_python),
        ["-m", "omnigent", "host", "", *extra_args],
        env=env,
        cwd=str(omnigent_repo_root),
        encoding="utf-8",
        timeout=_BOOT_TIMEOUT,
    )
    try:
        _, port = _boot_connect_and_get_server(child, home)
        opened = browser_log.read_text().splitlines() if browser_log.exists() else []
        assert opened == ([expect_open.format(port=port)] if expect_open else [])

        child.sendcontrol("c")
        child.expect_exact(_PROMPT_MARKER, timeout=_PROMPT_TIMEOUT)
        child.send("y\r")
        child.expect(pexpect.EOF, timeout=_EXIT_TIMEOUT)
        child.close()
        assert child.exitstatus == 0
    finally:
        if not child.closed:
            child.close(force=True)
        with contextlib.suppress(AssertionError, OSError, ValueError, IndexError):
            server_pid, _ = _read_local_server_record(home)
            _force_stop_server(server_pid)
