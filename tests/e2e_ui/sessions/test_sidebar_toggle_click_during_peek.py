"""E2E: clicking the sidebar toggle must open the sidebar even mid hover-peek.

The collapsed-sidebar "Open sidebar" toggle (chat header,
top-left) arms a 400ms dwell-to-peek timer on hover. Once the timer fires, the
peek card mounts as a floating overlay (``aside.is-peek``, z-50) that fades and
slides in over 200ms. A user who hovers the toggle and clicks — before, during,
or after that fade-in — must reach the toggle, not sidebar content in the card
(the brand link navigates to ``/``). Otherwise the sidebar never pins open, and
once the pointer moves away the peek dismisses and the sidebar collapses again.

The tests drive that journey with real pointer events at several click times
and assert the user-level contract: a click on the toggle's location pins the
sidebar open and keeps the user on their session.

Pure client-side interaction on the seeded session page — no LLM turn.
"""

from __future__ import annotations

from playwright.sync_api import Page, expect

_CONVERSATIONS = 'aside[aria-label="Conversations"]'
_PEEK_CARD = "aside.conversations-sidebar.is-peek"
# Park spot well away from the sidebar, the header, and the peek card.
_PARK = (900, 500)
# The dwell-to-peek delay in SidebarOpenButton.tsx (onPeekSidebar) is 400ms; the peek
# card then fades/slides in over 200ms. Clicking ~60ms after the timer fires
# lands squarely in the "peek mounted but not yet visible" window the report
# describes as "before any preview shows up".
_JUST_PAST_PEEK_DELAY_MS = 460
_WELL_BEFORE_PEEK_DELAY_MS = 120
# A human press lasts ~100-200ms, long enough to span the end of the fade-in.
_HUMAN_PRESS_MS = 150
# By 600ms the card's entry animation has ended and it takes pointer events.
_AFTER_PEEK_VISIBLE_MS = 800


def _collapse_sidebar(page: Page) -> None:
    """Collapse the sidebar the way a user does, then step the pointer away.

    Clicking "Close sidebar" leaves the pointer roughly where the header's
    "Open sidebar" toggle appears, which would arm the peek timer by itself —
    parking the pointer (pointerleave cancels the pending peek) and waiting
    out the timer window gives every test a clean, peek-free starting state.
    """
    conversations = page.locator(_CONVERSATIONS)
    if conversations.get_attribute("data-collapsed") != "true":
        page.get_by_role("button", name="Close sidebar").click()
    page.mouse.move(*_PARK, steps=2)
    expect(conversations).to_have_attribute("data-collapsed", "true")
    page.wait_for_timeout(700)


def _hover_toggle_then_click(page: Page, dwell_ms: int, press_ms: int = 0) -> bool:
    """Hover the "Open sidebar" toggle for ``dwell_ms``, then click its spot.

    Uses raw mouse events at the toggle's recorded coordinates — exactly what
    a user's pointer does — so an overlay covering the button intercepts the
    click just like it would in real use (locator ``click()`` would instead
    wait for the button to become the hit target and mask the bug).

    :returns: Whether the peek card was already mounted when the click landed
        (diagnostic context for the assertion message).
    """
    toggle = page.get_by_role("button", name="Open sidebar")
    expect(toggle).to_be_visible()
    box = toggle.bounding_box()
    assert box is not None
    cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2

    page.mouse.move(cx, cy, steps=2)  # pointerenter arms the 400ms peek timer
    page.wait_for_timeout(dwell_ms)
    peek_mounted = page.locator(_PEEK_CARD).count() > 0
    page.mouse.down()
    if press_ms:
        page.wait_for_timeout(press_ms)
    page.mouse.up()
    return peek_mounted


def _assert_sidebar_pinned_open(page: Page, session_id: str, context: str) -> None:
    """After the pointer walks away, the sidebar must be open — not a peek.

    Parks the pointer and waits out the peek-dismiss grace (200ms) plus the
    close transition, so a transient peek card can't impersonate an open
    sidebar: only a genuinely pinned-open sidebar keeps ``data-collapsed``
    off once the pointer is elsewhere.
    """
    page.mouse.move(*_PARK, steps=2)
    page.wait_for_timeout(900)
    expect(page.locator(_PEEK_CARD), f"peek card must not linger ({context})").to_have_count(0)
    expect(
        page.locator(_CONVERSATIONS),
        f"sidebar must be pinned open after clicking the toggle ({context})",
    ).not_to_have_attribute("data-collapsed", "true")
    assert f"/c/{session_id}" in page.url, (
        f"clicking the sidebar toggle must not navigate away ({context}); "
        f"the click fell through to the sidebar brand link — now at {page.url}"
    )


def test_click_during_invisible_peek_still_opens_sidebar(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A click landing just after the peek timer fires must still open the sidebar.

    Journey: collapse the sidebar → hover the top-left "Open
    sidebar" toggle → click before the hover-preview becomes visible → the
    sidebar must pin open on the same session. Today the click is swallowed
    by the invisible peek card and the sidebar stays collapsed (worse, the
    click can hit the brand link and kick the user to ``/``).
    """
    base_url, session_id = seeded_session
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_role("complementary", name="Workspace")).to_be_visible(timeout=30_000)

    _collapse_sidebar(page)
    peek_mounted = _hover_toggle_then_click(page, _JUST_PAST_PEEK_DELAY_MS)
    _assert_sidebar_pinned_open(
        page,
        session_id,
        f"clicked {_JUST_PAST_PEEK_DELAY_MS}ms after hover; "
        f"peek card mounted at click time: {peek_mounted}",
    )


def test_click_before_peek_delay_opens_sidebar(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A genuinely fast click — before the 400ms peek timer fires — keeps working.

    This path works today (pointerdown cancels the pending peek); it pins the
    contract so a fix for the invisible-peek window can't regress the plain
    hover-and-click journey.
    """
    base_url, session_id = seeded_session
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_role("complementary", name="Workspace")).to_be_visible(timeout=30_000)

    _collapse_sidebar(page)
    _hover_toggle_then_click(page, _WELL_BEFORE_PEEK_DELAY_MS)
    _assert_sidebar_pinned_open(
        page,
        session_id,
        f"clicked {_WELL_BEFORE_PEEK_DELAY_MS}ms after hover",
    )


def test_press_spanning_peek_fade_in_still_opens_sidebar(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A human-length press that starts before the preview is visible must open the sidebar."""
    base_url, session_id = seeded_session
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_role("complementary", name="Workspace")).to_be_visible(timeout=30_000)

    _collapse_sidebar(page)
    peek_mounted = _hover_toggle_then_click(
        page, _JUST_PAST_PEEK_DELAY_MS, press_ms=_HUMAN_PRESS_MS
    )
    _assert_sidebar_pinned_open(
        page,
        session_id,
        f"pressed {_JUST_PAST_PEEK_DELAY_MS}ms after hover for {_HUMAN_PRESS_MS}ms; "
        f"peek card mounted at press time: {peek_mounted}",
    )


def test_click_after_peek_card_appears_opens_sidebar(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Clicking the toggle's spot once the preview is visible must still open the sidebar."""
    base_url, session_id = seeded_session
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_role("complementary", name="Workspace")).to_be_visible(timeout=30_000)

    _collapse_sidebar(page)
    peek_mounted = _hover_toggle_then_click(page, _AFTER_PEEK_VISIBLE_MS)
    _assert_sidebar_pinned_open(
        page,
        session_id,
        f"clicked {_AFTER_PEEK_VISIBLE_MS}ms after hover; "
        f"peek card mounted at click time: {peek_mounted}",
    )
