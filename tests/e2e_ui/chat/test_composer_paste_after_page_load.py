"""E2E: pasting right after a session page loads lands in the composer."""

from __future__ import annotations

from playwright.sync_api import Page, expect

_PASTED_TEXT = "pasted straight after opening the session"


def test_first_paste_after_page_load_lands_in_composer(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """Ctrl/Cmd+V without clicking the composer first inserts the clipboard text."""
    base_url, session_id = seeded_session
    page.context.grant_permissions(["clipboard-read", "clipboard-write"], origin=base_url)

    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_enabled(timeout=30_000)
    page.evaluate("text => navigator.clipboard.writeText(text)", _PASTED_TEXT)

    expect(composer).to_be_focused()
    page.keyboard.press("ControlOrMeta+V")
    expect(composer).to_have_value(_PASTED_TEXT)
