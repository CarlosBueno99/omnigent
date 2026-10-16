"""An orchestrator must learn when its message to a Claude Code worker is held, not injected.

A mock-driven ``openai-agents`` parent dispatches a real ``claude-native`` worker through
``sys_session_send``. The worker's scripted model calls ``AskUserQuestion``, which Claude Code
prompts for in every permission mode, so the runner starts holding new messages for that worker
(``202 {"status": "buffered", "detail": "Message buffered until the pending prompt is
resolved."}``). The parent then sends a follow-up to the same worker and asks for the
worker's info and history. The parent's tool results are what an orchestrator (and a
person reading its chat) sees, so they must say the follow-up is held and expose why.

"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._helpers.session import bind_session_runner, bundle_files, post_session_bundle
from tests.e2e_ui.conftest import (
    _ensure_runner_online,
    _server_state,
    _temp_omnigent_mock_config,
    configure_mock_llm,
    set_fallback_mock_llm,
)
from tests.e2e_ui.messages.test_message_render_parity import _select_view_mode

pytestmark = pytest.mark.timeout(900)

_PARENT_MODEL = "mock-held-message-parent"
_COMPOSER = 'textarea[aria-label="Message the agent"]'
_TERMINAL_VIEW = '[data-testid="terminal-view"]'
_PENDING_APPROVAL = '[data-testid="approval-card"][data-state="pending"]'
# The runner's fixed steer text (omnigent/runner/tool_dispatch.py); its presence in the
# nudge's tool output while the server still queues the message is the reported bug.
_INJECTED_CLAIM = "injected into the running turn"
# claude-native boot + first-run pre-accept + the prompt's hook round trip.
_CHILD_READY_TIMEOUT_S = 240.0
_PARENT_TURN_TIMEOUT_S = 120.0
_HOLD_OBSERVATION_S = 60.0


def _runner_visible_mock_url(mock_llm_server_url: str) -> str:
    """Return the mock URL the runner's processes can reach.

    Under the workflow-prepared environment the test process talks to the mock through a
    per-invocation relay URL; the runner resolves the mock at the address recorded in its
    environment file instead.
    """
    if os.environ.get("OMNIGENT_REPRO_SERVER_URL"):
        env_file = (
            Path(__file__).resolve().parents[3] / ".omnigent" / "repro-env" / "environment.json"
        )
        if env_file.is_file():
            return str(json.loads(env_file.read_text())["mock_url"])
    return mock_llm_server_url


def _claude_mock_provider(mock_llm_server_url: str) -> contextlib.AbstractContextManager[None]:
    """Point the worker's Claude Code at the mock for the journey's lifetime.

    The runner derives the pane's model endpoint from the provider config, so a
    standalone server needs the mock provider installed; the workflow-prepared
    environment already carries one, where this is a no-op.
    """
    return _temp_omnigent_mock_config(
        _runner_visible_mock_url(mock_llm_server_url),
        "claude",
        workflow_owned=bool(_server_state.get("workflow_owned")),
    )


def _orchestrator_yaml(name: str, mock_v1_url: str) -> str:
    """Compat-form spec: an openai-agents parent with one Claude Code worker.

    The worker calls ``AskUserQuestion``, which Claude Code prompts for in every mode.
    """
    return f"""\
name: {name}
prompt: |
  You are an orchestrator. Dispatch the worker sub-agent via sys_session_send when
  asked and relay what your tools return.

executor:
  harness: openai-agents
  model: {_PARENT_MODEL}
  auth:
    type: api_key
    api_key: mock-key
    base_url: {mock_v1_url}

tools:
  worker:
    type: agent
    description: Claude Code worker sub-agent (asks before running commands).
    executor:
      harness: claude-native
    prompt: |
      You are a Claude Code worker. Do exactly the one task you are given using
      your tools.
