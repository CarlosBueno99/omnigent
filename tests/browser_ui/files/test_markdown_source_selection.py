"""Markdown Source-view selection painting in real Chromium with a sealed mock backend."""

from __future__ import annotations

import io
import re
from pathlib import Path

import pytest
from PIL import Image
from playwright.sync_api import Page, Route, expect

from tests.browser_ui.files.test_file_line_navigation import (
    BrowserSession,
    _mock_markdown_files,
    _open_viewer,
)
from tests.browser_ui.files.test_file_line_navigation import (
    seeded_session as seeded_session_fixture,
)

seeded_session = seeded_session_fixture
pytestmark = pytest.mark.browser_context_args(device_scale_factor=2, color_scheme="light")

_PATH = "src/rollback_plan.md"
_HEADING = "### Rollback SOP"
_CONTENT = (
    f"# Rollout plan\n\n{_HEADING}\n\n"
    "Expected flag rollback is **under 10 minutes**, plus normal pod-roll time.\n"
)
# Merges `###` into one glyph that keeps a single cell's advance but draws its ink
# in the two cells to the left; see fixtures/build_hash_ligature_font.py.
_LIGATURE_FONT = Path(__file__).with_name("fixtures") / "geist-mono-latin-hash-ligature.woff2"
_BUNDLED_MONO_FONT = re.compile(r".*/geist-mono-latin-wght-normal-[^/]*\.woff2$")


def _is_dark(pixel: tuple[int, int, int]) -> bool:
    red, green, blue = pixel
    return 0.299 * red + 0.587 * green + 0.114 * blue < 140


def _is_wash(pixel: tuple[int, int, int]) -> bool:
    # The light ::selection wash is about (252, 234, 244); anti-aliased ink on white
    # misses this band, so halos around clipped glyphs are not counted as wash.
    red, green, blue = pixel
    return (
        red >= 243 and blue >= 232 and 222 <= green <= 242 and red - green >= 10
    ) and red >= blue >= green


@pytest.mark.parametrize("font_mode", ["bundled", "ligature"])
def test_source_selection_wash_covers_heading_markers(
    page: Page, seeded_session: BrowserSession, font_mode: str
) -> None:
    """Dragging over a heading paints the selection behind its leading markers."""
    served_fonts: list[str] = []
    if font_mode == "ligature":

        def serve_ligature_font(route: Route) -> None:
            served_fonts.append(route.request.url)
            route.fulfill(body=_LIGATURE_FONT.read_bytes(), content_type="font/woff2")

        seeded_session.contract.route(_BUNDLED_MONO_FONT, serve_ligature_font)
    _mock_markdown_files(page, seeded_session, {_PATH: _CONTENT})
    viewer = _open_viewer(
        page, seeded_session, _PATH, {"diffActive": False, "previewableViewMode": "source"}
    )
    heading = viewer.locator('[data-line="3"]')
    paragraph = viewer.locator('[data-line="5"]')
    expect(heading).to_have_text(_HEADING, timeout=30_000)
    assert page.evaluate("document.fonts.ready.then(() => document.fonts.status)") == "loaded"
    assert served_fonts or font_mode == "bundled", "the ligature font was never served"

    start = heading.locator("span").first.bounding_box()
    end = paragraph.locator("span").last.bounding_box()
    assert start and end
    page.mouse.move(start["x"] + 1, start["y"] + start["height"] / 2)
    page.mouse.down()
    page.mouse.move(end["x"] + end["width"] - 1, end["y"] + end["height"] / 2, steps=12)
    page.mouse.up()
    page.wait_for_function(
        "heading => window.getSelection().toString().startsWith(heading)", arg=_HEADING
    )

    row = Image.open(io.BytesIO(heading.screenshot())).convert("RGB")
    pixels = row.load()
    columns = range(row.width)
    rows = range(row.height)
    # A wash column spans most of the line height; an ink halo covers only a few pixels.
    wash = [x for x in columns if sum(_is_wash(pixels[x, y]) for y in rows) >= 6]
    ink = [x for x in columns if sum(_is_dark(pixels[x, y]) for y in rows) >= 3]
    assert wash, "no selection wash painted on the heading row"
    assert ink, "no glyph ink found on the heading row"
    assert min(wash) <= min(ink) + 2, (
        f"selection wash starts at x={min(wash)} but heading ink starts at x={min(ink)} "
        "(device px): the highlight misses the leading markers"
    )
