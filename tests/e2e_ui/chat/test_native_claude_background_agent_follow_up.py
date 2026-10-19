"""A Claude follow-up is delivered while a backgrounded sub-agent outlives the turn."""

from __future__ import annotations

import json
import time
import uuid

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import get_mock_requests
from tests.e2e_ui.conftest import configure_mock_llm
from tests.e2e_ui.messages.test_message_render_parity import _ensure_chat_view
from tests.e2e_ui.messages.test_native_claude_render_parity import (
    _open_terminal_view,
    _wait_terminal_connected,
)

_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_FIRST_TURN_TIMEOUT_MS = 180_000
# Outlasts the follow-up assertions, so the sub-agent is still running throughout.
_SUB_AGENT_HOLD_S = 150.0


def _user_text_seen(mock_llm_server_url: str, token: str) -> bool:
    """Return whether any captured model request carries *token* in user content."""
    for captured in get_mock_requests(mock_llm_server_url):
        for message in captured.get("messages") or []:
            if message.get("role") == "user" and token in json.dumps(message.get("content")):
                return True
    return False


def _send(page: Page, text: str) -> None:
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(text)
    composer.press("Enter")


@pytest.mark.nightly
@pytest.mark.timeout(600)
def test_follow_up_is_delivered_while_background_agent_runs(
    request: pytest.FixtureRequest,
    native_claude_mock_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """Claude takes a new prompt once the turn ends, so a follow-up must not wait for Steer."""
    base_url, session_id = native_claude_mock_session
    nonce = uuid.uuid4().hex[:8]
    launch_token = f"launch-{nonce}"
    launch_reply = f"launch-reply-{nonce}"
    worker_token = f"worker-{nonce}-background-task"
    # Longest token, because the follow-up request also carries the earlier prompts.
    follow_up_token = f"follow-up-{nonce}-sent-after-the-agent-launched"
    follow_up_reply = f"follow-up-reply-{nonce}"
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": f"worker-done-{nonce}", "delay": _SUB_AGENT_HOLD_S}],
        match=worker_token,
    )
    agent_arguments = {
        "description": "Background worker",
        "prompt": f"{worker_token}: do the work, then report back.",
        "subagent_type": "general-purpose",
        "run_in_background": True,
    }
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": f"toolu_{nonce}",
                        "name": "Agent",
                        "arguments": json.dumps(agent_arguments),
                    }
                ]
            },
            {"text": launch_reply},
        ],
        match=launch_token,
        required_tools=["Agent"],
    )
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": follow_up_reply}],
        match=follow_up_token,
        required_tools=["Bash"],
    )

    page = request.getfixturevalue("page")
    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _ensure_chat_view(page)

    _send(page, f"{launch_token} Start the background worker agent, then reply.")
    expect(page.locator(_ASSISTANT, has_text=launch_reply).first).to_be_visible(
        timeout=_FIRST_TURN_TIMEOUT_MS
    )
    assert _user_text_seen(mock_llm_server_url, worker_token), "background sub-agent never started"
    # Let the turn's Stop hook land so the follow-up is typed at Claude's idle prompt.
    time.sleep(5.0)

    _send(page, f"{follow_up_token} Reply to this follow-up.")
    expect(page.get_by_test_id("composer-queued-strip")).to_have_count(0, timeout=10_000)
    expect(page.locator(_ASSISTANT, has_text=follow_up_reply).first).to_be_visible(timeout=30_000)
