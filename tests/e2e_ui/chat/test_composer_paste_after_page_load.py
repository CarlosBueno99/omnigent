"""E2E: pasting right after a session page loads lands in the composer."""

from __future__ import annotations

from playwright.sync_api import Page, expect

_PASTED_TEXT = "pasted straight after opening the session"

_RECORD_COMPOSER_MOUNT = """
new MutationObserver((_, observer) => {
  const composer = document.querySelector('textarea[aria-label="Message the agent"]');
  if (!composer) return;
  window.__composerMountedDisabled = composer.disabled;
  observer.disconnect();
}).observe(document, { childList: true, subtree: true });
"""


def test_first_paste_after_page_load_lands_in_composer(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """Ctrl/Cmd+V without clicking the composer first inserts the clipboard text."""
    base_url, session_id = seeded_session
    page.context.grant_permissions(["clipboard-read", "clipboard-write"], origin=base_url)
    page.add_init_script(_RECORD_COMPOSER_MOUNT)

    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_enabled(timeout=30_000)
    # A full page load mounts the composer before the page picks the session's agent.
    assert page.evaluate("window.__composerMountedDisabled") is True
    page.evaluate("text => navigator.clipboard.writeText(text)", _PASTED_TEXT)

    expect(composer).to_be_focused()
    page.keyboard.press("ControlOrMeta+V")
    expect(composer).to_have_value(_PASTED_TEXT)
