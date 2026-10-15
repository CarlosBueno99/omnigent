"""E2E: an agent's scheduled-task tool calls must park on an approval prompt.

Every agent ``sys_add_policy`` call parks for approval even when the session has
no guardrails (the engine's mandatory ``__ask_on_add_policy`` guard). The
``sys_scheduled_task_create`` / ``sys_scheduled_task_update`` tools create and
change durable recurring work through the same relay, so they are held to the
same bar: the chat shows a pending approval card before the task exists or
changes, and the Automations page only reflects the change once the user
approves. The mock model scripts the tool calls on a guardrail-free
openai-agents agent; both tests drive the chat composer and read the result on
the Automations page.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    _create_bundled_session,
    _ensure_runner_online,
    _server_state,
    configure_mock_llm,
    set_fallback_mock_llm,
)

_APPROVAL_CARD = '[data-testid="approval-card"][data-state="pending"]'
_TASK_ROW = '[data-testid="scheduled-task-row"]'
_RRULE = "FREQ=DAILY;BYHOUR=9;BYMINUTE=0"
_CREATE_REPLY = "Created the daily automation."
_UPDATE_REPLY = "Paused the automation."
_TURN_TIMEOUT_MS = 90_000
_RECORD_HOLD_MS = 2_500

_AGENT_YAML = """\
spec_version: 1
name: scheduling_probe
prompt: |
  You manage the user's automations with the sys_scheduled_task_* tools. When
  the user asks to create or change an automation, call the matching tool, then
  reply with one short sentence.

executor:
  model: {model}
  config:
    harness: openai-agents
"""


def _schedulable_agent_id(base_url: str) -> str:
    """An agent a task can be scheduled against (``hello_world`` when registered)."""
    agents = httpx.get(f"{base_url}/v1/agents", timeout=10.0).json()["data"]
    by_name = {agent["name"]: agent["id"] for agent in agents}
    for name in ("hello_world", "polly"):
        if name in by_name:
            return by_name[name]
    assert agents, "no agents registered to schedule a task against"
    return agents[0]["id"]


def _tasks_named(base_url: str, name: str) -> list[dict]:
    tasks = httpx.get(f"{base_url}/v1/scheduled-tasks", timeout=10.0).json()["scheduled_tasks"]
    return [task for task in tasks if task["name"] == name]


def _pending_elicitations(base_url: str, session_id: str) -> list[dict]:
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    resp.raise_for_status()
    return resp.json().get("pending_elicitations") or []


def _send(page: Page, text: str) -> None:
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def _await_gate_or_reply(page: Page, reply: str) -> str:
    """Return ``"approval"`` if a pending card shows first, ``"reply"`` if the turn ends first."""
    card = page.locator(_APPROVAL_CARD).first
    reply_text = page.get_by_text(reply, exact=True).first
    deadline = time.monotonic() + _TURN_TIMEOUT_MS / 1000
    while time.monotonic() < deadline:
        if card.count() and card.is_visible():
            return "approval"
        if reply_text.count() and reply_text.is_visible():
            return "reply"
        page.wait_for_timeout(250)
    raise AssertionError(f"neither an approval card nor the reply {reply!r} appeared")


def _approve(page: Page, reply: str) -> None:
    page.locator(_APPROVAL_CARD).first.get_by_role("button", name="Approve").click()
    expect(page.get_by_text(reply, exact=True).first).to_be_visible(timeout=_TURN_TIMEOUT_MS)


def _hold(page: Page) -> None:
    """Keep the asserted state on screen while the journey is being recorded."""
    if os.environ.get("OMNIGENT_E2E_RECORD_DIR"):
        page.wait_for_timeout(_RECORD_HOLD_MS)


def _open_task_row(page: Page, base_url: str, name: str):
    page.goto(f"{base_url}/tasks")
    row = page.locator(_TASK_ROW).filter(has_text=name)
    expect(row).to_be_visible(timeout=30_000)
    _hold(page)
    return row


@pytest.fixture
def scheduling_session(
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str, str, str]]:
    """``(base_url, session_id, model_key, task_name)`` on a guardrail-free agent.

    The first scripted model response is the ``sys_scheduled_task_create`` call
    for ``task_name``; the fallback text ends the turn.
    """
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])
    model = f"sched-approval-{uuid.uuid4().hex[:8]}"
    task_name = f"Daily digest {uuid.uuid4().hex[:6]}"
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_sched_create",
                        "name": "sys_scheduled_task_create",
                        "arguments": json.dumps(
                            {
                                "name": task_name,
                                "prompt": "Summarize yesterday's activity.",
                                "rrule": _RRULE,
                                "agent_id": _schedulable_agent_id(live_server),
                                "timezone": "UTC",
                            }
                        ),
                    }
                ]
            }
        ],
        key=model,
    )
    set_fallback_mock_llm(mock_llm_server_url, model, _CREATE_REPLY)
    session_id = _create_bundled_session(live_server, runner_id, _AGENT_YAML.format(model=model))
    policies = httpx.get(f"{live_server}/v1/sessions/{session_id}/policies", timeout=10.0).json()
    assert policies["data"] == [], policies
    assert not _tasks_named(live_server, task_name)
    try:
        yield (live_server, session_id, model, task_name)
    finally:
        for task in _tasks_named(live_server, task_name):
            httpx.delete(f"{live_server}/v1/scheduled-tasks/{task['id']}", timeout=10.0)
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        if respawned is not None:
            respawned.terminate()
            respawned.wait(timeout=5)


def _create_turn(page: Page, base_url: str, session_id: str, task_name: str) -> str:
    """Ask for the automation; return which came first, the approval card or the reply."""
    page.goto(f"{base_url}/c/{session_id}")
    _send(
        page,
        f"Create an automation named '{task_name}' that summarizes yesterday's "
        "activity every day at 09:00 UTC.",
    )
    outcome = _await_gate_or_reply(page, _CREATE_REPLY)
    if outcome == "approval":
        assert not _tasks_named(base_url, task_name), "task was created before approval"
        _hold(page)
        _approve(page, _CREATE_REPLY)
    _hold(page)
    return outcome


@pytest.mark.timeout(300)
def test_agent_scheduled_task_create_requires_approval(
    request: pytest.FixtureRequest,
    scheduling_session: tuple[str, str, str, str],
) -> None:
    """The agent's create call parks on an approval card before the task exists."""
    base_url, session_id, _model, task_name = scheduling_session
    page: Page = request.getfixturevalue("page")

    outcome = _create_turn(page, base_url, session_id, task_name)
    pending = _pending_elicitations(base_url, session_id)
    _open_task_row(page, base_url, task_name)

    assert outcome == "approval", (
        f"agent created automation {task_name!r} with no approval prompt: the reply "
        f"{_CREATE_REPLY!r} arrived with pending_elicitations={pending} and the "
        "Automations page lists the task"
    )


