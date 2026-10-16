"""A foreground host's terminal hangup must let an in-flight turn finish.

``omnigent host`` runs in the foreground of a real terminal (a pty), a session
on that host has a turn waiting on the model, and the terminal closes. The
kernel delivers SIGHUP to the host, which dies by it. The reply should still
reach the session before the runner exits.
"""

from __future__ import annotations

import contextlib
import os
import re
import sys
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path

import httpx
import pexpect
import pytest
import yaml

from omnigent.process_logging import PROCESS_LOG_FILE_ENV_VAR
from tests._helpers.compat import apply_runner_env, compat_runner_cwd, runner_executable
from tests._helpers.live_server import isolated_local_server, isolated_process_env
from tests._helpers.session import bind_session_runner, bundle_files, post_session_bundle
from tests.e2e.conftest import (
    configure_mock_llm,
    poll_session_until_terminal,
    release_mock_gate,
    send_user_message_to_session,
)
from tests.e2e.helpers import final_assistant_text

_HOST_ONLINE_TIMEOUT_S = 120.0
_RUNNER_ONLINE_TIMEOUT_S = 90.0
_GATE_TIMEOUT_S = 60.0
_TURN_TIMEOUT_S = 120.0
_REPLY = "HANGUP_SURVIVOR_REPLY"
_AGENT_YAML = """\
spec_version: 1
name: hangup-turn-agent
prompt: You are a terse test assistant. Reply exactly as asked.
executor:
  model: {model}
  config:
    harness: openai-agents
"""


class _ForegroundHostTerminal:
    """``omnigent host`` as the session leader of a pty, drained like a terminal."""

    def __init__(self, root: Path, base_url: str, mock_url: str) -> None:
        home = root / "home"
        config = home / ".omnigent"
        config.mkdir(parents=True)
        self.host_id = uuid.uuid4().hex
        name = f"hangup-{self.host_id[:8]}"
        (config / "config.yaml").write_text(
            yaml.safe_dump({"host": {"host_id": self.host_id, "name": name}})
        )
        self.log_path = root / "host.log"
        env = isolated_process_env(
            home, {"OPENAI_BASE_URL": f"{mock_url}/v1", "OPENAI_API_KEY": "mock-key"}, None
        )
        env.update(
            OMNIGENT_CONFIG_HOME=str(config),
            OMNIGENT_HOST_ID=self.host_id,
            OMNIGENT_HOST_NAME=name,
            TERM="xterm-256color",
        )
        env[PROCESS_LOG_FILE_ENV_VAR] = str(self.log_path)
        self.child = pexpect.spawn(
            runner_executable(),
            ["-m", "omnigent", "host", "--server", base_url, "--non-interactive", "--no-open"],
            env=apply_runner_env(env),
            cwd=compat_runner_cwd() or str(root),
            encoding="utf-8",
            timeout=5,
            dimensions=(40, 160),
        )
        # Open the transcript only after a successful spawn so a spawn failure
        # cannot leak the file handle (close() never runs in that case).
        self.transcript = (root / "terminal.txt").open("w", encoding="utf-8")
        self.child.logfile_read = self.transcript
        self._stop = threading.Event()
        self._drain = threading.Thread(target=self._pump, name="host-pty-drain", daemon=True)
        self._drain.start()

    def _pump(self) -> None:
        while not self._stop.is_set():
            try:
                self.child.read_nonblocking(4096, timeout=0.2)
            except pexpect.TIMEOUT:
                continue
            except (pexpect.EOF, OSError, ValueError):
                return

    def hang_up(self) -> None:
        """Close the terminal: the kernel sends SIGHUP to the host's foreground group."""
        self._stop.set()
        self._drain.join(timeout=5)
        os.close(self.child.child_fd)
        self.child.child_fd = -1

    def wait_exit(self, timeout: float) -> tuple[int | None, int | None]:
        deadline = time.monotonic() + timeout
        while self.child.isalive() and time.monotonic() < deadline:
            time.sleep(0.1)
        return self.child.exitstatus, self.child.signalstatus

    def close(self) -> None:
        self._stop.set()
        self._drain.join(timeout=5)
        if self.child.isalive():
            self.child.kill(9)
            self.wait_exit(timeout=5)
        if self.child.child_fd != -1:
            self.child.close(force=True)
        self.transcript.close()


def _pgid_or_none(pid: int | None) -> int | None:
    if not pid:
        return None
    try:
        return os.getpgid(pid)
    except ProcessLookupError:
        return None


