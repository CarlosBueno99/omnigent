"""Native Codex terminal startup while another process contends for its loopback port.

A codex wrapper on ``OMNIGENT_CODEX_PATH`` seizes any fixed port the runner passes."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Browser, Page, expect

from omnigent.harnesses.codex_native.bridge import bridge_dir_for_bridge_id
from omnigent.runner.identity import token_bound_runner_id
from tests._helpers.compat import apply_runner_env, compat_runner_cwd, runner_executable
from tests._helpers.native_session import create_native_session
from tests._helpers.session import bind_session_runner
from tests.e2e_ui.conftest import (
    _REPO_ROOT,
    _codex_cli_supports_mocked_app_server,
    _find_free_port,
)
from tests.e2e_ui.messages.test_native_codex_render_parity import (
    _open_terminal_view,
    _wait_terminal_connected,
)

_HEALTH_TIMEOUT_S = 60.0
_HEALTH_POLL_INTERVAL_S = 0.5
# Cold codex boot (version probe, catalog, TUI attach) or the app-server
# readiness loop giving up, whichever the runner reaches first.
_STARTUP_OUTCOME_TIMEOUT_S = 150.0
# Binding drives terminal auto-create synchronously; on the port-race failure
# path that out-waits the default 10s PATCH budget, so run the bind in the
# background and let the browser observe the result over its own stream.
_BIND_TIMEOUT_S = 180.0

_WRAPPER_SOURCE = r'''#!/usr/bin/env python3
"""Takes any fixed port the launcher released, then runs the real codex unchanged."""
import json, os, socket, sys, time

REAL_CODEX = {real!r}
JOURNAL = {journal!r}
SEIZE_FLAG = {flag!r}


def journal(event, **fields):
    record = {{"ts": time.time(), "event": event, **fields}}
    record["codex_home"] = os.environ.get("CODEX_HOME")
    with open(JOURNAL, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


def listen_port(argv):
    if argv[:1] != ["app-server"] or "--listen" not in argv:
        return None
    url = argv[argv.index("--listen") + 1]
    return int(url.rsplit(":", 1)[1]) if url.startswith("ws://127.0.0.1:") else None


argv = sys.argv[1:]
port = listen_port(argv)
if port is None or not os.path.exists(SEIZE_FLAG):
    journal("passthrough", argv=argv[:3])
elif port == 0:
    os.unlink(SEIZE_FLAG)
    journal("kernel_assigned", argv=argv)
else:
    os.unlink(SEIZE_FLAG)
    ready_r, ready_w = os.pipe()
    if os.fork() == 0:
        os.close(ready_r)
        os.setsid()
        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            holder.bind(("127.0.0.1", port))
            holder.listen(1)
            journal("seized", port=port, holder_pid=os.getpid(), argv=argv)
        except OSError as exc:
            journal("seize_failed", port=port, error=str(exc))
            os.write(ready_w, b"x")
            os._exit(1)
        os.write(ready_w, b"x")
        os.close(ready_w)
        while True:
            time.sleep(3600)
    os.close(ready_w)
    os.read(ready_r, 1)
    os.close(ready_r)
os.execv(REAL_CODEX, [REAL_CODEX, *argv])'''


@dataclass(frozen=True)
class _ContentionStack:
    base_url: str
    runner_id: str
    runner_log: Path
    journal: Path
    seize_flag: Path
    evidence_dir: Path

    def arm_competing_bind(self) -> None:
        """Make the next ``codex app-server --listen`` launch lose a fixed port."""
        self.seize_flag.touch()

    def launch_records(self) -> list[dict[str, object]]:
        if not self.journal.exists():
            return []
        return [json.loads(line) for line in self.journal.read_text().splitlines() if line.strip()]

    def armed_launches_for(self, session_id: str) -> list[dict[str, object]]:
        """Armed app-server launches of the session: port seized, or left to Codex."""
        # The app-server's CODEX_HOME lives under the session's bridge dir, whose
        # name is a digest of the session id rather than the raw id.
        digest = bridge_dir_for_bridge_id(session_id).name
        return [
            record
            for record in self.launch_records()
            if record["event"] in ("seized", "kernel_assigned")
            and digest in str(record.get("codex_home") or "")
        ]

    def runner_log_excerpt(self, session_id: str, *, context: int = 40) -> str:
        """Session-tagged runner log lines plus the traceback after a start failure."""
        lines = self.runner_log.read_text(errors="replace").splitlines()
        picked: list[str] = []
        tail_until = -1
        for index, line in enumerate(lines):
            if session_id in line:
                picked.append(line)
                if "Failed to auto-create" in line:
                    tail_until = index + context
            elif index <= tail_until:
                picked.append(line)
        return "\n".join(picked)


def _write_codex_wrapper(directory: Path, real_codex: str) -> tuple[Path, Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    wrapper = directory / "codex"
    journal = directory / "launches.jsonl"
    flag = directory / "seize-next-listen"
    wrapper.write_text(
        _WRAPPER_SOURCE.format(real=real_codex, journal=str(journal), flag=str(flag)),
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    return wrapper, journal, flag


def _write_mock_provider_config(config_home: Path, mock_url: str) -> None:
    config_home.mkdir(parents=True, exist_ok=True)
    (config_home / "config.yaml").write_text(
        f"""\
