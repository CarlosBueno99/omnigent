"""Model-unavailable recovery guidance on non-Codex harnesses.

Drives the real production error path end to end: a live ``claude`` CLI
(claude-native) and the in-process Claude Agent SDK (claude-sdk), each driven
from the web composer against the mock LLM scripted to reject the turn with an
HTTP 404 model-not-found (a stand-in for a live provider rejection). The Chat
view's failure card is read live and again after a page reload.

``claude-sdk`` otherwise surfaces only the raw provider error with no
model-picker / ``/model`` / provider-config hint; the shared diagnosis adds
one, which this guards. ``claude-native`` already carries a ``/model`` hint via
the Claude Code CLI passthrough; this guard confirms that stays intact.
"""

from __future__ import annotations

import io
import json
import logging
import os
import tarfile
import time
import uuid
from pathlib import Path

import httpx
import pytest
import yaml
from playwright.sync_api import Page, expect

from tests.e2e_ui.chat.test_native_claude_quota_error_in_chat import (
    _mock_request_count,
    _terminal_screen_text,
    _transcript_items,
)
from tests.e2e_ui.conftest import (
    _ensure_runner_online,
    _server_state,
    configure_mock_llm,
    reset_mock_llm,
    set_fallback_mock_llm,
)
from tests.e2e_ui.messages.test_message_render_parity import _USER, _WORKING, _select_view_mode
from tests.e2e_ui.messages.test_native_claude_render_parity import (
    _CLAUDE_MOCK_MODEL,
    _open_terminal_view,
    _wait_terminal_connected,
)

_log = logging.getLogger(__name__)

# An OpenRouter-style id that passes Omnigent's model-id and Claude-family
# validation but no configured provider actually serves.
_UNAVAILABLE_MODEL = "anthropic/claude-opus-4-8"
_NOT_FOUND_ERROR = f"model: {_UNAVAILABLE_MODEL}"
_NOT_FOUND_RESPONSES = [{"error": _NOT_FOUND_ERROR, "status_code": 404}] * 60

# Phrases an actionable model-unavailable recovery hint would use; the fix must
# make the turn-error card carry at least one of them.
_GUIDANCE_TOKENS = (
    "/model",
    "model picker",
    "pick a",
    "choose a",
    "choose another",
    "select a",
    "supported model",
    "available model",
    "different model",
    "provider config",
    "try this",
)

_FIRST_REQUEST_TIMEOUT_S = 120.0
_TURN_END_TIMEOUT_S = 180.0
_RETRY_QUIET_S = 20.0
_HOLD_MS = 4_000

_EVIDENCE_DIR = Path(
    os.environ.get("E2E_SCREENSHOT_DIR", "recordings/model-unavailable-guidance/exec")
)


def _save(name: str, payload: object) -> None:
    _EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    (_EVIDENCE_DIR / name).write_text(json.dumps(payload, indent=2, default=str))


def _screenshot(page: Page, name: str) -> None:
    _EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(_EVIDENCE_DIR / name))


def _stable_mock_url(fallback: str) -> str:
    """The runner-reachable mock URL.

    Defaults to the ``mock_llm_server_url`` fixture, which the co-located runner
    reaches in a standard standalone run. ``REPRO_STABLE_MOCK_URL`` overrides it
    for the prepared repro-env, whose per-exec proxy URL the long-lived runner
    cannot reach.
    """
    return os.environ.get("REPRO_STABLE_MOCK_URL") or fallback


def _bundle(name: str, config: dict[str, object]) -> bytes:
    with io.BytesIO() as buf:
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            data = yaml.safe_dump(config, sort_keys=False).encode()
            info = tarfile.TarInfo(f"{name}.yaml")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        return buf.getvalue()


def _create_claude_sdk_session(base_url: str, runner_id: str, mock_url: str) -> tuple[str, str]:
    name = f"sdk-model-guidance-{uuid.uuid4().hex[:8]}"
    bundle = _bundle(
        name,
        {
            "name": name,
            "prompt": "You are a terse assistant. Answer in as few words as possible.",
            "executor": {
                "harness": "claude-sdk",
                "model": _CLAUDE_MOCK_MODEL,
                "auth": {"type": "api_key", "api_key": "mock-key", "base_url": mock_url},
            },
        },
    )
    create = httpx.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": (f"{name}.tar.gz", bundle, "application/gzip")},
        timeout=60.0,
    )
    create.raise_for_status()
    session_id = str(create.json()["session_id"])
    httpx.patch(
        f"{base_url}/v1/sessions/{session_id}", json={"runner_id": runner_id}, timeout=30.0
    ).raise_for_status()
    return name, session_id