"""


@dataclass
class HeldMessageJourney:
    """State shared by the journey steps."""

    base_url: str
    mock_url: str
    parent_id: str
    nonce: str
    child_id: str | None = None
    worker_title: str = ""
    nudge_text: str = ""
    observations: dict[str, Any] = field(default_factory=dict)

    # Routing tokens must be equal length: the mock scores a match by (len, position),
    # so with full-history requests the rightmost (newest-turn) token only wins when all
    # tokens tie on length; otherwise an older turn's longer token keeps winning.
    @property
    def dispatch_token(self) -> str:
        return f"disp-{self.nonce}"

    @property
    def nudge_token(self) -> str:
        return f"nudg-{self.nonce}"

    @property
    def status_token(self) -> str:
        return f"stat-{self.nonce}"

    @property
    def worker_task_token(self) -> str:
        return f"work-{self.nonce}"


def _send_tool_call(call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {"call_id": call_id, "name": name, "arguments": json.dumps(arguments)}


def _prepare_parent(
    base_url: str,
    mock_url: str,
    tmp_path_factory: pytest.TempPathFactory,
    *,
    worker_prompts: bool,
) -> tuple[HeldMessageJourney, subprocess.Popen[bytes] | None]:
    """Register the orchestrator, bind its session, and script every model turn.

    :param worker_prompts: Script the worker's first model reply as a ``Bash`` tool call
        (Claude Code prompts for it) rather than plain text (the worker goes idle).
    """
    nonce = uuid.uuid4().hex[:8]
    journey = HeldMessageJourney(base_url=base_url, mock_url=mock_url, parent_id="", nonce=nonce)
    journey.worker_title = f"deploy-{nonce}"
    journey.nudge_text = f"nudge-message-{nonce}: permission granted, continue the deploy"

    set_fallback_mock_llm(mock_url, _PARENT_MODEL, "Acknowledged.")
    set_fallback_mock_llm(mock_url, "default", "ok")
    configure_mock_llm(
        mock_url,
        [
            {
                "tool_calls": [
                    _send_tool_call(
                        "call_dispatch",
                        "sys_session_send",
                        {
                            "agent": "worker",
                            "title": journey.worker_title,
                            "args": (
                                f"{journey.worker_task_token} Run the probe command with your "
                                "Bash tool and report its output."
                            ),
                        },
                    )
                ]
            },
            {"text": "Dispatched the worker; waiting for it to report back."},
        ],
        key=journey.dispatch_token,
        match=journey.dispatch_token,
    )
    configure_mock_llm(
        mock_url,
        [
            {
                "tool_calls": [
                    _send_tool_call(
                        "call_nudge",
                        "sys_session_send",
                        {
                            "agent": "worker",
                            "title": journey.worker_title,
                            "args": journey.nudge_text,
                        },
                    )
                ]
            },
            {"text": "Follow-up sent to the worker."},
        ],
        key=journey.nudge_token,
        match=journey.nudge_token,
    )
    worker_turn: list[dict[str, Any]]
    if worker_prompts:
        worker_turn = [
            {
                "tool_calls": [
                    _send_tool_call(
                        "toolu_probe",
                        "AskUserQuestion",
                        {
                            "questions": [
                                {
                                    "question": f"Proceed with probe-{nonce}?",
                                    "header": "Confirm",
                                    "multiSelect": False,
                                    "options": [
                                        {"label": "Yes", "description": "run the probe"},
                                        {"label": "No", "description": "skip it"},
                                    ],
                                }
                            ]
                        },
                    )
                ]
            },
            {"text": f"Probe finished: probe-{nonce}."},
        ]
    else:
        worker_turn = [{"text": f"Worker ready: probe-{nonce}."}]
    configure_mock_llm(
        mock_url, worker_turn, key=journey.worker_task_token, match=journey.worker_task_token
    )

    respawned = _ensure_runner_online(base_url, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])
    name = f"held-message-orchestrator-{nonce}"
    bundle = bundle_files(
        {
            f"{name}.yaml": _orchestrator_yaml(
                name, f"{_runner_visible_mock_url(mock_url)}/v1"
            ).encode()
        }
    )
    created = post_session_bundle(httpx.post, f"{base_url}/v1/sessions", bundle, timeout=30.0)
    created.raise_for_status()
    journey.parent_id = str(created.json()["session_id"])
    bind_session_runner(httpx.patch, base_url, journey.parent_id, runner_id, timeout=10.0)
    return journey, respawned


def _script_status_checks(journey: HeldMessageJourney) -> None:
    """Script the parent's info + history calls once the child id is known."""
    configure_mock_llm(
        journey.mock_url,
        [
            {
                "tool_calls": [
                    _send_tool_call(
                        "call_info", "sys_session_get_info", {"session_id": journey.child_id}
                    ),
                    _send_tool_call(
                        "call_history",
                        "sys_session_get_history",
                        {"conversation_id": journey.child_id},
                    ),
                ]
            },
            {"text": "Checked the worker's status."},
        ],
        key=journey.status_token,
        match=journey.status_token,
    )


def _wait_for(predicate: Callable[[], bool], *, timeout_s: float, interval_s: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


def _items(base_url: str, session_id: str) -> list[dict[str, Any]]:
    resp = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/items",
        params={"order": "asc", "limit": 1000},
        timeout=10.0,
    )
    resp.raise_for_status()
    return resp.json()["data"]


def _tool_output(base_url: str, session_id: str, call_id: str) -> str | None:
    """Return the parent's ``function_call_output`` text for *call_id*, if recorded yet."""
    for item in _items(base_url, session_id):
        data = item.get("data") or {}
        typ = item.get("type") or data.get("type")
        cid = item.get("call_id") or data.get("call_id")
        if typ == "function_call_output" and cid == call_id:
            output = item.get("output") or data.get("output")
            return str(output) if output is not None else ""
    return None


