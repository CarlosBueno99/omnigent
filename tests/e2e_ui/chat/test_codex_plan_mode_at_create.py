"""A Codex session created in Plan mode must send its first turn as Codex Plan Mode."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from playwright.sync_api import Page, expect

from omnigent.stores.conversation_store import CODEX_NATIVE_COLLABORATION_MODE_LABEL_KEY
from tests.e2e_ui.chat.test_codex_plan_mode_instructions import (
    _collaboration_mode_messages,
    _run_turn,
)
from tests.e2e_ui.conftest import native_codex_mock_session_context
from tests.e2e_ui.messages.test_message_render_parity import _ensure_chat_view
from tests.e2e_ui.messages.test_native_codex_render_parity import (
    _open_terminal_view,
    _wait_terminal_connected,
)


@pytest.fixture
def plan_mode_codex_session(
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str]]:
    """A real Codex CLI session carrying the Plan label the New Session screen sets."""
    with native_codex_mock_session_context(
        live_server,
        mock_llm_server_url,
        tmp_path_factory,
        labels={CODEX_NATIVE_COLLABORATION_MODE_LABEL_KEY: "plan"},
    ) as session:
        yield session


@pytest.mark.nightly
@pytest.mark.timeout(360)
def test_codex_session_created_in_plan_mode_sends_plan_mode_on_first_turn(
    request: pytest.FixtureRequest,
    plan_mode_codex_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    base_url, session_id = plan_mode_codex_session
    page: Page = request.getfixturevalue("page")
    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _ensure_chat_view(page)
    expect(page.get_by_test_id("composer-plan-mode")).to_contain_text("Plan mode", timeout=30_000)

    first_requests = _run_turn(page, mock_llm_server_url, f"first-{uuid.uuid4().hex[:8]}")
    assert first_requests, "no model request carried the first turn"
    collaboration_messages = _collaboration_mode_messages(first_requests[0])
    assert any("Plan Mode" in message for message in collaboration_messages), (
        f"First turn of a Plan-mode session reached the model without Codex Plan Mode "
        f"instructions: {collaboration_messages!r}"
    )