providers:
  codex-e2e-port-contention:
    kind: key
    default: openai
    openai:
      base_url: "{mock_url}/v1"
      api_key: "sk-e2e-mock"
      wire_api: responses
      models:
        default: gpt-4o
""",
        encoding="utf-8",
    )


@pytest.fixture
def codex_port_contention_stack(
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
    request: pytest.FixtureRequest,
) -> Iterator[_ContentionStack]:
    """A dedicated server + runner whose ``codex`` is the competing-bind wrapper.

    The runner resolves codex from its own environment at launch time, so the
    wrapper must be in place before the runner starts; the shared ``live_server``
    runner cannot be re-pointed.
    """
    if request.config.getoption("--ui-base-url"):
        pytest.skip("native Codex port-contention e2e requires an isolated spawned server")
    real_codex = shutil.which("codex")
    if real_codex is None:
        pytest.skip("codex CLI is required for native Codex e2e")
    if not _codex_cli_supports_mocked_app_server(real_codex):
        pytest.skip("codex CLI >= 0.139.0 is required for mocked app-server e2e")
    if shutil.which("tmux") is None:
        pytest.skip("tmux is required for native Codex terminals")

    server_tmp = tmp_path_factory.mktemp("e2e_ui_codex_port_contention")
    config_home = server_tmp / "config-home"
    source_codex_home = server_tmp / "source-codex-home"
    home_dir = server_tmp / "home"
    state_dir = server_tmp / "codex-native-state"
    artifact_dir = server_tmp / "artifacts"
    evidence_dir = server_tmp / "evidence"
    for path in (source_codex_home, home_dir, state_dir, artifact_dir, evidence_dir):
        path.mkdir(parents=True, exist_ok=True)
    # tmux.sock must fit the unix socket path limit, which the basetemp tree exceeds.
    tmp_dir = Path(tempfile.mkdtemp(prefix="codexport-"))
    wrapper, journal, flag = _write_codex_wrapper(server_tmp / "codex-wrapper", real_codex)
    _write_mock_provider_config(config_home, mock_llm_server_url)

    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    binding_token = secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(binding_token)
    runner_log = server_tmp / "runner-process.log"
    shared_env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("OPENAI_", "DATABRICKS_"))
    }
    shared_env.update(
        {
            "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
            "OMNIGENT_CONFIG_HOME": str(config_home),
            "OMNIGENT_CODEX_NATIVE_STATE_DIR": str(state_dir),
            "CODEX_HOME": str(source_codex_home),
            "HOME": str(home_dir),
            "OMNIGENT_CODEX_PATH": str(wrapper),
            "TMPDIR": str(tmp_dir),
        }
    )
    server_env = {**shared_env, "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token}
    runner_env = {
        **shared_env,
        "OMNIGENT_RUNNER_ID": runner_id,
        "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
        "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
        "RUNNER_SERVER_URL": base_url,
        "OMNIGENT_PROCESS_LOG_FILE": str(runner_log),
    }
    server_command = [
        sys.executable,
        "-c",
        "from omnigent.cli import main; main()",
        "server",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--database-uri",
        f"sqlite:///{server_tmp / 'test.db'}",
        "--artifact-location",
        str(artifact_dir),
    ]
    server_log_handle = open(server_tmp / "server.log", "w")  # noqa: SIM115
    runner_stdout_handle = open(server_tmp / "runner-stdout.log", "w")  # noqa: SIM115
    proc: subprocess.Popen[bytes] | None = None
    runner_proc: subprocess.Popen[bytes] | None = None
    try:
        proc = subprocess.Popen(
            server_command, env=server_env, stdout=server_log_handle, stderr=subprocess.STDOUT
        )
        runner_proc = subprocess.Popen(
            [runner_executable(), "-m", "omnigent.runner._entry"],
            env=apply_runner_env(runner_env),
            cwd=compat_runner_cwd(),
            stdout=runner_stdout_handle,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + _HEALTH_TIMEOUT_S
        last_error = "not polled yet"
        while True:
            if time.monotonic() > deadline:
                raise RuntimeError(
                    f"server/runner not healthy within {_HEALTH_TIMEOUT_S:.0f}s "
                    f"(last_error={last_error}); logs under {server_tmp}"
                )
            if proc.poll() is not None:
                last_error = f"server exited with {proc.returncode}"
            elif runner_proc.poll() is not None:
                last_error = f"runner exited with {runner_proc.returncode}"
            else:
                try:
                    if httpx.get(f"{base_url}/health", timeout=2).status_code == 200:
                        status = httpx.get(f"{base_url}/v1/runners/{runner_id}/status", timeout=2)
                        if status.status_code == 200 and status.json()["online"] is True:
                            break
                        last_error = f"runner status {status.status_code}: {status.text[:200]}"
                except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException) as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(_HEALTH_POLL_INTERVAL_S)
        yield _ContentionStack(
            base_url=base_url,
            runner_id=runner_id,
            runner_log=runner_log,
            journal=journal,
            seize_flag=flag,
            evidence_dir=evidence_dir,
        )
    finally:
        for child in (runner_proc, proc):
            if child is not None and child.poll() is None:
                child.send_signal(signal.SIGTERM)
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
        if journal.exists():
            for line in journal.read_text().splitlines():
                record = json.loads(line) if line.strip() else {}
                if record.get("event") == "seized":
                    with suppress(ProcessLookupError, PermissionError):
                        os.kill(int(record["holder_pid"]), signal.SIGKILL)
        server_log_handle.close()
        runner_stdout_handle.close()
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _terminal_resource_present(base_url: str, session_id: str) -> bool:
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}/resources", timeout=10.0)
    if resp.status_code != 200:
        return False
    return any(item.get("type") == "terminal" for item in (resp.json().get("data") or []))


def _wait_startup_outcome(
    page: Page, bind_thread: threading.Thread, base_url: str, session_id: str
) -> str:
    """Classify startup once terminal auto-create settles.

    The toggle is enabled before the app-server is even launched, so a reliable
    outcome needs the auto-create to finish: bind returning marks that point.
    A failure surfaces as the browser's error pill; success as a live terminal
    resource. An early error pill short-circuits the wait.
    """
    error_pill = page.get_by_test_id("error-pill")
    deadline = time.monotonic() + _STARTUP_OUTCOME_TIMEOUT_S
    while bind_thread.is_alive() and time.monotonic() < deadline:
        if error_pill.count() and error_pill.first.is_visible():
            return "error"
        page.wait_for_timeout(500)
    settle_deadline = time.monotonic() + 20.0
    while time.monotonic() < settle_deadline:
        if error_pill.count() and error_pill.first.is_visible():
            return "error"
        if _terminal_resource_present(base_url, session_id):
            return "terminal"
        page.wait_for_timeout(500)
    return "timeout"


def _session_snapshot(base_url: str, session_id: str) -> dict[str, object]:
    session = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    resources = httpx.get(f"{base_url}/v1/sessions/{session_id}/resources", timeout=10.0)
    return {
        "session_status": session.json().get("status")
        if session.status_code == 200
        else session.status_code,
        "resource_types": [item.get("type") for item in (resources.json().get("data") or [])]
        if resources.status_code == 200
        else resources.status_code,
    }


def _start_codex_session(stack: _ContentionStack, page: Page) -> tuple[str, str]:
    """Open an unbound Codex session in the browser, then bind it off-thread.

    Binding kicks off the runner's terminal auto-create. On the port-race
    failure path that PATCH blocks until the app-server readiness loop gives
    up, so it runs on a background thread while the page observes the terminal
    coming up or the failure pill over its own session stream.
    """
    created = create_native_session(
        httpx,
        stack.base_url,
        harness="codex",
        metadata={"workspace": str(_REPO_ROOT)},
    )
    session_id = str(created["session_id"])
    page.goto(f"{stack.base_url}/c/{session_id}")

    bind_errors: list[BaseException] = []

    def _bind() -> None:
        try:
            bind_session_runner(
                httpx.patch, stack.base_url, session_id, stack.runner_id, timeout=_BIND_TIMEOUT_S
            )
        except BaseException as exc:
            bind_errors.append(exc)

    bind_thread = threading.Thread(target=_bind, name="codex-bind", daemon=True)
    bind_thread.start()
    outcome = _wait_startup_outcome(page, bind_thread, stack.base_url, session_id)
    if outcome == "timeout":
        # A late subscription can miss the live session.status event; the failed
        # status is persisted, so a reload re-renders it.
        page.reload()
        outcome = _wait_startup_outcome(page, bind_thread, stack.base_url, session_id)
    bind_thread.join(timeout=5)
    return session_id, outcome


@pytest.mark.timeout(600)
def test_native_codex_terminal_starts_when_probed_port_is_taken(
    request: pytest.FixtureRequest,
    browser: Browser,
    codex_port_contention_stack: _ContentionStack,
) -> None:
    """A rival for the app-server's loopback port must not leave the session without a terminal."""
    stack = codex_port_contention_stack

    # Control: the same stack starts a terminal when nobody contends for the port.
    control_context = browser.new_context()
    try:
        control_page = control_context.new_page()
        control_id, control_outcome = _start_codex_session(stack, control_page)
        assert control_outcome == "terminal", (
            f"control session {control_id} did not start a terminal ({control_outcome}); "
            f"runner log:\n{stack.runner_log_excerpt(control_id)}"
        )
        _open_terminal_view(control_page)
        _wait_terminal_connected(control_page)
        control_page.screenshot(path=stack.evidence_dir / f"{control_id}-control-connected.png")
    finally:
        control_context.close()

    stack.arm_competing_bind()
    page: Page = request.getfixturevalue("page")
    session_id, outcome = _start_codex_session(stack, page)
    armed = stack.armed_launches_for(session_id)
    diagnostics = {
        "session_id": session_id,
        "outcome": outcome,
        "armed_launches": armed,
        **_session_snapshot(stack.base_url, session_id),
        "runner_log_excerpt": stack.runner_log_excerpt(session_id),
    }
    (stack.evidence_dir / f"{session_id}-diagnostics.json").write_text(
        json.dumps(diagnostics, indent=1), encoding="utf-8"
    )
    page.screenshot(path=stack.evidence_dir / f"{session_id}-startup-outcome.png")
    if not armed:
        pytest.fail(
            f"the armed launch was never observed, so this run is inconclusive: {diagnostics}"
        )
    if outcome == "error":
        page.get_by_test_id("error-pill").first.locator("button[aria-expanded]").first.click()
        expect(page.get_by_test_id("error-message-content").first).to_be_visible(timeout=10_000)
        page.wait_for_timeout(3_000)
        page.screenshot(path=stack.evidence_dir / f"{session_id}-error-expanded.png")
        diagnostics["error_message"] = page.get_by_test_id(
            "error-message-content"
        ).first.inner_text()
        (stack.evidence_dir / f"{session_id}-diagnostics.json").write_text(
            json.dumps(diagnostics, indent=1), encoding="utf-8"
        )
    assert outcome == "terminal", (
        f"Codex terminal never started while another process contended for its port: "
        f"{json.dumps(diagnostics, indent=1)}"
    )
    _open_terminal_view(page)
    _wait_terminal_connected(page)