def _snapshot(base_url: str, session_id: str) -> dict[str, Any]:
    resp = httpx.get(
        f"{base_url}/v1/sessions/{session_id}", params={"include_items": "false"}, timeout=10.0
    )
    resp.raise_for_status()
    return resp.json()


def _child_rows(base_url: str, parent_id: str) -> list[dict[str, Any]]:
    resp = httpx.get(f"{base_url}/v1/sessions/{parent_id}/child_sessions", timeout=10.0)
    if resp.status_code != 200:
        return []
    return resp.json().get("data") or []


def _pending_elicitation_seen(base_url: str, parent_id: str, child_id: str | None) -> bool:
    """Whether the worker's forwarded prompt is parked on the parent or child snapshot."""
    for session_id in (parent_id, child_id):
        if session_id and (_snapshot(base_url, session_id).get("pending_elicitations") or []):
            return True
    return False


def _transcript_text(base_url: str, session_id: str) -> str:
    return json.dumps(_items(base_url, session_id))


def _send(page: Page, text: str) -> None:
    composer = page.locator(_COMPOSER)
    expect(composer).to_be_visible(timeout=30_000)
    expect(composer).to_be_enabled(timeout=30_000)
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def _await_tool_output(journey: HeldMessageJourney, call_id: str) -> str:
    holder: dict[str, str] = {}

    def _done() -> bool:
        output = _tool_output(journey.base_url, journey.parent_id, call_id)
        if output is None:
            return False
        holder["output"] = output
        return True

    assert _wait_for(_done, timeout_s=_PARENT_TURN_TIMEOUT_S), (
        f"parent never recorded the {call_id} tool result"
    )
    return holder["output"]


def _dispatch_worker(page: Page, journey: HeldMessageJourney) -> None:
    """Send the parent its dispatch message and wait until the worker session exists."""
    page.goto(f"{journey.base_url}/c/{journey.parent_id}")
    _send(page, f"{journey.dispatch_token} Dispatch the worker to run the probe.")

    def _child_seen() -> bool:
        rows = _child_rows(journey.base_url, journey.parent_id)
        if rows:
            journey.child_id = str(rows[0].get("id") or rows[0].get("session_id"))
            return True
        return False

    assert _wait_for(_child_seen, timeout_s=_PARENT_TURN_TIMEOUT_S), (
        "parent never dispatched the worker"
    )
    assert journey.child_id
    _script_status_checks(journey)


def _expand_tool_card(page: Page, name: str, nth: int = -1) -> None:
    """Best-effort: open a collapsed tool card so its Output panel is on screen for the film."""
    cards = page.get_by_role("button", name=name)
    target = cards.last if nth < 0 else cards.nth(nth)
    try:
        expect(target).to_be_visible(timeout=15_000)
        target.scroll_into_view_if_needed()
        target.click()
        page.wait_for_timeout(500)
    except Exception:
        return


def _save_evidence(journey: HeldMessageJourney, label: str) -> None:
    """Persist observations for reviewers when an evidence directory is requested."""
    evidence_dir = os.environ.get("OMNIGENT_REPRO_EVIDENCE_DIR")
    if not evidence_dir:
        return
    path = Path(evidence_dir) / f"{label}-{journey.nonce}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "parent_session_id": journey.parent_id,
                "child_session_id": journey.child_id,
                "worker_title": journey.worker_title,
                "nudge_text": journey.nudge_text,
                "observations": journey.observations,
            },
            indent=2,
            default=str,
        )
        + "\n"
    )


