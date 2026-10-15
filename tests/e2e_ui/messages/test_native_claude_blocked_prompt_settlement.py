"""Web messages Claude refused must settle with their real outcome, not as "never recorded".

Two refusals on the real Claude CLI: a ``UserPromptSubmit`` hook blocks a web
message (Claude shows the hook's reason only in its terminal), and ``/login``
sent from the web composer is refused before it reaches the terminal. A later
message Claude answers must not turn the refused one into the generic
``native_prompt_not_recorded`` error ("Message not delivered. Try sending it
again."), which is reserved for inputs whose loss is unexplained.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._helpers.native_session import create_native_session
from tests.e2e_ui.conftest import (
    _REPO_ROOT,
    _bind_session_runner,
    _ensure_runner_online,
    _server_state,
    _temp_omnigent_mock_config,
    reset_mock_llm,
    set_fallback_mock_llm,
)

from .test_message_render_parity import _ASSISTANT, _USER, _WORKING, _ensure_chat_view
from .test_native_claude_render_parity import (
    _CLAUDE_MOCK_MODEL,
    _MOCK_TURN_TIMEOUT_MS,
    _open_terminal_view,
    _wait_terminal_connected,
)

_MISSING_CODE = "native_prompt_not_recorded"
_HOOK_MARKER = "BLOCKME"
_HOOK_REASON = "Blocked: test"
_HOOK_DECISION = json.dumps({"decision": "block", "reason": _HOOK_REASON}, separators=(",", ":"))
# Claude pipes the hook payload to stdin; only its prompt field can carry the marker.
_HOOK_SCRIPT = (
    "#!/bin/sh\n"
    "input=$(cat)\n"
    'case "$input" in\n'
    f"  *{_HOOK_MARKER}*) printf '%s\\n' '{_HOOK_DECISION}' ;;\n"
    "esac\n"
    "exit 0\n"
)
_SETTLE_TIMEOUT_S = 45.0


@pytest.fixture
def hook_blocked_claude_session(
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str]]:
    """A real Claude CLI whose project settings block prompts containing ``BLOCKME``.

    The workspace lives under the worktree: the runner may run in a sandbox
    whose ``/tmp`` is private, so a pytest temp dir would be invisible to it.
    """
    workspace = _REPO_ROOT / ".omnigent" / "e2e-hook-workspaces" / uuid.uuid4().hex[:8]
    hooks_dir = workspace / ".claude" / "hooks"
    hooks_dir.mkdir(parents=True)
    script = hooks_dir / "block_marker.sh"
    script.write_text(_HOOK_SCRIPT, encoding="utf-8")
    script.chmod(0o755)
    hook = {"type": "command", "command": str(script)}
    settings = {"hooks": {"UserPromptSubmit": [{"hooks": [hook]}]}}
    (workspace / ".claude" / "settings.json").write_text(json.dumps(settings), encoding="utf-8")

    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])
    try:
        with _temp_omnigent_mock_config(
            mock_llm_server_url, "claude", workflow_owned=bool(_server_state.get("workflow_owned"))
        ):
            created = create_native_session(
                httpx, live_server, harness="claude", metadata={"workspace": str(workspace)}
            )
            session_id = str(created["session_id"])
            _bind_session_runner(live_server, session_id, runner_id)
            try:
                yield (live_server, session_id)
            finally:
                httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
    finally:
        if respawned is not None:
            respawned.terminate()
            try:
                respawned.wait(timeout=5)
            except subprocess.TimeoutExpired:
                respawned.kill()
                respawned.wait(timeout=5)
        shutil.rmtree(workspace, ignore_errors=True)


def _compose(page: Page, text: str) -> None:
    # The placeholder changes while a turn runs; the label is stable.
    box = page.get_by_label("Message the agent")
    expect(box).to_be_enabled(timeout=30_000)
    box.fill(text)
    box.press("Enter")


def _mark(t0: float, label: str) -> None:
    print(f"[cue] +{time.monotonic() - t0:5.1f}s {label}", flush=True)


def _snapshot(base_url: str, session_id: str) -> dict[str, Any]:
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=15.0)
    resp.raise_for_status()
    return resp.json()


def _items(base_url: str, session_id: str) -> list[dict[str, Any]]:
    resp = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/items",
        params={"limit": 200, "order": "asc"},
        timeout=15.0,
    )
    resp.raise_for_status()
    return list(resp.json()["data"])


def _field(item: dict[str, Any], name: str) -> Any:
    return item.get(name) if name in item else (item.get("data") or {}).get(name)


def _error_items(base_url: str, session_id: str) -> list[tuple[Any, Any]]:
    return [
        (_field(item, "code"), _field(item, "message"))
        for item in _items(base_url, session_id)
        if item.get("type") == "error"
    ]


def _dump(base_url: str, session_id: str, label: str) -> None:
    pending = json.dumps(_snapshot(base_url, session_id).get("pending_inputs"))
    errors = json.dumps([i for i in _items(base_url, session_id) if i.get("type") == "error"])
    print(f"[http] {label} GET /v1/sessions/{session_id} pending_inputs={pending}", flush=True)
    print(f"[http] {label} GET /v1/sessions/{session_id}/items error_items={errors}", flush=True)


def _wait_until_settled(base_url: str, session_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + _SETTLE_TIMEOUT_S
    snapshot = _snapshot(base_url, session_id)
    while snapshot.get("pending_inputs") and time.monotonic() < deadline:
        time.sleep(2.0)
        snapshot = _snapshot(base_url, session_id)
    return snapshot


def _assert_not_reported_missing(
    base_url: str, session_id: str, refused: str, followup: str, pending_after_refusal: Any
) -> None:
    settled = _wait_until_settled(base_url, session_id)
    errors = _error_items(base_url, session_id)
    missing = [error for error in errors if error[0] == _MISSING_CODE]
    assert settled.get("pending_inputs") == [], settled.get("pending_inputs")
    assert missing == [], (
        f"the refused web message {refused!r} stayed pending ({pending_after_refusal}) and, once "
        f"{followup!r} was accepted, was persisted as never recorded: {missing}; "
        f"all errors: {errors}"
    )


def _arm_mock_reply(mock_llm_server_url: str, token: str) -> None:
    reset_mock_llm(mock_llm_server_url)
    for model in ("default", _CLAUDE_MOCK_MODEL):
        set_fallback_mock_llm(mock_llm_server_url, model, token)


@pytest.mark.nightly
@pytest.mark.timeout(480)
def test_hook_blocked_web_message_is_not_reported_as_never_recorded(
    request: pytest.FixtureRequest,
    hook_blocked_claude_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    base_url, session_id = hook_blocked_claude_session
    token = f"tok-{uuid.uuid4().hex[:8]}"
    _arm_mock_reply(mock_llm_server_url, token)
    shots = Path(request.config.getoption("--output")) / request.node.name
    shots.mkdir(parents=True, exist_ok=True)

    page: Page = request.getfixturevalue("page")
    t0 = time.monotonic()
    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _mark(t0, "Claude TUI attached")
    _ensure_chat_view(page)

    blocked = f"{_HOOK_MARKER} hello"
    _compose(page, blocked)
    _mark(t0, f"sent {blocked!r}")
    expect(page.locator(_USER, has_text=blocked).first).to_be_visible(timeout=30_000)
    page.wait_for_timeout(6_000)
    _open_terminal_view(page)
    page.wait_for_timeout(5_000)
    page.screenshot(path=str(shots / "terminal-after-hook-block.png"))
    _mark(t0, "terminal view after the hook block")
    _ensure_chat_view(page)
    pending_after_block = _snapshot(base_url, session_id).get("pending_inputs")
    print(f"[probe] pending inputs after the hook block: {pending_after_block}", flush=True)
    _dump(base_url, session_id, "after-hook-block")
    # Claude must be idle before the bridge pastes the next prompt.
    page.wait_for_timeout(8_000)

    followup = f"Reply with exactly this token and nothing else: {token}"
    _compose(page, followup)
    _mark(t0, "sent the follow-up message")
    expect(page.locator(_ASSISTANT, has_text=token).first).to_be_visible(
        timeout=2 * _MOCK_TURN_TIMEOUT_MS
    )
    expect(page.locator(_WORKING)).to_have_count(0, timeout=60_000)
    _mark(t0, "follow-up answered")
    _wait_until_settled(base_url, session_id)
    page.wait_for_timeout(4_000)
    page.screenshot(path=str(shots / "chat-after-follow-up.png"))
    _mark(t0, "chat view after settlement")
    print(f"[probe] error items: {_error_items(base_url, session_id)}", flush=True)
    _dump(base_url, session_id, "after-follow-up")
    print(
        "[probe] on screen: "
        f"error pills={page.get_by_test_id('error-pill').count()} "
        f"'Message not delivered'={page.get_by_text('Message not delivered').count()} "
        f"'never recorded'={page.get_by_text('never recorded').count()}",
        flush=True,
    )
    page.wait_for_timeout(3_000)

    expect(page.locator(_USER, has_text=blocked)).to_have_count(1)
    _assert_not_reported_missing(base_url, session_id, blocked, followup, pending_after_block)


@pytest.mark.nightly
@pytest.mark.timeout(480)
def test_refused_web_login_is_not_reported_as_never_recorded(
    request: pytest.FixtureRequest,
    native_claude_mock_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    base_url, session_id = native_claude_mock_session
    token = f"tok-{uuid.uuid4().hex[:8]}"
    _arm_mock_reply(mock_llm_server_url, token)
    shots = Path(request.config.getoption("--output")) / request.node.name
    shots.mkdir(parents=True, exist_ok=True)

    page: Page = request.getfixturevalue("page")
    t0 = time.monotonic()
    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _mark(t0, "Claude TUI attached")
    _ensure_chat_view(page)

    _compose(page, "/login")
    _mark(t0, "sent '/login'")
    expect(page.locator(_USER, has_text="/login").first).to_be_visible(timeout=30_000)
    expect(page.get_by_test_id("error-pill").first).to_be_visible(timeout=60_000)
    deadline = time.monotonic() + 30.0
    refusal: list[tuple[Any, Any]] = []
    while not refusal and time.monotonic() < deadline:
        refusal = [e for e in _error_items(base_url, session_id) if "omni setup" in str(e[1])]
        time.sleep(1.0)
    assert refusal, _error_items(base_url, session_id)
    pending_after_refusal = _snapshot(base_url, session_id).get("pending_inputs")
    print(f"[probe] pending inputs after the refusal: {pending_after_refusal}", flush=True)
    _dump(base_url, session_id, "after-login-refusal")
    page.screenshot(path=str(shots / "chat-after-login-refusal.png"))
    _mark(t0, "refusal shown")
    page.wait_for_timeout(5_000)

    followup = f"Reply with exactly this token and nothing else: {token}"
    _compose(page, followup)
    _mark(t0, "sent the follow-up message")
    expect(page.locator(_ASSISTANT, has_text=token).first).to_be_visible(
        timeout=2 * _MOCK_TURN_TIMEOUT_MS
    )
    expect(page.locator(_WORKING)).to_have_count(0, timeout=60_000)
    _mark(t0, "follow-up answered")
    _wait_until_settled(base_url, session_id)
    page.wait_for_timeout(4_000)
    page.screenshot(path=str(shots / "chat-after-follow-up.png"))
    _mark(t0, "chat view after settlement")
    print(f"[probe] error items: {_error_items(base_url, session_id)}", flush=True)
    _dump(base_url, session_id, "after-follow-up")
    print(
        "[probe] on screen: "
        f"error pills={page.get_by_test_id('error-pill').count()} "
        f"'Message not delivered'={page.get_by_text('Message not delivered').count()} "
        f"'never recorded'={page.get_by_text('never recorded').count()}",
        flush=True,
    )
    page.wait_for_timeout(3_000)

    expect(page.locator(_USER, has_text="/login")).to_have_count(1)
    _assert_not_reported_missing(base_url, session_id, "/login", followup, pending_after_refusal)
