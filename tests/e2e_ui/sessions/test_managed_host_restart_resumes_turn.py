"""A top-level managed session must continue its interrupted turn after its host restarts.

Real server, a managed-host stand-in (the test-gateway sandbox launcher starts a
real ``omnigent host`` daemon locally), the host-launched runner and the mock LLM
all run as separate processes; the SPA is driven in headless Chromium. The turn
is held open by the mock, the host and runner are killed and kept down past the
runner liveness window, the host is relaunched with its original identity, and
the runner is woken without a user message.

Run::

    OMNIGENT_E2E_RECORD_DIR="$PWD/recordings/omni-12806/raw" \\
        python -m pytest tests/e2e_ui/sessions/test_managed_host_restart_resumes_turn.py \\
        --ui-skip-build -k same_server
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import psutil
import pytest
import yaml
from playwright.sync_api import Page, expect, sync_playwright

from omnigent.stores.conversation_store import RUNNER_LIVENESS_TTL_S
from tests._helpers.server_runner import server_runner
from tests.e2e.conftest import (
    configure_mock_llm,
    get_mock_requests,
    release_mock_gate,
    set_fallback_mock_llm,
)
from tests.e2e_ui.conftest import open_right_rail

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PASSTHROUGH_ENV = frozenset({"PATH", "LANG", "LC_ALL", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR"})
_PROMPT = "Summarize the restart recovery notes."
_MARKER = "MANAGED_RESTART_RESUMED_REPLY"
_AGENT_NAME = "managed_restart_probe"
_MODEL = "managed-restart-probe"
_HOST_ENTRY = "omnigent.host._daemon_entry"
_WORKING = '[data-testid="working-indicator"]'
_ERROR_PILL = '[data-testid="error-pill"]'
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'

pytestmark = [pytest.mark.timeout(900, method="signal")]


def _poll(check: Callable[[], Any], what: str, *, timeout: float, interval: float = 1.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError(f"timed out after {timeout:.0f}s waiting for {what}")


def _snapshot(client: httpx.Client, session_id: str) -> dict[str, Any]:
    response = client.get(f"/v1/sessions/{session_id}")
    response.raise_for_status()
    return response.json()


def _list_entry(client: httpx.Client, session_id: str) -> dict[str, Any] | None:
    response = client.get("/v1/sessions", params={"visibility": "all", "limit": 100})
    response.raise_for_status()
    return next((row for row in response.json()["data"] if row["id"] == session_id), None)


def _gate_pending(mock_url: str) -> bool:
    response = httpx.get(f"{mock_url}/gate/pending", timeout=5.0, trust_env=False)
    response.raise_for_status()
    return bool(response.json().get("pending"))


def _runner_online(client: httpx.Client, runner_id: str) -> bool:
    response = client.get(f"/v1/runners/{runner_id}/status", timeout=5.0)
    return response.status_code == 200 and response.json().get("online") is True


def _host_online(client: httpx.Client, host_id: str) -> bool:
    response = client.get("/v1/hosts", timeout=5.0)
    response.raise_for_status()
    return any(
        row.get("host_id") == host_id and row.get("status") == "online"
        for row in response.json().get("hosts", [])
    )


def _find_host_process(server_pid: int) -> psutil.Process:
    for child in psutil.Process(server_pid).children(recursive=True):
        try:
            if _HOST_ENTRY in " ".join(child.cmdline()):
                return child
        except psutil.Error:
            continue
    raise AssertionError("the managed launcher did not start a host daemon under the server")


def _kill_tree(root: psutil.Process) -> list[int]:
    """SIGKILL the host daemon and everything it launched (zygote + runner)."""
    victims = [*root.children(recursive=True), root]
    pids = []
    for proc in victims:
        try:
            pids.append(proc.pid)
            proc.send_signal(signal.SIGKILL)
        except psutil.Error:
            continue
    psutil.wait_procs(victims, timeout=15)
    return pids


def _runner_init_events(host_data: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for log in sorted((host_data / "logs" / "runner").glob("*.log")):
        for line in log.read_text(errors="replace").splitlines():
            if "Runner session initialization finished" not in line:
                continue
            fields = dict(re.findall(r"(\w+)=([^\s]+)", line))
            fields["log"] = log.name
            events.append(fields)
    return events


class _Evidence:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.t0 = time.monotonic()
        self.timeline: list[dict[str, Any]] = []

    def note(self, step: str, **fields: Any) -> None:
        entry = {"t": round(time.monotonic() - self.t0, 1), "step": step, **fields}
        self.timeline.append(entry)
        print(json.dumps(entry, default=str), flush=True)

    def shot(self, page: Page, name: str) -> None:
        try:
            page.screenshot(path=str(self.directory / f"{name}.png"))
        except Exception as exc:
            self.note("screenshot_failed", name=name, error=str(exc))

    def save(self) -> None:
        (self.directory / "timeline.json").write_text(
            json.dumps(self.timeline, indent=1, default=str)
        )


@pytest.mark.parametrize(
    "server_restarts_during_grace",
    [False, True],
    ids=["same_server", "server_recycled_within_grace"],
)
def test_top_level_managed_session_resumes_interrupted_turn_after_host_restart(
    tmp_path: Path,
    mock_llm_server_url: str,
    built_spa: None,
    server_restarts_during_grace: bool,
) -> None:
    """The interrupted top-level turn continues once its managed host's runner is back.

    ``same_server`` keeps one server process (its disconnect grace timer marks the
    session Disconnected). ``server_recycled_within_grace`` restarts the server on
    the same database right after the kill, so the replacement replica reaches the
    stale ``running`` row through the lazy session-list reconcile instead.
    """
    evidence_root = os.environ.get("OMNI_12806_EVIDENCE_DIR")
    evidence = _Evidence(
        (Path(evidence_root) if evidence_root else tmp_path / "evidence")
        / ("journey-recycled" if server_restarts_during_grace else "journey")
    )
    mock_url = mock_llm_server_url
    root = tmp_path / "managed"
    root.mkdir()
    config = root / "config"
    config.mkdir()
    model = _MODEL
    # The landing-page agent picker lists registered agents, so the probe is a
    # built-in rather than a session-scoped bundle.
    agent_yaml = root / f"{_AGENT_NAME}.yaml"
    agent_yaml.write_text(
        yaml.safe_dump(
            {
                "name": _AGENT_NAME,
                "prompt": "Reply with the configured answer.",
                "executor": {
                    "harness": "openai-agents",
                    "model": model,
                    "auth": {
                        "type": "api_key",
                        "api_key": "mock-key",
                        "base_url": f"{mock_url}/v1",
                    },
                },
                "terminals": {"bash": {"command": "bash", "args": ["--noprofile", "--norc"]}},
            }
        )
    )
    set_fallback_mock_llm(mock_url, key="_policy_llm_", text='{"action":"allow","reason":""}')
    # The original turn holds inside the mock; the continuation's request answers
    # at once so the reply can land without a user message.
    configure_mock_llm(mock_url, [{"text": _MARKER, "block": True}, {"text": _MARKER}], key=model)
    set_fallback_mock_llm(mock_url, key=model, text=_MARKER)
    base_env = {key: value for key, value in os.environ.items() if key in _PASSTHROUGH_ENV}
    server_env = {
        "OMNIGENT_CONFIG_HOME": str(config),
        "OMNIGENT_BUILTIN_AGENT_DIRS": str(agent_yaml),
        "OMNIGENT_E2E_GATEWAY_ROOT": str(root),
        "OMNIGENT_RUNNER_TUNNEL_TOKEN": None,
        "OPENAI_API_KEY": "mock-key",
        "OPENAI_BASE_URL": f"{mock_url}/v1",
    }
    relaunched_host: subprocess.Popen[bytes] | None = None
    record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
    with (
        server_runner(
            root / "stack",
            server_bootstrap="from tests.e2e._gateway_preview_server import main; main()",
            server_env=server_env,
            base_env=base_env,
            server_cwd=_REPO_ROOT,
        ) as stack,
        httpx.Client(base_url=stack.base_url, trust_env=False, timeout=60) as client,
        sync_playwright() as playwright,
    ):
        agents = client.get("/v1/agents").json()["data"]
        agent_id = next(agent["id"] for agent in agents if agent["name"] == _AGENT_NAME)
        browser = playwright.chromium.launch()
        context_options: dict[str, Any] = {"viewport": {"width": 1440, "height": 900}}
        if record_dir:
            context_options["record_video_dir"] = record_dir
        context = browser.new_context(**context_options)
        page = context.new_page()
        session_id = ""
        try:
            evidence.note("open_new_chat", url=stack.base_url)
            page.goto(f"{stack.base_url}/")
            expect(page.get_by_test_id("new-chat-landing-input")).to_be_visible(timeout=30_000)
            page.get_by_test_id("new-chat-landing-agent-select").click()
            page.get_by_test_id(f"new-chat-landing-agent-{agent_id}").click()
            page.get_by_test_id("new-chat-landing-host-chip").click()
            sandbox_row = page.get_by_test_id("new-chat-landing-sandbox-option")
            expect(sandbox_row).to_be_visible(timeout=15_000)
            evidence.note("sandbox_row", label=sandbox_row.inner_text())
            sandbox_row.click()
            page.get_by_test_id("new-chat-landing-input").fill(_PROMPT)
            expect(page.get_by_test_id("new-chat-landing-submit")).to_be_enabled()
            page.get_by_test_id("new-chat-landing-submit").click()
            expect(page).to_have_url(re.compile(r"/c/[a-f0-9]+"), timeout=30_000)
            session_id = page.url.rsplit("/", 1)[-1]
            evidence.note("managed_session_created", session_id=session_id)

            def bound_row() -> dict[str, Any] | None:
                row = _snapshot(client, session_id)
                return row if row.get("runner_id") else None

            bound = _poll(bound_row, "the managed host and runner to bind", timeout=120)
            host_id, runner_id = bound["host_id"], bound["runner_id"]
            _poll(lambda: _runner_online(client, runner_id), "runner tunnel online", timeout=90)
            _poll(
                lambda: _gate_pending(mock_url),
                "the turn to block inside the mock LLM",
                timeout=90,
            )
            expect(page.locator(_WORKING)).to_be_visible(timeout=30_000)
            evidence.note(
                "turn_in_flight",
                host_id=host_id,
                runner_id=runner_id,
                status=_snapshot(client, session_id).get("status"),
            )
            evidence.shot(page, "01-turn-in-flight")

            host_proc = _find_host_process(stack.server.pid)
            host_spec = {
                "cmdline": host_proc.cmdline(),
                "cwd": host_proc.cwd(),
                "env": host_proc.environ(),
            }
            host_data = Path(host_spec["env"]["OMNIGENT_DATA_DIR"])
            killed = _kill_tree(host_proc)
            kill_at = time.monotonic()
            evidence.note("host_and_runner_killed", pids=killed, host_pid=host_proc.pid)

            if server_restarts_during_grace:
                stack.restart_server()
                evidence.note("server_recycled", pid=stack.server.pid)
                page.reload()

            def disconnected_state() -> dict[str, Any] | None:
                row = _snapshot(client, session_id)
                error = row.get("last_task_error") or {}
                if row.get("status") == "failed" and error.get("code") == "runner_disconnected":
                    return row
                if server_restarts_during_grace and row.get("status") != "running":
                    return row
                return None

            settled = _poll(
                disconnected_state,
                "the session to leave 'running' after the liveness window",
                timeout=RUNNER_LIVENESS_TTL_S + 70,
            )
            pill = page.locator(_ERROR_PILL)
            pill_text = pill.first.inner_text() if pill.count() else ""
            evidence.note(
                "after_liveness_window",
                seconds_since_kill=round(time.monotonic() - kill_at, 1),
                status=settled.get("status"),
                last_task_error=settled.get("last_task_error"),
                error_pill=pill_text,
            )
            evidence.shot(page, "02-after-liveness-window")

            # Facet 2: what a session-list read settles the row to.
            page.reload()
            expect(page.locator('[aria-label="Message the agent"]')).to_be_visible(timeout=30_000)
            entry = _list_entry(client, session_id)
            pill_text = pill.first.inner_text() if pill.count() else ""
            evidence.note(
                "after_list_reload",
                list_status=(entry or {}).get("status"),
                snapshot_status=_snapshot(client, session_id).get("status"),
                last_task_error=_snapshot(client, session_id).get("last_task_error"),
                error_pill=pill_text,
            )
            evidence.shot(page, "03-after-list-reload")

            # Trigger 2A: the sandbox comes back by itself with the same host identity.
            relaunch_log = open(evidence.directory / "relaunched-host.log", "ab")  # noqa: SIM115
            relaunched_host = subprocess.Popen(
                host_spec["cmdline"],
                cwd=host_spec["cwd"],
                env=host_spec["env"],
                stdout=relaunch_log,
                stderr=subprocess.STDOUT,
            )
            _poll(
                lambda: _host_online(client, host_id),
                "the relaunched host to come online",
                timeout=90,
            )
            host_back_at = time.monotonic()
            evidence.note("host_relaunched_online", host_pid=relaunched_host.pid)
            runner_reconnected_alone = False
            while time.monotonic() - host_back_at < 120:
                row = _snapshot(client, session_id)
                if row.get("runner_id") and _runner_online(client, row["runner_id"]):
                    runner_reconnected_alone = True
                    break
                time.sleep(5)
            evidence.note(
                "after_host_back_watch",
                runner_reconnected_without_user_action=runner_reconnected_alone,
                status=_snapshot(client, session_id).get("status"),
                runner_id=_snapshot(client, session_id).get("runner_id"),
            )
            evidence.shot(page, "04-host-back-no-runner")

            # Trigger 2B: a non-message wake — open a shell for the session.
            if not runner_reconnected_alone:
                open_right_rail(page)
                page.get_by_role("button", name="Open new").click()
                page.get_by_role("menuitem", name=re.compile(r"^Shell")).click()
                evidence.note("shell_wake_requested")
                evidence.shot(page, "05-shell-wake-requested")

                def woken_runner_online() -> bool:
                    row = _snapshot(client, session_id)
                    return bool(row.get("runner_id")) and _runner_online(client, row["runner_id"])

                _poll(woken_runner_online, "the woken runner to come online", timeout=120)
            runner_online_at = time.monotonic()
            new_runner_id = _snapshot(client, session_id)["runner_id"]
            evidence.note("runner_online_after_restart", runner_id=new_runner_id)

            # Facet 1: does the interrupted turn continue on its own?
            continued = False
            while time.monotonic() - runner_online_at < 60:
                row = _snapshot(client, session_id)
                if row.get("status") == "running" or _gate_pending(mock_url):
                    continued = True
                    break
                time.sleep(2)
            inits = _runner_init_events(host_data)
            evidence.note(
                "after_reconnect_watch",
                continued=continued,
                status=_snapshot(client, session_id).get("status"),
                gate_pending=_gate_pending(mock_url),
                model_requests=len(get_mock_requests(mock_url, key=model)),
                runner_session_inits=inits,
                working_visible=page.locator(_WORKING).count(),
                error_pill=(pill.first.inner_text() if pill.count() else ""),
            )
            evidence.shot(page, "06-after-reconnect")
            release_mock_gate(mock_url)
            deadline = time.monotonic() + 30
            marker_seen = False
            while time.monotonic() < deadline:
                if _MARKER in client.get(f"/v1/sessions/{session_id}/items").text:
                    marker_seen = True
                    break
                time.sleep(2)
            evidence.note(
                "after_gate_release",
                marker_in_transcript=marker_seen,
                status=_snapshot(client, session_id).get("status"),
                assistant_bubbles=page.locator(_ASSISTANT).count(),
            )
            if marker_seen:
                expect(page.locator(_ASSISTANT).filter(has_text=_MARKER)).to_be_visible(
                    timeout=15_000
                )
            page.wait_for_timeout(3_000)
            evidence.shot(page, "07-final")
            # Expected once a top-level managed session resumes like a sub-agent does:
            # the runner reconnects on its own when the host returns and the blocked
            # reply lands with no user message. (`continued` alone is unreliable — the
            # killed turn's original gate stays pending regardless.)
            resumed_without_user_action = runner_reconnected_alone and marker_seen
            assert resumed_without_user_action, (
                f"Session {session_id}: a top-level managed session did not resume its "
                f"interrupted turn after its host ({new_runner_id}) restarted. Expected the "
                f"runner to reconnect on its own and the blocked reply {_MARKER!r} to land in "
                f"the transcript with no user message. Observed "
                f"runner_reconnected_without_user_action={runner_reconnected_alone}, "
                f"reply_completed={marker_seen}, "
                f"status={_snapshot(client, session_id).get('status')!r}, "
                f"model_requests={len(get_mock_requests(mock_url, key=model))}, "
                f"runner inits={inits}."
            )
        finally:
            evidence.note("teardown", session_id=session_id)
            evidence.save()
            try:
                context.close()
            finally:
                browser.close()
            if relaunched_host is not None:
                for proc in [*psutil.Process(relaunched_host.pid).children(recursive=True)]:
                    with contextlib.suppress(psutil.Error):
                        proc.send_signal(signal.SIGKILL)
                relaunched_host.kill()
                relaunched_host.wait(timeout=10)
