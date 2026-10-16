r"""UI journey: a web message sent while Claude Code's ``/permissions`` dialog is open.

A person can open Claude Code's own settings dialogs from the embedded
terminal and leave one up. Before the runner types a web-composer message into
Claude it checks the pane for a pending decision prompt (a question or a tool
permission request) and holds the message if one is up. The ``/permissions``
dialog is not a question Claude asked, though its footer carries the same
"Enter to select"/"Enter to submit" + "Esc to cancel" text those prompts do.
The runner must treat it as a terminal-opened surface instead: back out of it
with Escape (its documented dismissal) and deliver the message, or at least
tell the sender a dialog is in the way.

Drives the real claude-native stack (a live ``claude`` CLI in the session
terminal, fed by the in-process mock LLM). The dialog is opened with real
keystrokes through the xterm and left on its add-rule field; the message is
sent from the web composer. The pane is read through the product's read-only
terminal attach to confirm the dialog state before and after the send.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Browser, expect

from omnigent.harnesses.claude_native import bridge
from tests._helpers.terminal_screen import read_terminal_screen
from tests.e2e_ui.conftest import reset_mock_llm, set_fallback_mock_llm

from .test_message_render_parity import (
    _ASSISTANT,
    _WORKING,
    _ensure_chat_view,
    _item_text,
    _ordered_message_items,
    _turn_prompt,
)
from .test_native_claude_render_parity import (
    _CLAUDE_MOCK_MODEL,
    _focus_tui,
    _open_terminal_view,
    _send_composer_turn,
    _wait_terminal_connected,
)

_log = logging.getLogger(__name__)

# The add-rule field's footer on this Claude Code build; the field itself is
# titled "Add allow permission rule".
_DIALOG_FOOTER = ("Enter to submit", "Esc to cancel")
_RULE_EDITOR_TITLE = "permission rule"
_DIALOG_TIMEOUT_S = 40.0
# Several times the healthy mock-turn latency; a delivered message is answered
# within a few seconds.
_HOLD_WINDOW_S = 60.0
_RECOVERY_TIMEOUT_MS = 60_000
_ALLOW_RULE = "Bash(echo:*)"
# A sender-facing notice an acceptable fix may show instead of delivering, from
# workingIndicatorLabel(blockedOn="dialog open") in chatBubbleParts.tsx.
_DIALOG_NOTICE = "dialog in the terminal"


def _session(base_url: str, session_id: str) -> dict[str, object]:
    response = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    response.raise_for_status()
    return dict(response.json())


def _dump(dump_dir: Path, name: str, text: str) -> None:
    (dump_dir / f"{time.strftime('%H%M%S')}-{name}.txt").write_text(text, encoding="utf-8")


def _snapshot(dump_dir: Path, name: str, base_url: str, session_id: str) -> str:
    screen = read_terminal_screen(base_url, session_id)
    session = _session(base_url, session_id)
    items = [
        {"role": item.get("role"), "text": _item_text(item)[:120]}
        for item in _ordered_message_items(base_url, session_id)
    ]
    _dump(
        dump_dir,
        name,
        f"status={session.get('status')!r} prompt_visible={bridge._user_prompt_visible(screen)}\n"
        f"message items={json.dumps(items)}\n--- screen ---\n{screen}\n",
    )
    return screen


def _footer_matches(screen: str) -> bool:
    return all(hint in screen for hint in _DIALOG_FOOTER)


def _wait_for_screen(
    base_url: str,
    session_id: str,
    predicate: Callable[[str], bool],
    *,
    timeout_s: float,
    what: str,
) -> str:
    deadline = time.monotonic() + timeout_s
    screen = ""
    while time.monotonic() < deadline:
        screen = read_terminal_screen(base_url, session_id)
        if predicate(screen):
            return screen
        time.sleep(1.0)
    raise AssertionError(f"TUI never showed {what} within {timeout_s}s; last screen:\n{screen}")


def _open_add_rule_field(page, base_url: str, session_id: str, dump_dir: Path) -> str:
    """Open /permissions, focus the Allow list, and open the add-rule field.

    Leaves the dialog on the "Enter permission rule…" field, two Escapes deep.
    """
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _focus_tui(page)
    page.keyboard.type("/permissions", delay=40)
    page.wait_for_timeout(800)
    page.keyboard.press("Enter")
    _wait_for_screen(
        base_url,
        session_id,
        lambda s: "to switch" in s and "Esc to cancel" in s,
        timeout_s=_DIALOG_TIMEOUT_S,
        what="the /permissions dialog",
    )
    _snapshot(dump_dir, "01-permissions-dialog-open", base_url, session_id)
    page.keyboard.press("ArrowDown")
    page.wait_for_timeout(800)
    _snapshot(dump_dir, "02-allow-row-focused", base_url, session_id)
    page.keyboard.press("Enter")
    page.wait_for_timeout(800)
    page.keyboard.type(_ALLOW_RULE, delay=40)
    page.wait_for_timeout(800)
    screen = _wait_for_screen(
        base_url,
        session_id,
        lambda s: _footer_matches(s) and _RULE_EDITOR_TITLE in s,
        timeout_s=_DIALOG_TIMEOUT_S,
        what="the add-rule field",
    )
    _snapshot(dump_dir, "03-add-rule-field-open", base_url, session_id)
    return screen


def _send_from_web(page, text: str) -> bool:
    """Send *text* from the chat composer; steer it out of the queue if busy."""
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()
    queued = page.get_by_test_id("composer-queued-strip")
    try:
        expect(queued).to_be_visible(timeout=5_000)
    except AssertionError:
        return False
    queued.get_by_role("button", name="Send queued message now", exact=True).click()
    expect(queued).to_be_hidden(timeout=30_000)
    return True


def _reply_visible(page, token: str) -> bool:
    return page.locator(_ASSISTANT, has_text=token).count() > 0


@pytest.mark.nightly
@pytest.mark.timeout(600)
def test_web_message_is_not_held_behind_an_open_permissions_dialog(
    browser: Browser,
    native_claude_mock_session: tuple[str, str],
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """A message sent while the /permissions dialog is open is still delivered."""
    base_url, session_id = native_claude_mock_session
    dump_dir = tmp_path / "journey"
    dump_dir.mkdir()
    _log.info(
        "permissions-dialog journey: base_url=%s session_id=%s dumps=%s",
        base_url,
        session_id,
        dump_dir,
    )
    record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")

    (baseline_marker, baseline_token), (held_marker, held_token) = (
        (f"usr-{i + 1}-{uuid.uuid4().hex[:8]}", f"ast-{i + 1}-{uuid.uuid4().hex[:8]}")
        for i in range(2)
    )

    recording = (
        {"record_video_dir": record_dir, "record_video_size": {"width": 1280, "height": 720}}
        if record_dir
        else {}
    )
    context = browser.new_context(**recording)
    page = context.new_page()
    held = True
    labels: list[str] = []
    final_screen = ""
    footer_unchanged = False
    notice_shown = False
    try:
        page.goto(f"{base_url}/c/{session_id}")
        _open_terminal_view(page)
        _wait_terminal_connected(page)
        _ensure_chat_view(page)
        reset_mock_llm(mock_llm_server_url)

        started = time.monotonic()
        _send_composer_turn(
            page,
            mock_llm_server_url,
            base_url=base_url,
            session_id=session_id,
            index=1,
            user_marker=baseline_marker,
            assistant_token=baseline_token,
            expected_user_bubbles=1,
        )
        baseline_latency = time.monotonic() - started
        _dump(dump_dir, "00-baseline", f"baseline turn answered in {baseline_latency:.1f}s\n")
        _snapshot(dump_dir, "00-idle-after-baseline", base_url, session_id)

        _open_add_rule_field(page, base_url, session_id, dump_dir)

        _ensure_chat_view(page)
        set_fallback_mock_llm(mock_llm_server_url, "default", held_token)
        set_fallback_mock_llm(mock_llm_server_url, _CLAUDE_MOCK_MODEL, held_token)
        steered = _send_from_web(page, _turn_prompt(2, held_marker, held_token))
        sent_at = time.strftime("%Y-%m-%dT%H:%M:%S")

        deadline = time.monotonic() + _HOLD_WINDOW_S
        next_snapshot = time.monotonic()
        while time.monotonic() < deadline:
            if _reply_visible(page, held_token):
                held = False
                break
            working = page.locator(_WORKING)
            if working.count() > 0:
                label = working.first.inner_text().strip()
                if label and label not in labels:
                    labels.append(label)
            if any(_DIALOG_NOTICE in label.lower() for label in labels):
                break
            if time.monotonic() >= next_snapshot:
                elapsed = int(_HOLD_WINDOW_S - (deadline - time.monotonic()))
                _snapshot(dump_dir, f"10-hold-{elapsed:02d}s", base_url, session_id)
                next_snapshot = time.monotonic() + 20.0
            page.wait_for_timeout(1000)

        final_screen = _snapshot(dump_dir, "11-hold-final", base_url, session_id)
        footer_unchanged = _footer_matches(final_screen)
        notice_shown = any(_DIALOG_NOTICE in label.lower() for label in labels)
        if not held:
            expect(page.locator(_ASSISTANT, has_text=held_token).first).to_be_visible(
                timeout=10_000
            )
            # Hold the reply, then end on the terminal the runner backed out of,
            # so a recording of this journey shows the dialog gone as well.
            page.wait_for_timeout(2_000)
            _open_terminal_view(page)
            _wait_terminal_connected(page)
            page.wait_for_timeout(2_000)
        page.screenshot(path=str(dump_dir / "11-hold-final.png"), full_page=False)
        _dump(
            dump_dir,
            "12-hold-result",
            f"sent_at={sent_at} steered_from_queue={steered} held={held} "
            f"footer_still_matches={footer_unchanged} notice_shown={notice_shown}\n"
            f"working_labels={labels!r}\n",
        )
    finally:
        context.close()

    recovered = None
    if held:
        context = browser.new_context()
        page = context.new_page()
        try:
            page.goto(f"{base_url}/c/{session_id}")
            _open_terminal_view(page)
            _wait_terminal_connected(page)
            _focus_tui(page)
            for attempt in range(1, 4):
                page.keyboard.press("Escape")
                page.wait_for_timeout(1500)
                screen = _snapshot(dump_dir, f"20-after-escape-{attempt}", base_url, session_id)
                if not _footer_matches(screen):
                    break
            _ensure_chat_view(page)
            try:
                expect(page.locator(_ASSISTANT, has_text=held_token).first).to_be_visible(
                    timeout=_RECOVERY_TIMEOUT_MS
                )
                recovered = True
            except AssertionError:
                recovered = False
            _snapshot(dump_dir, "21-after-recovery", base_url, session_id)
            _dump(dump_dir, "22-recovery-result", f"delivered_after_escape={recovered}\n")
        finally:
            context.close()

    assert (not held) or notice_shown, (
        f"web message {held_marker!r} was held for {_HOLD_WINDOW_S:.0f}s behind the open "
        f"/permissions dialog with neither a reply nor a 'dialog in the terminal' notice "
        f"(dialog unchanged={footer_unchanged}, working labels={labels}); "
        f"delivered only after Esc: {recovered}"
    )
    if not held:
        assert not footer_unchanged, (
            f"the reply arrived but the /permissions dialog is still open:\n{final_screen}"
        )