def _pid_running(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state not in {"Z", "X"}


def _wait_until(predicate: Callable[[], object], timeout: float, interval: float = 0.25) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


@pytest.mark.skipif(sys.platform != "linux", reason="pty hangup semantics; /proc liveness")
@pytest.mark.timeout(420)
def test_terminal_hangup_lets_in_flight_turn_finish(
    tmp_path: Path, isolated_mock_llm_server_url: str
) -> None:
    mock_url = isolated_mock_llm_server_url
    model = f"hangup-turn-{uuid.uuid4().hex[:8]}"
    configure_mock_llm(mock_url, [{"text": _REPLY, "block": True}], key=model)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner_pid: int | None = None
    zygote_pid: int | None = None
    with (
        isolated_local_server(tmp_path / "server") as base_url,
        httpx.Client(base_url=base_url, trust_env=False, timeout=30) as client,
    ):
        terminal = _ForegroundHostTerminal(tmp_path / "host", base_url, mock_url)
        try:

            def _host_online() -> bool:
                rows = client.get("/v1/hosts").json().get("hosts", [])
                return any(
                    r.get("host_id") == terminal.host_id and r.get("status") == "online"
                    for r in rows
                )

            assert _wait_until(_host_online, _HOST_ONLINE_TIMEOUT_S), (
                f"host never came online\n{terminal.log_path.read_text(errors='replace')[-4000:]}"
            )
            bundle = bundle_files({"config.yaml": _AGENT_YAML.format(model=model).encode()})
            created = post_session_bundle(client.post, "/v1/sessions", bundle)
            created.raise_for_status()
            session_id = str(created.json()["session_id"])
            launch = client.post(
                f"/v1/hosts/{terminal.host_id}/runners",
                json={"session_id": session_id, "workspace": str(workspace)},
                timeout=90.0,
            )
            assert launch.status_code == 200, launch.text
            runner_id = launch.json()["runner_id"]
            assert _wait_until(
                lambda: client.get(f"/v1/runners/{runner_id}/status").json().get("online") is True,
                _RUNNER_ONLINE_TIMEOUT_S,
            ), "host-launched runner never came online"
            bind_session_runner(client.patch, base_url, session_id, runner_id)
            host_log = terminal.log_path.read_text(errors="replace")
            launched = re.search(r"Launched runner \S+ for workspace .*? \(pid=(\d+)\)", host_log)
            assert launched is not None, host_log[-4000:]
            runner_pid = int(launched.group(1))
            forked = re.search(r"zygote pid=(\d+), runner pid=", host_log)
            zygote_pid = int(forked.group(1)) if forked else None
            host_pgid = _pgid_or_none(terminal.child.pid)
            print(
                f"host pid={terminal.child.pid} pgid={host_pgid} zygote={zygote_pid} "
                f"(pgid={_pgid_or_none(zygote_pid)}) runner={runner_pid} "
                f"(pgid={_pgid_or_none(runner_pid)}) session={session_id} runner_id={runner_id}"
            )

            response_id = send_user_message_to_session(
                client, session_id=session_id, content=f"Reply with exactly {_REPLY}."
            )
            assert _wait_until(
                lambda: (
                    httpx.get(f"{mock_url}/gate/pending", timeout=5, trust_env=False)
                    .json()
                    .get("pending")
                ),
                _GATE_TIMEOUT_S,
            ), "the turn never reached the model"
            before = client.get(f"/v1/sessions/{session_id}").json()
            print(f"status before hangup: {before.get('status')}")

            hangup_at = time.monotonic()
            terminal.hang_up()
            host_exit = terminal.wait_exit(timeout=15)
            print(
                f"host exit (exitstatus, signalstatus)={host_exit} "
                f"after {time.monotonic() - hangup_at:.2f}s"
            )
            assert not terminal.child.isalive(), (
                f"host survived the terminal hangup (test precondition): {host_exit}"
            )
            time.sleep(2.0)
            runner_alive_at_release = _pid_running(runner_pid)
            release_mock_gate(mock_url)
            print(
                f"gate released {time.monotonic() - hangup_at:.2f}s after hangup; "
                f"runner alive={runner_alive_at_release} zygote alive="
                f"{_pid_running(zygote_pid) if zygote_pid else None}"
            )

            body = poll_session_until_terminal(
                client, session_id=session_id, response_id=response_id, timeout=_TURN_TIMEOUT_S
            )
            # Failure labels land asynchronously; re-read once the status settled.
            time.sleep(1.0)
            settled = client.get(f"/v1/sessions/{session_id}").json()
            error = settled.get("last_task_error") or body.get("error")
            print(
                f"turn terminal {time.monotonic() - hangup_at:.1f}s after hangup: "
                f"status={body['status']} last_task_error={error}"
            )
            assert body["status"] == "completed", (
                f"the in-flight turn did not finish after the terminal hangup: "
                f"status={body['status']} last_task_error={error} "
                f"(runner alive when the model replied: {runner_alive_at_release})"
            )
            assert _REPLY in final_assistant_text(body)
            assert _wait_until(lambda: not _pid_running(runner_pid), 30.0), (
                f"runner {runner_pid} kept running after its turn finished"
            )
        finally:
            for pid in (runner_pid, zygote_pid):
                if pid and _pid_running(pid):
                    with contextlib.suppress(ProcessLookupError, PermissionError):
                        os.kill(pid, 9)
            terminal.close()
