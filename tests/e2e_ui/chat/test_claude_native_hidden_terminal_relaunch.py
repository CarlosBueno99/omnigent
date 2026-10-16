"""E2E: a claude-native message after a pane relaunch survives a display:none terminal view.

The stock chat view (``visibility:hidden``) is the control. Closing the pane through the
terminal resource API is the same registry close the idle reaper performs.
"""

from __future__ import annotations

import json
import logging
import time

import httpx
import pytest
from playwright.sync_api import Page, WebSocket, expect

from tests.e2e_ui.conftest import set_fallback_mock_llm
from tests.e2e_ui.messages.test_message_render_parity import _ensure_chat_view, _send

_log = logging.getLogger(__name__)

_CLAUDE_MOCK_MODEL = "claude-sonnet-4-20250514"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_ERROR_PILL = '[data-testid="error-pill"]'
_HIDE_TERMINAL_VIEW_CSS = '[data-testid="main-terminal-view"] { display: none !important; }'

_FIRST_TOKEN = "FIRST_TURN_ANSWERED"
_RELAUNCH_TOKEN = "RELAUNCH_TURN_ANSWERED"

# Claude boots in the pane before the first prompt can land.
_FIRST_TURN_TIMEOUT_MS = 180_000
_PANE_CLOSE_TIMEOUT_S = 30.0
# 30 s pane re-create wait + 180 s readiness cap + attach latency.
_RELAUNCH_OUTCOME_TIMEOUT_MS = 330_000


def _running_terminal_ids(base_url: str, session_id: str) -> list[str]:
    response = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/resources/terminals",
        params={"limit": 100},
        timeout=10.0,
    )
    response.raise_for_status()
    return [
        str(item.get("id"))
        for item in response.json().get("data", [])
        if (item.get("metadata") or {}).get("running") is not False
    ]


def _close_running_terminals(page: Page, base_url: str, session_id: str) -> None:
    """Close the session's native pane through the resource API and wait for it to go."""
    for terminal_id in _running_terminal_ids(base_url, session_id):
        httpx.delete(
            f"{base_url}/v1/sessions/{session_id}/resources/terminals/{terminal_id}",
            timeout=30.0,
        ).raise_for_status()
    started = time.monotonic()
    while time.monotonic() - started < _PANE_CLOSE_TIMEOUT_S:
        if not _running_terminal_ids(base_url, session_id):
            return
        page.wait_for_timeout(1_000)
    raise AssertionError(
        f"the Claude pane was still listed {_PANE_CLOSE_TIMEOUT_S:.0f}s after closing it"
    )


def _record_terminal_frames(page: Page) -> list[dict[str, object]]:
    """Collect the text frames the browser sends on terminal attach sockets."""
    frames: list[dict[str, object]] = []

    def on_websocket(ws: WebSocket) -> None:
        if "terminal" not in ws.url or "attach" not in ws.url:
            return

        def on_sent(payload: str | bytes) -> None:
            if isinstance(payload, str):
                frames.append({"t": round(time.monotonic(), 3), "url": ws.url, "sent": payload})

        ws.on("framesent", on_sent)

    page.on("websocket", on_websocket)
    return frames


def _expanded_error_pill_texts(page: Page) -> list[str]:
    pills = page.locator(_ERROR_PILL)
    texts: list[str] = []
    for index in range(pills.count()):
        pill = pills.nth(index)
        if pill.get_by_test_id("error-disclosure-icon").count():
            pill.click()
        texts.append(pill.inner_text())
    return texts


@pytest.mark.timeout(900)
@pytest.mark.parametrize(
    "hide_terminal_view",
    [True, False],
    ids=["terminal-view-display-none", "stock-chat-view"],
)
def test_message_after_pane_relaunch_is_delivered(
    request: pytest.FixtureRequest,
    native_claude_mock_session: tuple[str, str],
    mock_llm_server_url: str,
    hide_terminal_view: bool,
) -> None:
    base_url, session_id = native_claude_mock_session
    _log.info("native-claude session %s on %s", session_id, base_url)
    set_fallback_mock_llm(mock_llm_server_url, "default", _FIRST_TOKEN)
    set_fallback_mock_llm(mock_llm_server_url, _CLAUDE_MOCK_MODEL, _FIRST_TOKEN)

    page: Page = request.getfixturevalue("page")
    _log.info("browser page opened")
    frames = _record_terminal_frames(page)
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_test_id("view-mode-toggle")).to_be_visible(timeout=_FIRST_TURN_TIMEOUT_MS)
    _ensure_chat_view(page)
    _send(page, f"Reply with exactly this token and nothing else: {_FIRST_TOKEN}")
    expect(page.locator(_ASSISTANT, has_text=_FIRST_TOKEN).first).to_be_visible(
        timeout=_FIRST_TURN_TIMEOUT_MS
    )
    _log.info("first turn answered; terminal frames so far: %s", json.dumps(frames))

    if hide_terminal_view:
        page.add_style_tag(content=_HIDE_TERMINAL_VIEW_CSS)
        display = page.evaluate(
            "getComputedStyle(document.querySelector('[data-testid=\"main-terminal-view\"]')).display"
        )
        assert display == "none", display

    _close_running_terminals(page, base_url, session_id)
    _log.info("Claude pane closed")
    frames_before_relaunch = len(frames)

    set_fallback_mock_llm(mock_llm_server_url, "default", _RELAUNCH_TOKEN)
    set_fallback_mock_llm(mock_llm_server_url, _CLAUDE_MOCK_MODEL, _RELAUNCH_TOKEN)
    _send(
        page,
        f"After the relaunch, reply with exactly this token and nothing else: {_RELAUNCH_TOKEN}",
    )
    sent_at = time.monotonic()
    outcome = page.locator(_ERROR_PILL).or_(page.locator(_ASSISTANT, has_text=_RELAUNCH_TOKEN))
    expect(outcome.first).to_be_visible(timeout=_RELAUNCH_OUTCOME_TIMEOUT_MS)
    elapsed = time.monotonic() - sent_at

    pill_texts = _expanded_error_pill_texts(page)
    relaunch_frames = frames[frames_before_relaunch:]
    _log.info(
        "relaunch outcome after %.0fs; terminal frames after relaunch: %s; error pills: %s",
        elapsed,
        json.dumps(relaunch_frames),
        json.dumps(pill_texts),
    )
    resize_frames = [
        frame["sent"] for frame in relaunch_frames if '"resize"' in str(frame["sent"])
    ]
    assert not any("did not become ready" in text for text in pill_texts), (
        f"the relaunched Claude pane never became ready ({elapsed:.0f}s after Send); "
        f"browser resize frames after the relaunch: {resize_frames}; error: {pill_texts}"
    )
    expect(page.locator(_ASSISTANT, has_text=_RELAUNCH_TOKEN).first).to_be_visible(timeout=30_000)