def _observe_held_send(page: Page, journey: HeldMessageJourney) -> list[str]:
    """Nudge the parked worker, then record what the orchestrator was told versus what is held."""
    assert journey.child_id
    base_url, child_id = journey.base_url, journey.child_id
    before = _snapshot(base_url, child_id)
    journey.observations["child_before_nudge"] = {
        "status": before.get("status"),
        "pending_elicitations": len(before.get("pending_elicitations") or []),
        "pending_inputs": before.get("pending_inputs"),
    }

    _send(
        page, f"{journey.nudge_token} Tell the worker the permission is granted and to continue."
    )
    nudge_output = _await_tool_output(journey, "call_nudge")
    journey.observations["sys_session_send_output"] = nudge_output
    _expand_tool_card(page, "sys_session_send")
    page.wait_for_timeout(3_000)

    _send(page, f"{journey.status_token} Check on the worker.")
    info_output = _await_tool_output(journey, "call_info")
    history_output = _await_tool_output(journey, "call_history")
    journey.observations["sys_session_get_info_output"] = info_output
    journey.observations["sys_session_get_history_output"] = history_output
    # The parent batches both status reads into one card; label and expand are best-effort.
    _expand_tool_card(page, "Called 2 tools")
    page.wait_for_timeout(2_000)

    # The hold is only a hold if the message is still queued after a healthy delivery would
    # long since have landed; keep the prompt open and re-read the server state.
    time.sleep(_HOLD_OBSERVATION_S)
    after = _snapshot(base_url, child_id)
    pending_inputs = after.get("pending_inputs") or []
    held_server_side = any(journey.nudge_text in json.dumps(entry) for entry in pending_inputs)
    in_transcript = journey.nudge_text in _transcript_text(base_url, child_id)
    journey.observations["child_after_hold"] = {
        "status": after.get("status"),
        "pending_elicitations": len(after.get("pending_elicitations") or []),
        "pending_inputs": pending_inputs,
        "nudge_in_transcript": in_transcript,
        "observed_for_s": _HOLD_OBSERVATION_S,
    }

    failures: list[str] = []
    if held_server_side and not in_transcript:
        if _INJECTED_CLAIM in nudge_output:
            failures.append(
                "sys_session_send told the orchestrator the message was injected into the running "
                "turn, but the server still queues it in pending_inputs after "
                f"{_HOLD_OBSERVATION_S:.0f}s: {nudge_output}"
            )
        try:
            info = json.loads(info_output)
        except json.JSONDecodeError:
            info = {}
        exposes_hold = bool(info.get("blocked_on")) or bool(info.get("pending_inputs"))
        if info.get("status") == "running" and not exposes_hold:
            failures.append(
                "sys_session_get_info reports a bare 'running' with no blocked_on reason and no "
                f"pending_inputs while the worker is parked with a held message: {info_output}"
            )
        if journey.nudge_text not in history_output:
            failures.append(
                "sys_session_get_history does not show the held message anywhere: "
                f"{history_output[:500]}"
            )
    else:
        failures.append(
            "precondition not met: the nudge was not held server-side "
            f"(pending_inputs={pending_inputs!r}, in_transcript={in_transcript})"
        )
    return failures


def _show_worker_terminal(page: Page, journey: HeldMessageJourney) -> None:
    """Navigate to the worker's terminal so its still-open prompt is on screen."""
    assert journey.child_id
    page.goto(f"{journey.base_url}/c/{journey.child_id}")
    expect(page.get_by_test_id("view-mode-toggle")).to_be_visible(timeout=60_000)
    _select_view_mode(page, "Terminal")
    expect(page.locator(_TERMINAL_VIEW).last).to_have_attribute(
        "data-state", "connected", timeout=60_000
    )
    page.wait_for_timeout(4_000)


def _cleanup(
    page: Page, journey: HeldMessageJourney, respawned: subprocess.Popen[bytes] | None
) -> None:
    # Stop filming before the sessions disappear, so a recording ends on the journey's state.
    if not page.is_closed():
        page.context.close()
    for session_id in (journey.child_id, journey.parent_id):
        if session_id:
            httpx.post(
                f"{journey.base_url}/v1/sessions/{session_id}/events",
                json={"type": "stop_session"},
                timeout=10.0,
            )
            httpx.delete(f"{journey.base_url}/v1/sessions/{session_id}", timeout=10.0)
    if respawned is not None:
        respawned.terminate()
        try:
            respawned.wait(timeout=5)
        except subprocess.TimeoutExpired:
            respawned.kill()
            respawned.wait(timeout=5)


def test_send_to_worker_parked_on_permission_prompt_is_reported_as_held(
    request: pytest.FixtureRequest,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """A follow-up held behind the worker's question prompt must not be reported as injected."""
    journey, respawned = _prepare_parent(
        live_server, mock_llm_server_url, tmp_path_factory, worker_prompts=True
    )
    page: Page = request.getfixturevalue("page")
    try:
        with _claude_mock_provider(mock_llm_server_url):
            _dispatch_worker(page, journey)
            assert journey.child_id
            # AskUserQuestion parks the worker (Claude Code prompts for a question in every
            # permission mode); the forwarded elicitation is the signal the runner will now
            # hold a new send instead of injecting it.
            assert _wait_for(
                lambda: _pending_elicitation_seen(
                    live_server, journey.parent_id, journey.child_id
                ),
                timeout_s=_CHILD_READY_TIMEOUT_S,
                interval_s=2.0,
            ), "worker never parked on its question prompt (no forwarded elicitation)"
            # Bring the prompt card on screen for the recording; the hold is proven
            # server-side below.
            with contextlib.suppress(AssertionError):
                expect(page.locator(_PENDING_APPROVAL).first).to_be_visible(timeout=30_000)
            failures = _observe_held_send(page, journey)
            _show_worker_terminal(page, journey)
            _save_evidence(journey, "permission-prompt")
            assert not failures, "\n".join(failures)
    finally:
        _cleanup(page, journey, respawned)
