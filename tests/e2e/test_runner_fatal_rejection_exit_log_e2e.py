"""A never-connected runner rejected with HTTP 401 exits with a concise log.

A local responder answering every request with 401 stands in for a Databricks
edge rejecting the host's stale bearer; after three attempts the runner exits
on purpose. The host forwards the log tail into the session error, so the log
must end with the ``runner exiting: ...`` reason and carry no teardown
traceback. The runner is launched with the host's environment contract so the
exit path runs in a real process.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from omnigent.process_logging import PROCESS_LOG_FILE_ENV_VAR
from omnigent.runner.identity import (
    RUNNER_DELEGATED_AUTH_ENV_VAR,
    RUNNER_ID_ENV_VAR,
    RUNNER_INITIAL_AUTH_TOKEN_ENV_VAR,
    RUNNER_PARENT_PID_ENV_VAR,
    RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR,
    RUNNER_WORKSPACE_ENV_VAR,
    token_bound_runner_id,
)
from omnigent.runner.transports.ws_tunnel.serve import RUNNER_TUNNEL_REJECTION_PREFIX
from tests._helpers.compat import apply_runner_env, compat_runner_cwd, runner_executable
from tests._helpers.live_server import isolated_process_env

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BINDING_TOKEN = "e2e-rejected-runner-binding-token"
_RUNNER_EXIT_TIMEOUT_S = 120


class _RejectEverything(BaseHTTPRequestHandler):
    """Answer the tunnel upgrade and the delegated token mint alike with 401."""

    # The websockets client only accepts an HTTP/1.1 status line.
    protocol_version = "HTTP/1.1"

    def _reject(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        body = b"Unauthorized\n"
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    do_GET = _reject
    do_POST = _reject

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture()
def rejecting_front_door() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RejectEverything)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.min_runner_version("0.19.0")
def test_fatal_401_rejection_exits_with_reason_and_no_traceback(
    tmp_path: Path, rejecting_front_door: str
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    log_path = home / ".omnigent" / "logs" / "runner" / "runner-e2e.log"
    for directory in (log_path.parent, workspace):
        directory.mkdir(parents=True)

    env = isolated_process_env(
        home,
        {
            "PYTHONPATH": str(_REPO_ROOT),
            "RUNNER_SERVER_URL": rejecting_front_door,
            RUNNER_ID_ENV_VAR: token_bound_runner_id(_BINDING_TOKEN),
            RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR: _BINDING_TOKEN,
            RUNNER_DELEGATED_AUTH_ENV_VAR: "1",
            RUNNER_INITIAL_AUTH_TOKEN_ENV_VAR: "stale-host-bearer",
            RUNNER_WORKSPACE_ENV_VAR: str(workspace),
            RUNNER_PARENT_PID_ENV_VAR: str(os.getpid()),
            PROCESS_LOG_FILE_ENV_VAR: str(log_path),
        },
        None,
    )
    apply_runner_env(env)

    with (
        log_path.open("a") as log_handle,
        subprocess.Popen(
            [runner_executable(), "-P", "-m", "omnigent.runner._entry"],
            env=env,
            cwd=compat_runner_cwd() or str(workspace),
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=log_handle,
        ) as proc,
    ):
        try:
            exit_code = proc.wait(timeout=_RUNNER_EXIT_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
            pytest.fail(
                "runner did not exit after persistent 401 rejections:\n"
                + log_path.read_text(errors="replace")[-3000:]
            )

    log_text = log_path.read_text(errors="replace")
    rejection = f"{RUNNER_TUNNEL_REJECTION_PREFIX}(HTTP 401 persisted across 3 attempts)"
    assert exit_code == 1, log_text[-3000:]
    assert f"runner exiting: {rejection}" in log_text, log_text[-3000:]
    assert f"run `omnigent login {rejecting_front_door}` to re-authenticate" in log_text
    assert "Traceback (most recent call last)" not in log_text, log_text[-3000:]
    assert "CancelledError" not in log_text, log_text[-3000:]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
