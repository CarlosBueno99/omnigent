"""Composer menus floating over a long transcript must not let its text show through.

Built-in dark palettes make ``bg-popover`` translucent, so the ``/`` and ``@`` menus need a
backdrop blur to hide the reply behind them; light mode (opaque popover) is the control.
The leak is the pixel difference inside the panel with the transcript text hidden vs. shown.
"""

from __future__ import annotations

import io
import json
import uuid
from typing import Any

from playwright.sync_api import Locator, Page, expect

from tests.e2e_ui.conftest import (
    _CUSTOM_AGENT_MODEL,
    configure_mock_llm,
    reset_mock_llm,
    set_fallback_mock_llm,
)
from tests.e2e_ui.messages.test_message_render_parity import _ensure_chat_view
from tests.e2e_ui.messages.test_native_codex_render_parity import (
    _open_terminal_view,
    _wait_terminal_connected,
)

_COMPOSER_LABEL = "Message the agent"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_WORKING = '[data-testid="working-indicator"]'
_SLASH_ROWS = "[data-testid^='slash-menu-item-']"
_MENTION_ROWS = "[data-testid^='file-mention-item-']"
_CODEX_MOCK_MODEL = "gpt-4o"
_TURN_TIMEOUT_MS = 120_000
_PARAGRAPHS = 40
# Glyphs leaking through an 80 %-opaque panel differ from the panel fill by about
# 0.2 x the glyph contrast (~40 grey levels); an opaque or blurred panel stays well below.
_MAX_LEAK_LEVELS = 16


def _long_reply(marker: str) -> str:
    return "\n\n".join(
        f"Paragraph {index}: {marker} bright transcript text keeps scrolling down to the composer."
        for index in range(1, _PARAGRAPHS + 1)
    )


def _choose_mode(page: Page, base_url: str, mode: str) -> None:
    page.goto(f"{base_url}/settings/appearance")
    group = page.get_by_role("radiogroup", name="Mode", exact=True)
    expect(group).to_be_visible(timeout=30_000)
    card = group.get_by_role("radio", name=mode)
    card.click()
    expect(card).to_have_attribute("aria-checked", "true")


def _composer(page: Page) -> Locator:
    composer = page.get_by_label(_COMPOSER_LABEL)
    expect(composer).to_be_visible(timeout=30_000)
    expect(composer).to_be_enabled(timeout=60_000)
    return composer


def _send_and_wait_for_long_reply(page: Page, marker: str) -> None:
    _composer(page).fill(f"Context {marker}. Reply with {_PARAGRAPHS} paragraphs.")
    page.get_by_role("button", name="Send", exact=True).click()
    expect(
        page.locator(_ASSISTANT, has_text=f"Paragraph {_PARAGRAPHS}: {marker}").first
    ).to_be_visible(timeout=_TURN_TIMEOUT_MS)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=_TURN_TIMEOUT_MS)


def _seed_workspace_files(page: Page, base_url: str, session_id: str) -> None:
    # The runner starts each native session on an empty isolated workspace, so
    # seed files through the product's write API for the "@" menu to list.
    fs = f"{base_url}/v1/sessions/{session_id}/resources/environments/default/filesystem"
    for name in ("README.md", "main.py", "config.yaml", "notes.txt", "src/app.ts"):
        page.request.put(
            f"{fs}/{name}",
            data=json.dumps({"content": f"// {name}\n"}),
            headers={"content-type": "application/json"},
        )


def _open_menu_panel(composer: Locator, page: Page, trigger: str, rows_selector: str) -> Locator:
    """Type *trigger* into the composer and return its menu panel once the rows have loaded."""
    composer.fill(trigger)
    rows = page.locator(rows_selector)
    expect(rows.first).to_be_visible(timeout=60_000)
    panel = page.locator("div.bg-popover", has=rows).last
    expect(panel.get_by_text("Loading")).to_have_count(0, timeout=30_000)
    return panel


def _set_transcript_text_visibility(page: Page, visibility: str) -> None:
    page.evaluate(
        "visibility => document.querySelectorAll('[data-testid=\"message-bubble\"] p')"
        ".forEach(el => { el.style.visibility = visibility; })",
        visibility,
    )


def _region_diff(
    reference: bytes, actual: bytes, box: tuple[int, int, int, int]
) -> dict[str, Any]:
    from PIL import Image, ImageChops

    before = Image.open(io.BytesIO(reference)).convert("L").crop(box)
    after = Image.open(io.BytesIO(actual)).convert("L").crop(box)
    histogram = ImageChops.difference(before, after).histogram()
    total = sum(histogram)
    return {
        "max_diff": max((level for level, count in enumerate(histogram) if count), default=0),
        "mean_diff": sum(level * count for level, count in enumerate(histogram)) / total,
        "fraction_over_8": (
            sum(count for level, count in enumerate(histogram) if level > 8) / total
        ),
        "pixels": total,
    }