@pytest.mark.timeout(300)
def test_agent_scheduled_task_update_requires_approval(
    request: pytest.FixtureRequest,
    mock_llm_server_url: str,
    scheduling_session: tuple[str, str, str, str],
) -> None:
    """The agent's update call parks on an approval card before the task changes."""
    base_url, session_id, model, task_name = scheduling_session
    page: Page = request.getfixturevalue("page")

    _create_turn(page, base_url, session_id, task_name)
    (task,) = _tasks_named(base_url, task_name)
    assert task["state"] == "active"
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_sched_pause",
                        "name": "sys_scheduled_task_update",
                        "arguments": json.dumps(
                            {"scheduled_task_id": task["id"], "state": "paused"}
                        ),
                    }
                ]
            }
        ],
        key=model,
    )
    set_fallback_mock_llm(mock_llm_server_url, model, _UPDATE_REPLY)

    _send(page, "Pause that automation.")
    outcome = _await_gate_or_reply(page, _UPDATE_REPLY)
    if outcome == "approval":
        assert _tasks_named(base_url, task_name)[0]["state"] == "active", (
            "task was paused before approval"
        )
        _hold(page)
        _approve(page, _UPDATE_REPLY)
    _hold(page)
    pending = _pending_elicitations(base_url, session_id)
    row = _open_task_row(page, base_url, task_name)
    expect(row.get_by_test_id("task-paused-pill")).to_be_visible()

    assert outcome == "approval", (
        f"agent paused automation {task_name!r} with no approval prompt: the reply "
        f"{_UPDATE_REPLY!r} arrived with pending_elicitations={pending} and the "
        "Automations page shows it paused"
    )