def _attempt_model_switch(base_url: str, session_id: str) -> dict[str, object]:
    """Request the unavailable model the way the UI does (persist model_override)."""
    resp = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"model_override": _UNAVAILABLE_MODEL},
        timeout=30.0,
    )
    body: object
    try:
        body = resp.json()
    except Exception:
        body = resp.text[:1000]
    return {"status": resp.status_code, "body": body}


def _wait_for_turn_failure(page: Page, mock_url: str, baseline: int) -> int:
    """Block until the provider stopped seeing requests and the turn is no longer working."""
    started = time.monotonic()
    while _mock_request_count(mock_url) <= baseline:
        if time.monotonic() - started > _FIRST_REQUEST_TIMEOUT_S:
            raise AssertionError("the harness never contacted the model provider after Send")
        page.wait_for_timeout(1_000)
    last_count = _mock_request_count(mock_url)
    quiet_since = time.monotonic()
    deadline = started + _TURN_END_TIMEOUT_S
    while time.monotonic() < deadline:
        page.wait_for_timeout(2_000)
        count = _mock_request_count(mock_url)
        if count != last_count:
            last_count, quiet_since = count, time.monotonic()
            continue
        pill_up = page.get_by_test_id("error-pill").count() > 0
        quiet = time.monotonic() - quiet_since >= _RETRY_QUIET_S
        if pill_up or (quiet and page.locator(_WORKING).count() == 0):
            return count
    return last_count


_TURN_ERROR_FRAGMENT = "ran into an error during this turn"


def _capture_card(page: Page) -> dict[str, object]:
    """Read every failure pill the Chat view shows for the failed turn(s)."""
    pills = page.get_by_test_id("error-pill")
    count = pills.count()
    cards: list[dict[str, object]] = []
    for i in range(count):
        pill = pills.nth(i)
        toggle = pill.locator('button[aria-expanded="false"]')
        if toggle.count():
            toggle.first.click()
        headline = pill.get_by_test_id("error-headline")
        body = pill.get_by_test_id("error-message-content")
        text = pill.inner_text()
        hay = text.lower()
        cards.append(
            {
                "headline": headline.inner_text() if headline.count() else text,
                "body": body.inner_text() if body.count() else "",
                "full": text,
                "recovery_tokens": sorted(t for t in _GUIDANCE_TOKENS if t in hay),
                "model_id": _UNAVAILABLE_MODEL in text,
            }
        )
    full_hay = "\n".join(c["full"] for c in cards)
    return {
        "pill_count": count,
        "cards": cards,
        "model_id_preserved": _UNAVAILABLE_MODEL in full_hay,
        "headlines": [c["headline"] for c in cards],
    }


def _turn_error_pill(card: dict[str, object]) -> dict[str, object] | None:
    for c in card["cards"]:  # type: ignore[index]
        if _TURN_ERROR_FRAGMENT in str(c["headline"]).lower():
            return c
    return None


def _assert_turn_error_shows_guidance(where: str, card: dict[str, object]) -> None:
    assert card.get("pill_count"), f"{where}: no error pill rendered for the failed turn"
    turn = _turn_error_pill(card)
    assert turn is not None, (
        f"{where}: no model-not-found turn-error pill; headlines={card['headlines']!r}"
    )
    assert turn["recovery_tokens"], (
        f"{where}: model-not-found turn-error card shows no actionable recovery hint "
        f"(expected model-picker / '/model' / provider-config guidance); body={turn['body']!r}"
    )