def _measure_transcript_leak(page: Page, panel: Locator, label: str) -> dict[str, Any]:
    """Pixel difference inside *panel* between a hidden-text reference frame and the real frame."""
    style = panel.evaluate(
        "el => { const s = getComputedStyle(el); return {backgroundColor: s.backgroundColor,"
        " backdropFilter: s.backdropFilter || s.webkitBackdropFilter}; }"
    )
    box = panel.bounding_box()
    last_reply = page.locator(_ASSISTANT).last.bounding_box()
    assert box is not None and last_reply is not None
    overlaps_reply = box["y"] < last_reply["y"] + last_reply["height"] < box["y"] + box["height"]
    _set_transcript_text_visibility(page, "hidden")
    reference = page.screenshot()
    _set_transcript_text_visibility(page, "")
    actual = page.screenshot()
    region = (
        int(box["x"]) + 2,
        int(box["y"]) + 2,
        int(box["x"] + box["width"]) - 2,
        int(box["y"] + box["height"]) - 2,
    )
    return {
        "label": label,
        "dark_mode": page.evaluate("() => document.documentElement.classList.contains('dark')"),
        "panel_style": style,
        "panel_box": box,
        "last_reply_box": last_reply,
        "menu_overlaps_last_reply": overlaps_reply,
        **_region_diff(reference, actual, region),
    }


def test_slash_menu_hides_transcript_in_dark_mode(
    request: Any,
    custom_agent_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """Light mode is the opaque control; dark mode must not show the reply through ``/``."""
    base_url, session_id = custom_agent_session
    marker = uuid.uuid4().hex[:8]
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url, [{"text": _long_reply(marker)}], key=marker, match=marker
    )
    set_fallback_mock_llm(mock_llm_server_url, _CUSTOM_AGENT_MODEL, "")

    page = request.getfixturevalue("page")
    page.emulate_media(color_scheme="light")
    page.goto(f"{base_url}/c/{session_id}")
    _send_and_wait_for_long_reply(page, marker)

    composer = _composer(page)
    panel = _open_menu_panel(composer, page, "/", _SLASH_ROWS)
    light = _measure_transcript_leak(page, panel, "light-slash")
    assert light["menu_overlaps_last_reply"], light
    assert light["max_diff"] <= _MAX_LEAK_LEVELS, f"light-mode control leaks: {light}"
    composer.fill("")

    _choose_mode(page, base_url, "Dark")
    page.goto(f"{base_url}/c/{session_id}")
    composer = _composer(page)
    expect(page.locator(_ASSISTANT).last).to_be_visible(timeout=30_000)
    panel = _open_menu_panel(composer, page, "/", _SLASH_ROWS)
    expect(panel.get_by_text("Skills", exact=True)).to_be_visible()
    dark = _measure_transcript_leak(page, panel, "dark-slash")
    assert dark["dark_mode"] and dark["menu_overlaps_last_reply"], dark
    assert dark["max_diff"] <= _MAX_LEAK_LEVELS, (
        f"transcript text shows through the slash menu: {dark}"
    )


def test_file_mention_menu_hides_transcript_in_dark_mode(
    request: Any,
    native_codex_mock_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """A Codex-native session's ``@`` menu must not show the reply through in dark mode."""
    base_url, session_id = native_codex_mock_session
    marker = uuid.uuid4().hex[:8]
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url, [{"text": _long_reply(marker)}], key=marker, match=marker
    )
    set_fallback_mock_llm(mock_llm_server_url, _CODEX_MOCK_MODEL, "")

    page = request.getfixturevalue("page")
    page.emulate_media(color_scheme="light")
    _choose_mode(page, base_url, "Dark")
    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _ensure_chat_view(page)
    _seed_workspace_files(page, base_url, session_id)
    _send_and_wait_for_long_reply(page, marker)

    panel = _open_menu_panel(_composer(page), page, "@", _MENTION_ROWS)
    dark = _measure_transcript_leak(page, panel, "dark-mention")
    assert dark["dark_mode"] and dark["menu_overlaps_last_reply"], dark
    assert dark["max_diff"] <= _MAX_LEAK_LEVELS, (
        f"transcript text shows through the @ menu: {dark}"
    )
