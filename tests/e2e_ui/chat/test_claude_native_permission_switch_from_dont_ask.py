"""E2E: the composer permission picker switches a claude-native session out of Don't ask.

A claude-native session launched with ``--permission-mode dontAsk`` renders Claude
Code's ``⏵⏵ don't ask on (shift+tab to cycle)`` footer and the composer chip reads
"Don't ask". Choosing a switchable mode from that chip must apply it: the chip
settles on the new mode and the terminal footer changes. Don't ask itself stays
launch-only — the runner only needs to read it as the cycle's starting point.

The footer is read through the product's read-only terminal-attach WebSocket
(rendered with ``pyte``): the runner's tmux socket is not reachable from here.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from collections.abc import Iterator

import httpx
import pyte
import pytest
import websockets
from playwright.sync_api import Page, expect

from tests._helpers.async_thread import run_in_fresh_loop
from tests.e2e_ui.conftest import (
    _create_native_claude_session,
    _ensure_runner_online,
    _server_state,
    _temp_omnigent_mock_config,
)

_TARGET_MODE = "acceptEdits"
_TARGET_LABEL = "Accept edits"
_DONT_ASK_FOOTER = "don't ask on"
_TARGET_FOOTER = "accept edits on"
# Claude CLI boot + first footer paint.
_FOOTER_TIMEOUT_S = 120.0
# The runner's own budget is ~3 s (tmux advert wait + footer settle), so a
# switch that takes longer than this has failed rather than slowed.
_SWITCH_TIMEOUT_MS = 20_000
_PANE_COLS, _PANE_ROWS = 120, 40


@pytest.fixture
def native_claude_dont_ask_session(
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str]]:
    """A real Claude CLI launched into ``dontAsk`` against the mock provider."""
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])
    with _temp_omnigent_mock_config(
        mock_llm_server_url, "claude", workflow_owned=bool(_server_state.get("workflow_owned"))
    ):
        session_id = _create_native_claude_session(
            live_server, runner_id, terminal_launch_args=["--permission-mode", "dontAsk"]
        )
        try:
            yield (live_server, session_id)
        finally:
            httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
            if respawned is not None:
                respawned.terminate()
                try:
                    respawned.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    respawned.kill()
                    respawned.wait(timeout=5)


def _terminal_id(base_url: str, session_id: str) -> str | None:
    """Return the session's Claude terminal resource id once the runner has launched it."""
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}/resources", timeout=10.0)
    resp.raise_for_status()
    for resource in resp.json().get("data", []):
        if resource.get("type") == "terminal":
            return str(resource["id"])
    return None


def _render_terminal(
    base_url: str, session_id: str, terminal_id: str, settle_s: float = 3.0
) -> str:
    """Attach read-only to the terminal and return the screen text it renders."""
    ws_url = (
        base_url.replace("http://", "ws://", 1).replace("https://", "wss://", 1)
        + f"/v1/sessions/{session_id}/resources/terminals/{terminal_id}/attach?read_only=true"
    )
    screen = pyte.Screen(_PANE_COLS, _PANE_ROWS)
    stream = pyte.ByteStream(screen)

    async def _collect() -> None:
        async with websockets.connect(ws_url, max_size=None, open_timeout=20) as ws:
            await ws.send(json.dumps({"type": "resize", "cols": _PANE_COLS, "rows": _PANE_ROWS}))
            deadline = time.monotonic() + settle_s
            while (remaining := deadline - time.monotonic()) > 0:
                try:
                    frame = await asyncio.wait_for(ws.recv(), timeout=remaining)
                except TimeoutError:
                    break
                if isinstance(frame, bytes):
                    stream.feed(frame)

    run_in_fresh_loop(_collect())
    return "\n".join(screen.display)


def _wait_for_footer(base_url: str, session_id: str, footer: str, timeout_s: float) -> str:
    """Poll the rendered terminal until its mode footer contains *footer*; return the screen."""
    deadline = time.monotonic() + timeout_s
    screen = ""
    while time.monotonic() < deadline:
        terminal_id = _terminal_id(base_url, session_id)
        if terminal_id is not None:
            screen = _render_terminal(base_url, session_id, terminal_id)
            if footer in screen.lower():
                return screen
        time.sleep(1.0)
    raise AssertionError(f"terminal never rendered {footer!r}; last screen:\n{screen}")


@pytest.mark.nightly
@pytest.mark.timeout(360)
def test_permission_picker_switches_out_of_dont_ask(
    request: pytest.FixtureRequest,
    native_claude_dont_ask_session: tuple[str, str],
) -> None:
    """Picking Accept edits on a don't-ask session applies it in Claude's terminal.

    Control first: the real TUI booted into don't-ask and painted the footer the
    runner must read. The page is requested only afterwards so a recording
    starts at the user's first action.
    """
    base_url, session_id = native_claude_dont_ask_session
    _wait_for_footer(base_url, session_id, _DONT_ASK_FOOTER, timeout_s=_FOOTER_TIMEOUT_S)

    page: Page = request.getfixturevalue("page")
    page.goto(f"{base_url}/c/{session_id}")
    chip = page.get_by_test_id("composer-permission-chip")
    expect(chip).to_be_visible(timeout=60_000)
    expect(chip).to_have_text("Don't ask")

    chip.click()
    expect(page.get_by_test_id("composer-permission-menu")).to_be_visible()
    with page.expect_response(
        lambda r: r.request.method == "PATCH" and r.url.endswith(f"/v1/sessions/{session_id}"),
        timeout=_SWITCH_TIMEOUT_MS,
    ) as switched:
        page.get_by_test_id(f"composer-permission-option-{_TARGET_MODE}").click()
    response = switched.value

    error_row = page.get_by_text("Could not switch", exact=False)
    if response.status != 200:
        # Let the composer render the rejection so a recording ends on it.
        expect(error_row.first).to_be_visible(timeout=10_000)
    assert response.status == 200, f"mode switch rejected: {response.text()}"
    expect(chip).to_have_text(_TARGET_LABEL)
    expect(error_row).to_have_count(0)
    _wait_for_footer(base_url, session_id, _TARGET_FOOTER, timeout_s=10.0)
