"""A Claude-native session started with ``-p`` must submit that prompt only once.

``omni claude --server <url> -p "<prompt>"`` persists the prompt as the leading
``terminal_launch_args`` entry so the runner's first launch submits it. The
runner re-applies the stored args on every later pane launch, including the
cold-resume relaunch after the pane was closed and the user reconnected with
**Resume session**; that relaunch must not submit the prompt again.
"""

from __future__ import annotations

import os
import shutil
import time
import uuid
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    _CLAUDE_MOCK_MODEL,
    _create_native_claude_session,
    _ensure_runner_online,
    _server_state,
    _temp_omnigent_mock_config,
    reset_mock_llm,
    set_fallback_mock_llm,
)
from tests.e2e_ui.messages.test_message_render_parity import (
    _item_text,
    _ordered_message_items,
    _select_view_mode,
)

_TERMINAL_VIEW = '[data-testid="terminal-view"]'
_USER_BUBBLE = '[data-testid="message-bubble"][data-role="user"]'
# The idle reaper closes an idle native pane through the same terminal close the
# resources API exposes. A short window (seconds) drives the reported "after a
# while" journey; without one the test closes the pane directly.
_IDLE_TIMEOUT_ENV = "OMNIGENT_NATIVE_PANE_IDLE_TIMEOUT_S"
_TERMINAL_READY_TIMEOUT_MS = 120_000
_FIRST_TURN_TIMEOUT_S = 180
_PANE_CLOSE_TIMEOUT_S = 60
# Idle window + one reaper scan + the pane-output busy window, with margin.
_REAP_TIMEOUT_S = 420
# A relaunched pane that replays the prompt does so within seconds of
# reconnecting; this is how long it gets before the relaunch counts as clean.
_RESUBMIT_WATCH_S = 60
_SHOTS_DIR = os.environ.get("OMNIGENT_E2E_SHOTS_DIR")


def _terminals(base_url: str, session_id: str) -> list[dict[str, object]]:
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}/resources/terminals", timeout=15.0)
    resp.raise_for_status()
    return list(resp.json().get("data", []))


def _user_items_with(base_url: str, session_id: str, marker: str) -> list[str]:
    return [
        _item_text(item)
        for item in _ordered_message_items(base_url, session_id)
        if item.get("role") == "user" and marker in _item_text(item)
    ]


def _assistant_items(base_url: str, session_id: str) -> int:
    items = _ordered_message_items(base_url, session_id)
    return sum(1 for item in items if item.get("role") == "assistant")


def _mock_requests_with(mock_url: str, marker: str) -> int:
    resp = httpx.get(f"{mock_url}/mock/requests", timeout=15.0)
    resp.raise_for_status()
    count = 0
    for body in resp.json().get("requests", []):
        messages = body.get("messages") if isinstance(body, dict) else None
        if not isinstance(messages, list) or not messages:
            continue
        last = messages[-1]
        if not isinstance(last, dict) or last.get("role") != "user":
            continue
        if marker in str(last.get("content")):
            count += 1
    return count


def _wait_until(page: Page, predicate, timeout_s: float, what: str) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        page.wait_for_timeout(1000)
    raise AssertionError(f"{what} did not happen within {timeout_s:.0f}s")


def _pause(page: Page, ms: int) -> None:
    """Hold a state on screen only when the run is being filmed or screenshotted."""
    if _SHOTS_DIR is not None or os.environ.get("OMNIGENT_E2E_RECORD_DIR"):
        page.wait_for_timeout(ms)


def _shot(page: Page, name: str) -> None:
    if _SHOTS_DIR is None:
        return
    shots = Path(_SHOTS_DIR)
    shots.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(shots / f"{name}.png"))


def _short_idle_window_s() -> float | None:
    raw = os.environ.get(_IDLE_TIMEOUT_ENV)
    if raw is None:
        return None
    try:
        window = float(raw)
    except ValueError:
        return None
    return window if 0 < window <= 60 else None


def _close_claude_pane(page: Page, base_url: str, session_id: str) -> None:
    idle_window = _short_idle_window_s()
    if idle_window is not None:
        closed_after = time.monotonic()
        _wait_until(
            page,
            lambda: not _terminals(base_url, session_id),
            _REAP_TIMEOUT_S,
            "the idle reaper removing the Claude pane",
        )
        print(f"[journey] pane reaped after {time.monotonic() - closed_after:.0f}s idle")
        return
    terminals = _terminals(base_url, session_id)
    terminal_id = str(terminals[0]["id"]) if terminals else None
    if terminal_id is not None:
        httpx.delete(
            f"{base_url}/v1/sessions/{session_id}/resources/terminals/{terminal_id}",
            timeout=30.0,
        ).raise_for_status()
    _wait_until(
        page,
        lambda: not _terminals(base_url, session_id),
        _PANE_CLOSE_TIMEOUT_S,
        "the closed Claude pane leaving the session's terminals",
    )
    print(f"[journey] pane {terminal_id} closed through the resources API")


