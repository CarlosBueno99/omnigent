"""Codex Plan mode turned on from the web composer must reach the model as Codex Plan Mode.

A real Codex CLI runs against the mock model backend, so the request journal shows
the collaboration-mode instructions Codex sent for the turn after Plan mode is on.
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import configure_mock_llm, reset_mock_llm, set_fallback_mock_llm
from tests.e2e_ui.messages.test_message_render_parity import (
    _ASSISTANT,
    _WORKING,
    _ensure_chat_view,
)
from tests.e2e_ui.messages.test_native_codex_render_parity import (
    _CODEX_MOCK_MODEL,
    _MOCK_TURN_TIMEOUT_MS,
    _open_terminal_view,
    _wait_terminal_connected,
)


def _run_turn(page: Page, mock_url: str, prompt_marker: str) -> list[dict]:
    """Send one composer turn and return the model requests that carried it."""
    reply = f"reply-{prompt_marker}"
    reset_mock_llm(mock_url)
    configure_mock_llm(mock_url, [{"text": reply}], key=prompt_marker, match=prompt_marker)
    set_fallback_mock_llm(mock_url, _CODEX_MOCK_MODEL, "")
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_editable(timeout=30_000)
    composer.fill(f"Add a hello.txt file with a greeting. Marker {prompt_marker}.")
    page.get_by_role("button", name="Send", exact=True).click()
    expect(page.locator(_ASSISTANT, has_text=reply).first).to_be_visible(
        timeout=_MOCK_TURN_TIMEOUT_MS
    )
    expect(page.locator(_WORKING)).to_have_count(0, timeout=_MOCK_TURN_TIMEOUT_MS)
    captured = httpx.get(f"{mock_url}/mock/requests", timeout=10.0).json()["requests"]
    return [request for request in captured if prompt_marker in json.dumps(request)]


def _collaboration_mode_messages(request: dict) -> list[str]:
    """Return the developer messages Codex wrapped in ``<collaboration_mode>``."""
    messages = []
    for item in request.get("input") or []:
        if item.get("role") != "developer":
            continue
        content = item.get("content")
        parts = content if isinstance(content, list) else [{"text": str(content)}]
        text = "\n".join(str(part.get("text", "")) for part in parts if isinstance(part, dict))
        if "<collaboration_mode>" in text:
            messages.append(text)
    return messages


@pytest.mark.nightly
@pytest.mark.timeout(360)
def test_codex_plan_mode_toggle_sends_plan_mode_instructions(
    request: pytest.FixtureRequest,
    native_codex_mock_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    base_url, session_id = native_codex_mock_session
    page: Page = request.getfixturevalue("page")
    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _ensure_chat_view(page)
    _run_turn(page, mock_llm_server_url, f"first-{uuid.uuid4().hex[:8]}")

    page.locator("[data-composer-card]").get_by_role(
        "button", name="Add", exact=True
    ).first.click()
    plan_action = page.get_by_test_id("composer-add-menu").get_by_test_id("composer-plan-action")
    expect(plan_action).to_have_attribute("aria-label", "Enter Plan mode", timeout=30_000)
    plan_action.click()
    expect(page.get_by_test_id("composer-plan-mode")).to_contain_text("Plan mode", timeout=30_000)

    plan_requests = _run_turn(page, mock_llm_server_url, f"plan-{uuid.uuid4().hex[:8]}")
    assert plan_requests, "no model request carried the Plan-mode turn"
    collaboration_messages = _collaboration_mode_messages(plan_requests[0])
    assert any("Plan Mode" in message for message in collaboration_messages), (
        f"Plan-mode turn reached the model without Codex Plan Mode instructions: "
        f"{collaboration_messages!r}"
    )
    developer_text = json.dumps(
        [item for item in plan_requests[0].get("input") or [] if item.get("role") == "developer"]
    )
    # The wrapper agent's prompt, which Codex reads from its session config.toml.
    assert "Codex is running in the session terminal" in developer_text, (
        "Plan-mode turn dropped the agent's own instructions"
    )
