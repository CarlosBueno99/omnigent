"""Real Codex binds a kernel-assigned loopback port and startup adopts it.

The installed ``codex`` CLI starts without a model request or credentials in an
isolated environment, so this checks the real bind banner Codex prints, that the
endpoint it reports accepts an initialized client, and that cancelling startup
reaps the child it owns.

Usage::

    python -m pytest tests/e2e/test_codex_native_app_server_port_assignment_e2e.py -v
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import psutil
import pytest

from omnigent.harnesses.codex_native.app_server import (
    CodexAppServerClient,
    CodexNativeAppServer,
)
from tests.e2e._codex_loopback_resume import clean_codex_env

pytestmark = pytest.mark.skipif(
    shutil.which("codex") is None, reason="requires the real Codex CLI"
)

_OFFLINE_OVERRIDES = [
    "analytics.enabled=false",
    "feedback.enabled=false",
    "features.plugins=false",
    'otel.metrics_exporter="none"',
    "check_for_update_on_startup=false",
]


@pytest.mark.parametrize("cancel_startup", [False, True], ids=["ready", "cancelled"])
async def test_startup_adopts_the_port_codex_binds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel_startup: bool
) -> None:
    """Codex reports its assigned endpoint; clients use it and cancellation reaps the child."""
    source_home = tmp_path / "source-codex-home"
    source_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(source_home))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    child_home = tmp_path / "home"
    child_home.mkdir()
    server = CodexNativeAppServer(
        codex_path=shutil.which("codex") or "codex",
        socket_path=tmp_path / "unused.sock",
        codex_home=tmp_path / "codex-home",
        env=clean_codex_env(child_home),
        config_overrides=list(_OFFLINE_OVERRIDES),
        cwd=workspace,
        bridge_dir=tmp_path / "bridge",
        listen_url="ws://127.0.0.1:0",
        reconcile_process_registry=False,
        session_id="codex-port-assignment-e2e",
    )

    startup = asyncio.create_task(server.start())
    try:
        while server.proc is None and not startup.done():
            await asyncio.sleep(0.05)
        child = server.proc
        assert child is not None, startup.result()
        if cancel_startup:
            startup.cancel()
            with pytest.raises(asyncio.CancelledError):
                await startup
        else:
            await asyncio.wait_for(startup, 60)
            listen_url = server.listen_url
            assert listen_url is not None and listen_url != "ws://127.0.0.1:0"
            assert f"  listening on: {listen_url}" in (server.recent_stderr or [])
            client = CodexAppServerClient(ws_url=listen_url, client_name="port-assignment-e2e")
            await client.connect()
            try:
                assert "result" in await client.request("config/read", {})
            finally:
                await client.close()
            adopted_port = int(listen_url.rsplit(":", 1)[1])
            await server.close()
            with pytest.raises(OSError):
                await asyncio.open_connection("127.0.0.1", adopted_port)
    finally:
        if not startup.done():
            startup.cancel()
        await asyncio.gather(startup, return_exceptions=True)
        await server.close()

    assert child.returncode is not None
    assert not psutil.pid_exists(child.pid)
    assert server.proc is None and server.stderr_task is None
