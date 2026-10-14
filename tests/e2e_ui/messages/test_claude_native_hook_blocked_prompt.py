"""A web prompt that a Claude Code ``UserPromptSubmit`` hook blocks is explained in the chat."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    _REPO_ROOT,
    _create_native_claude_session,
    _ensure_runner_online,
    _server_state,
    _temp_omnigent_mock_config,
    prepared_repro_environment,
    reset_mock_llm,
    set_fallback_mock_llm,
)

from .test_message_render_parity import _ASSISTANT, _USER, _ensure_chat_view
from .test_native_claude_render_parity import (
    _CLAUDE_MOCK_MODEL,
    _open_terminal_view,
    _wait_terminal_connected,
)

_log = logging.getLogger(__name__)

_BLOCK_MARKER = "BLOCKME"
_BLOCK_REASON = "Blocked: test"
# The hook reads Claude's JSON payload on stdin; a prompt carrying the marker is refused.
_BLOCK_HOOK_COMMAND = (
    f"if grep -q {_BLOCK_MARKER}; then printf '%s' "
    f"'{json.dumps({'decision': 'block', 'reason': _BLOCK_REASON})}'; fi"
)
# Claude shows the refusal in its own TUI well within this; a web mirror of a
# recorded prompt normally lands in under a second.
_SETTLE_WINDOW_MS = 20_000
_REPLY_TIMEOUT_MS = 90_000


def _workspace_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A workspace directory the session's runner can read.

    The workflow-prepared runner shares only the worktree with the test
    sandbox, so its workspaces live under ``.omnigent``; a fixture-spawned
    runner shares ``tmp_path``.
    """
    if prepared_repro_environment()["OMNIGENT_REPRO_SERVER_URL"]:
        root = _REPO_ROOT / ".omnigent" / "e2e-workspaces" / f"hook-block-{uuid.uuid4().hex[:8]}"
        root.mkdir(parents=True)
        return root
    return tmp_path_factory.mktemp("hook-block-workspace")


@pytest.fixture
def hook_blocked_claude_session(
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str, Path]]:
    """A real Claude CLI whose project settings block prompts containing ``BLOCKME``."""
    workspace = _workspace_root(tmp_path_factory)
    settings = workspace / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "UserPromptSubmit": [
                        {"hooks": [{"type": "command", "command": _BLOCK_HOOK_COMMAND}]}
                    ]
                }
            }
        )
    )
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])
    with _temp_omnigent_mock_config(
        mock_llm_server_url, "claude", workflow_owned=bool(_server_state.get("workflow_owned"))
    ):
        session_id = _create_native_claude_session(
            live_server, runner_id, workspace=str(workspace)
        )
        try:
            yield (live_server, session_id, workspace)
        finally:
            httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
            shutil.rmtree(workspace, ignore_errors=True)
            if respawned is not None:
                respawned.terminate()
                try:
                    respawned.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    respawned.kill()
                    respawned.wait(timeout=5)


def _send(page: Page, text: str) -> None:
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_enabled(timeout=30_000)
    composer.fill(text)
    composer.press("Enter")


def _session_items(base_url: str, session_id: str) -> list[dict]:
    response = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/items",
        params={"order": "asc", "limit": 100},
        timeout=10.0,
    )
    response.raise_for_status()
    return response.json()["data"]


def _pending_texts(base_url: str, session_id: str) -> list[str]:
    snapshot = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0).json()
    return [
        block.get("text", "")
        for entry in snapshot.get("pending_inputs") or []
        for block in entry.get("content", [])
    ]


@pytest.mark.nightly
@pytest.mark.timeout(420)
def test_hook_blocked_web_prompt_is_explained_with_the_hook_reason(
    request: pytest.FixtureRequest,
    hook_blocked_claude_session: tuple[str, str, Path],
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    base_url, session_id, workspace = hook_blocked_claude_session
    _log.info("hook-blocked claude session: session_id=%s workspace=%s", session_id, workspace)
    blocked_prompt = f"{_BLOCK_MARKER} hello"
    follow_up = f"hello again {uuid.uuid4().hex[:8]}"
    reply = f"reply-{uuid.uuid4().hex[:8]}"
    reset_mock_llm(mock_llm_server_url)
    for model in ("default", _CLAUDE_MOCK_MODEL):
        set_fallback_mock_llm(mock_llm_server_url, model, reply)

    page: Page = request.getfixturevalue("page")
    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _ensure_chat_view(page)

    _send(page, blocked_prompt)
    blocked_bubble = page.locator(_USER, has_text=blocked_prompt)
    expect(blocked_bubble).to_have_count(1, timeout=30_000)
    # The TUI is where the hook's refusal is printed; show it, then return to the chat.
    page.wait_for_timeout(5_000)
    _open_terminal_view(page)
    page.wait_for_timeout(5_000)
    page.screenshot(path=str(tmp_path / "terminal-after-block.png"))
    _ensure_chat_view(page)
    page.wait_for_timeout(_SETTLE_WINDOW_MS - 10_000)
    page.screenshot(path=str(tmp_path / "chat-after-block.png"))
    pending_after_block = _pending_texts(base_url, session_id)
    errors_after_block = [
        item for item in _session_items(base_url, session_id) if item["type"] == "error"
    ]
    copy_link_disabled = blocked_bubble.get_by_test_id("copy-message-link").is_disabled()
    _log.info(
        "after block: pending_inputs=%s errors=%s copy_link_disabled=%s",
        pending_after_block,
        errors_after_block,
        copy_link_disabled,
    )

    _send(page, follow_up)
    expect(page.locator(_ASSISTANT, has_text=reply).first).to_be_visible(timeout=_REPLY_TIMEOUT_MS)
    expect(page.locator(_USER, has_text=follow_up)).to_have_count(1, timeout=30_000)
    pill = page.get_by_test_id("error-pill")
    if pill.count():
        pill.first.click()
        page.wait_for_timeout(4_000)
    page.screenshot(path=str(tmp_path / "chat-after-follow-up.png"))
    pill_text = pill.first.inner_text() if pill.count() else ""
    pill_headline = (
        pill.first.get_by_test_id("error-headline").inner_text() if pill.count() else ""
    )
    items = _session_items(base_url, session_id)
    errors = [item for item in items if item["type"] == "error"]
    (tmp_path / "items.json").write_text(json.dumps(items, indent=2))
    _log.info(
        "after follow-up: errors=%s pill_headline=%r pill_text=%r",
        errors,
        pill_headline,
        pill_text,
    )

    failures: list[str] = []
    if pending_after_block or not errors_after_block:
        failures.append(
            f"blocked prompt was still pending {_SETTLE_WINDOW_MS // 1000}s after the hook "
            f"refused it (pending_inputs={pending_after_block}, errors={errors_after_block})"
        )
    explained = [error for error in errors if _BLOCK_REASON in json.dumps(error)]
    if not explained:
        failures.append(
            "no error item for the blocked prompt names the hook's reason "
            f"{_BLOCK_REASON!r}; errors={[(e.get('code'), e.get('message')) for e in errors]}"
        )
    if "hook blocked this message" not in pill_headline or _BLOCK_REASON not in pill_text:
        failures.append(
            "the chat does not say a hook blocked the message and why; "
            f"headline={pill_headline!r} pill={pill_text!r}"
        )
    assert not failures, "\n".join(failures)