def _drive(
    page: Page,
    base_url: str,
    session_id: str,
    mock_url: str,
    label: str,
    open_terminal: bool,
) -> dict[str, object]:
    reset_mock_llm(mock_url)
    set_fallback_mock_llm(mock_url, _CLAUDE_MOCK_MODEL, "ok")

    page.goto(f"{base_url}/c/{session_id}")
    if open_terminal:
        _open_terminal_view(page)
        _wait_terminal_connected(page)
        page.wait_for_timeout(8_000)
        _screenshot(page, f"{label}-1-terminal-idle.png")
        _select_view_mode(page, "Chat")

    composer = page.get_by_role("textbox", name="Message the agent")
    expect(composer).to_be_visible(timeout=60_000)
    switch = _attempt_model_switch(base_url, session_id)
    _log.info("%s model switch to %s -> %s", label, _UNAVAILABLE_MODEL, switch)
    _screenshot(page, f"{label}-2-after-switch.png")

    # From here the provider rejects every request as model-not-found.
    configure_mock_llm(mock_url, _NOT_FOUND_RESPONSES, key=_CLAUDE_MOCK_MODEL)
    configure_mock_llm(mock_url, _NOT_FOUND_RESPONSES, key="default")
    configure_mock_llm(mock_url, _NOT_FOUND_RESPONSES, key=_UNAVAILABLE_MODEL)
    baseline = _mock_request_count(mock_url)

    prompt = f"model-guidance-{uuid.uuid4().hex[:6]} please say hello"
    composer.fill(prompt)
    page.get_by_role("button", name="Send", exact=True).click()
    expect(page.locator(_USER, has_text=prompt)).to_have_count(1, timeout=60_000)
    _screenshot(page, f"{label}-3-sent.png")

    requests_seen = _wait_for_turn_failure(page, mock_url, baseline) - baseline
    _log.info("%s: provider saw %d requests after Send", label, requests_seen)
    expect(page.get_by_test_id("error-pill").first).to_be_visible(timeout=60_000)
    page.wait_for_timeout(_HOLD_MS)

    live_card = _capture_card(page)
    _screenshot(page, f"{label}-4-error-card-live.png")

    pane = _terminal_screen_text(base_url, session_id) if open_terminal else ""
    items = _transcript_items(base_url, session_id)

    page.reload()
    expect(page.get_by_test_id("error-pill").first).to_be_visible(timeout=60_000)
    page.wait_for_timeout(2_000)
    reload_card = _capture_card(page)
    page.wait_for_timeout(_HOLD_MS)
    _screenshot(page, f"{label}-5-error-card-reloaded.png")

    assert live_card["model_id_preserved"], (
        f"{label} live: unavailable model id dropped from the card"
    )

    record = {
        "session_id": session_id,
        "model_switch": switch,
        "requests_seen": requests_seen,
        "live_card": live_card,
        "reload_card": reload_card,
        "model_id_in_pane": _UNAVAILABLE_MODEL in pane or "404" in pane,
        "item_types": [i.get("type") for i in items],
    }
    _save(f"{label}.json", record)

    _assert_turn_error_shows_guidance(f"{label} live", live_card)
    _assert_turn_error_shows_guidance(f"{label} after reload", reload_card)
    return record


@pytest.mark.timeout(500)
def test_claude_native_model_unavailable_error_shows_recovery_guidance(
    request: pytest.FixtureRequest,
    native_claude_mock_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    # Already-covered guard: claude-native surfaces "Run /model" guidance via the
    # Claude Code CLI passthrough, so this passes today and the fix must keep it.
    base_url, session_id = native_claude_mock_session
    _log.info("claude-native session %s on %s", session_id, base_url)
    page: Page = request.getfixturevalue("page")
    _drive(page, base_url, session_id, mock_llm_server_url, "claude-native", open_terminal=True)


@pytest.mark.timeout(400)
def test_claude_sdk_model_unavailable_error_shows_recovery_guidance(
    request: pytest.FixtureRequest,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    # Without the shared diagnosis the claude-sdk model-not-found card shows
    # only the raw provider error with no recovery hint; this guards the hint.
    assert _ensure_runner_online(live_server, tmp_path_factory) is None
    runner_id = str(_server_state["runner_id"])
    runner_mock_url = _stable_mock_url(mock_llm_server_url)
    name, session_id = _create_claude_sdk_session(live_server, runner_id, runner_mock_url)
    _log.info("claude-sdk session %s (%s) on %s", session_id, name, live_server)
    page: Page = request.getfixturevalue("page")
    try:
        _drive(
            page, live_server, session_id, mock_llm_server_url, "claude-sdk", open_terminal=False
        )
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