@pytest.mark.timeout(900)
def test_relaunched_claude_pane_does_not_resubmit_the_initial_prompt(
    request: pytest.FixtureRequest,
    built_spa: None,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    for binary in ("claude", "tmux"):
        if shutil.which(binary) is None:
            pytest.skip(f"requires the real {binary} executable")

    nonce = uuid.uuid4().hex[:8]
    prompt = f"INITIAL_PROMPT_{nonce}: reply with one short sentence."

    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    if respawned is not None:
        request.addfinalizer(respawned.terminate)
    runner_id = str(_server_state["runner_id"])
    mock_config = _temp_omnigent_mock_config(
        mock_llm_server_url, "claude", workflow_owned=bool(_server_state.get("workflow_owned"))
    )
    mock_config.__enter__()
    request.addfinalizer(lambda: mock_config.__exit__(None, None, None))

    reset_mock_llm(mock_llm_server_url)
    set_fallback_mock_llm(mock_llm_server_url, "default", "Acknowledged.")
    set_fallback_mock_llm(mock_llm_server_url, _CLAUDE_MOCK_MODEL, "Acknowledged.")

    # Same persisted payload as `omni claude --server <url> -p "<prompt>"`.
    session_id = _create_native_claude_session(
        live_server, runner_id, terminal_launch_args=[prompt]
    )
    request.addfinalizer(
        lambda: httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
    )
    print(f"[journey] session_id={session_id} prompt={prompt!r}")

    page: Page = request.getfixturevalue("page")
    page.goto(f"{live_server}/c/{session_id}")
    _select_view_mode(page, "Terminal")
    terminal = page.locator(_TERMINAL_VIEW).last
    expect(terminal).to_have_attribute(
        "data-state", "connected", timeout=_TERMINAL_READY_TIMEOUT_MS
    )

    _wait_until(
        page,
        lambda: (
            len(_user_items_with(live_server, session_id, nonce)) >= 1
            and _assistant_items(live_server, session_id) >= 1
        ),
        _FIRST_TURN_TIMEOUT_S,
        "the initial prompt and its reply reaching the transcript",
    )
    first_prompt_items = _user_items_with(live_server, session_id, nonce)
    first_prompt_requests = _mock_requests_with(mock_llm_server_url, f"INITIAL_PROMPT_{nonce}")
    print(
        f"[journey] after first turn: prompt user items={len(first_prompt_items)} "
        f"mock requests ending in the prompt={first_prompt_requests} "
        f"terminals={[t.get('id') for t in _terminals(live_server, session_id)]}"
    )
    assert len(first_prompt_items) == 1, first_prompt_items
    _pause(page, 3000)
    _shot(page, "01-first-prompt-submitted")
    _select_view_mode(page, "Chat")
    prompt_bubbles = page.locator(_USER_BUBBLE).filter(has_text=f"INITIAL_PROMPT_{nonce}")
    expect(prompt_bubbles).to_have_count(1, timeout=30_000)
    expect(prompt_bubbles.first).to_be_visible()
    _pause(page, 3000)
    _shot(page, "01b-chat-one-prompt")
    _select_view_mode(page, "Terminal")

    _close_claude_pane(page, live_server, session_id)
    resume = page.get_by_role("button", name="Resume session")
    expect(resume).to_be_visible(timeout=60_000)
    expect(page.get_by_text("The harness is not running.")).to_be_visible()
    _pause(page, 3000)
    _shot(page, "02-pane-closed")

    resume.click()
    terminal = page.locator(_TERMINAL_VIEW).last
    expect(terminal).to_have_attribute(
        "data-state", "connected", timeout=_TERMINAL_READY_TIMEOUT_MS
    )
    print("[journey] pane relaunched after Resume session")
    deadline = time.monotonic() + _RESUBMIT_WATCH_S
    while time.monotonic() < deadline:
        if len(_user_items_with(live_server, session_id, f"INITIAL_PROMPT_{nonce}")) >= 2:
            break
        page.wait_for_timeout(1000)
    _pause(page, 5000)
    _shot(page, "03-terminal-after-resume")
    _select_view_mode(page, "Chat")
    prompt_bubbles = page.locator(_USER_BUBBLE).filter(has_text=f"INITIAL_PROMPT_{nonce}")
    expect(prompt_bubbles.first).to_be_visible(timeout=30_000)
    _pause(page, 5000)
    visible_prompt_bubbles = prompt_bubbles.count()
    print(f"[journey] chat view shows {visible_prompt_bubbles} user bubble(s) with the prompt")
    _shot(page, "04-chat-after-resume")

    prompt_items = _user_items_with(live_server, session_id, f"INITIAL_PROMPT_{nonce}")
    prompt_requests = _mock_requests_with(mock_llm_server_url, f"INITIAL_PROMPT_{nonce}")
    transcript = [
        (item.get("role"), _item_text(item)[:60])
        for item in _ordered_message_items(live_server, session_id)
    ]
    print(
        f"[journey] after relaunch: prompt user items={len(prompt_items)} "
        f"mock requests ending in the prompt={prompt_requests} items={transcript}"
    )
    assert len(prompt_items) == 1 and visible_prompt_bubbles == 1, (
        f"the initial prompt was submitted {len(prompt_items)} times after the pane relaunch "
        f"({visible_prompt_bubbles} user bubbles on screen; mock saw {prompt_requests} request(s) "
        f"ending in it): {prompt_items}"
    )
    assert prompt_requests == first_prompt_requests, (
        f"the model received the initial prompt again after the relaunch "
        f"({first_prompt_requests} -> {prompt_requests} requests)"
    )
