"""E2E: a claude-native ``/compact`` must survive an Omnigent restart on cold resume.

Journey: a terminal-first Claude Code session accumulates a few turns, the user
runs ``/compact`` from the web composer (the runner injects it into the Claude
TUI), chat keeps working, then the Omnigent server and runner restart. Reopening
the session and sending a message makes the runner cold-resume Claude with a
transcript rebuilt from the persisted Omnigent items. The resumed context must
start at the compaction summary, not carry the whole pre-compaction history back
(which, against a real model near its window, surfaces as "Context limit
reached").

The stack is test-owned (``tests/_helpers/server_runner.py``) so the restart is
a real process restart against the same database; the model is the mock
Anthropic endpoint, so the compaction summary text is scripted.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import textwrap
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.sync_api import Browser, Page, expect

from omnigent.harnesses.claude_native.bridge import (
    BRIDGE_ID_LABEL_KEY,
    bridge_dir_for_bridge_id,
)
from omnigent.testing.process_reaper import reap_leaked_omnigent_processes
from tests._helpers.live_server import terminate_process
from tests._helpers.native_session import create_native_session
from tests._helpers.server_runner import ServerRunner, server_runner
from tests._helpers.session import bind_session_runner
from tests.e2e_ui.conftest import reset_mock_llm, set_fallback_mock_llm
from tests.e2e_ui.messages.test_message_render_parity import (
    _ASSISTANT,
    _WORKING,
    _ensure_chat_view,
    _send,
)
from tests.e2e_ui.messages.test_native_claude_render_parity import (
    _open_terminal_view,
    _wait_terminal_connected,
)
from tests.helpers.ui_configuration import _CLAUDE_MOCK_MODEL

# Mock replies are instant; the budget covers Claude Code boot + terminal attach.
_TURN_TIMEOUT_MS = 120_000
_COMPACTION_TIMEOUT_S = 120.0
_BIND_TIMEOUT_S = 180.0
_PRE_TURNS = 3


def _runner_env(stack: ServerRunner, mock_url: str) -> dict[str, str | None]:
    """Provider config and Claude onboarding state for a runner-owned HOME.

    ``CLAUDE_CONFIG_DIR`` stays unset, as on a local install, so Claude Code
    and the cold-resume transcript rewrite agree on ``~/.claude/projects``.
    """
    config_home = stack.runner_home / ".omnigent-config"
    config_home.mkdir(parents=True, exist_ok=True)
    (config_home / "config.yaml").write_text(
        textwrap.dedent(
            f"""\
            providers:
              mock-claude:
                kind: key
                default: [anthropic]
                anthropic:
                  base_url: "{mock_url}"
                  api_key: "mock-key"
                  models:
                    default: {_CLAUDE_MOCK_MODEL}
            """
        )
    )
    (stack.runner_home / ".claude.json").write_text(
        json.dumps(
            {
                "hasCompletedOnboarding": True,
                "projects": {str(stack.workspace.resolve()): {"hasTrustDialogAccepted": True}},
            }
        )
    )
    return {
        "OMNIGENT_CONFIG_HOME": str(config_home),
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "CLAUDE_CONFIG_DIR": None,
        "ANTHROPIC_API_KEY": None,
        "ANTHROPIC_AUTH_TOKEN": None,
        "ANTHROPIC_BASE_URL": None,
        "ANTHROPIC_MODEL": None,
    }


def _create_bound_claude_session(client: httpx.Client, stack: ServerRunner) -> str:
    created = create_native_session(client, stack.base_url, harness="claude")
    session_id = str(created["session_id"])
    bind_session_runner(
        client.patch, stack.base_url, session_id, stack.runner_id, timeout=_BIND_TIMEOUT_S
    )
    return session_id


def _session(client: httpx.Client, base_url: str, session_id: str) -> dict[str, Any]:
    response = client.get(f"{base_url}/v1/sessions/{session_id}", timeout=30.0)
    response.raise_for_status()
    return response.json()


def _items(client: httpx.Client, base_url: str, session_id: str) -> list[dict[str, Any]]:
    response = client.get(
        f"{base_url}/v1/sessions/{session_id}/items",
        params={"limit": 500, "order": "asc"},
        timeout=30.0,
    )
    response.raise_for_status()
    return list(response.json()["data"])


def _claude_transcript_path(stack: ServerRunner, external_session_id: str) -> Path:
    project = re.sub(r"[^A-Za-z0-9]", "-", str(stack.workspace.resolve()))
    return stack.runner_home / ".claude" / "projects" / project / f"{external_session_id}.jsonl"


def _bridge_dir(session: dict[str, Any], session_id: str) -> Path:
    labels = session.get("labels") or {}
    return bridge_dir_for_bridge_id(labels.get(BRIDGE_ID_LABEL_KEY) or session_id)


def _copy_bridge_state(bridge_dir: Path, dest: Path) -> None:
    """Keep the forwarder's bridge files (private tmp) next to the stack logs."""
    dest.mkdir(parents=True, exist_ok=True)
    for path in bridge_dir.glob("*"):
        if path.is_file():
            shutil.copy2(path, dest / path.name)


def _tmux_advert(bridge_dir: Path) -> dict[str, str] | None:
    advert = bridge_dir / "tmux.json"
    if not advert.exists():
        return None
    return json.loads(advert.read_text(encoding="utf-8"))


def _kill_claude_pane(bridge_dir: Path) -> None:
    info = _tmux_advert(bridge_dir)
    if info and info.get("socket_path"):
        subprocess.run(
            ["tmux", "-S", info["socket_path"], "kill-server"],
            check=False,
            timeout=10,
            stderr=subprocess.DEVNULL,
        )


def _pane_text(bridge_dir: Path) -> str:
    info = _tmux_advert(bridge_dir)
    if not info:
        return ""
    proc = subprocess.run(
        ["tmux", "-S", info["socket_path"], "capture-pane", "-t", info["tmux_target"], "-p"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return proc.stdout if proc.returncode == 0 else ""


def _open_session(page: Page, base_url: str, session_id: str) -> None:
    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _ensure_chat_view(page)


def _send_turn(page: Page, mock_url: str, prompt: str, reply: str) -> None:
    set_fallback_mock_llm(mock_url, "default", reply)
    set_fallback_mock_llm(mock_url, _CLAUDE_MOCK_MODEL, reply)
    _send(page, prompt)
    expect(page.locator(_ASSISTANT, has_text=reply).first).to_be_visible(timeout=_TURN_TIMEOUT_MS)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=_TURN_TIMEOUT_MS)


def _compact_from_composer(
    page: Page,
    client: httpx.Client,
    base_url: str,
    session_id: str,
    mock_url: str,
    summary: str,
) -> dict[str, Any]:
    """Run ``/compact`` from the composer and return the persisted compaction item."""
    set_fallback_mock_llm(mock_url, "default", summary)
    set_fallback_mock_llm(mock_url, _CLAUDE_MOCK_MODEL, summary)
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    composer.click()
    composer.fill("/compact")
    composer.press("Enter")
    deadline = time.monotonic() + _COMPACTION_TIMEOUT_S
    while time.monotonic() < deadline:
        compactions = [
            item
            for item in _items(client, base_url, session_id)
            if item.get("type") == "compaction"
        ]
        if compactions:
            return compactions[-1]
        time.sleep(1.0)
    raise AssertionError("no compaction item was persisted after /compact")


def _restart_stack(
    stack: ServerRunner, runner_env: dict[str, str | None], bridge_dir: Path
) -> None:
    """Stop the runner and server (taking the Claude pane with them), then start both."""
    terminate_process(stack.runner)
    stack.runner = None
    _kill_claude_pane(bridge_dir)
    reap_leaked_omnigent_processes(stack.runner_home / ".omnigent")
    stack.restart_server()
    stack.start_runner(env=runner_env)


def _user_texts(request: dict[str, Any]) -> list[str]:
    texts: list[str] = []
    for message in request.get("messages", []):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            texts.extend(block.get("text", "") for block in content if isinstance(block, dict))
    return texts


def _request_for_marker(mock_url: str, marker: str) -> dict[str, Any]:
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        requests = httpx.get(f"{mock_url}/mock/requests", timeout=10.0).json()["requests"]
        hits = [
            request
            for request in requests
            if isinstance(request, dict) and any(marker in text for text in _user_texts(request))
        ]
        if hits:
            return hits[-1]
        time.sleep(1.0)
    raise AssertionError(f"the mock never received a request carrying {marker!r}")


@pytest.mark.nightly
@pytest.mark.timeout(900)
def test_compacted_history_stays_compacted_after_restart(
    browser: Browser,
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """Cold resume after a restart must not bring the compacted history back."""
    nonce = uuid.uuid4().hex[:8]
    pre_markers = [f"PRE-{index}-{nonce}" for index in range(1, _PRE_TURNS + 1)]
    post_marker, resume_marker = f"POST-1-{nonce}", f"RESUME-1-{nonce}"
    summary = f"SUMMARY-{nonce} the earlier turns were about context markers."
    reset_mock_llm(mock_llm_server_url)

    with (
        httpx.Client(trust_env=False) as client,
        server_runner(tmp_path / "stack") as stack,
    ):
        runner_env = _runner_env(stack, mock_llm_server_url)
        stack.start_runner(env=runner_env)
        session_id = _create_bound_claude_session(client, stack)
        base_url = stack.base_url
        bridge_dir = _bridge_dir(_session(client, base_url, session_id), session_id)
        filler = " ".join(f"detail-{index}" for index in range(40))

        timeline: list[tuple[str, float]] = []
        context = browser.new_context()
        started = time.monotonic()

        def mark(label: str) -> None:
            timeline.append((label, round(time.monotonic() - started, 2)))

        try:
            page = context.new_page()
            _open_session(page, base_url, session_id)
            mark("session open, terminal connected, chat view")
            for marker in pre_markers:
                _send_turn(
                    page,
                    mock_llm_server_url,
                    f"Context marker {marker}. {filler}",
                    f"ack-{marker}",
                )
                mark(f"turn {marker} answered")

            compaction = _compact_from_composer(
                page, client, base_url, session_id, mock_llm_server_url, summary
            )
            mark("/compact sent and compaction item persisted")
            _send_turn(
                page, mock_llm_server_url, f"Context marker {post_marker}.", f"ack-{post_marker}"
            )
            mark(f"turn {post_marker} answered")
            external_session_id = str(
                _session(client, base_url, session_id)["external_session_id"]
            )
            _copy_bridge_state(bridge_dir, stack.root / "bridge-before-restart")
            before_restart = _claude_transcript_path(stack, external_session_id).read_text(
                encoding="utf-8"
            )

            mark("restart begins")
            _restart_stack(stack, runner_env, bridge_dir)
            mark("server and runner back")

            # The next turn is what re-creates the Claude terminal (cold resume).
            page.goto(f"{base_url}/c/{session_id}")
            _ensure_chat_view(page)
            mark("session reopened")
            _send_turn(
                page,
                mock_llm_server_url,
                f"Context marker {resume_marker}.",
                f"ack-{resume_marker}",
            )
            mark(f"turn {resume_marker} answered")
            _open_terminal_view(page)
            _wait_terminal_connected(page)
            mark("terminal view connected")
            page.wait_for_timeout(6_000)
            (stack.root / "pane-after-resume.txt").write_text(_pane_text(bridge_dir))
            mark("end")
        finally:
            context.close()
            (stack.root / "timeline.json").write_text(json.dumps(timeline, indent=2))
            _copy_bridge_state(bridge_dir, stack.root / "bridge-after-resume")
            (stack.root / "mock-requests.json").write_text(
                json.dumps(httpx.get(f"{mock_llm_server_url}/mock/requests", timeout=10.0).json())
            )

        transcript = _claude_transcript_path(stack, external_session_id).read_text(
            encoding="utf-8"
        )
        (stack.root / "claude-transcript-before-restart.jsonl").write_text(before_restart)
        (stack.root / "compaction-item.json").write_text(json.dumps(compaction, indent=2))
        resumed_request = _request_for_marker(mock_llm_server_url, resume_marker)
        (stack.root / "resumed-model-request.json").write_text(
            json.dumps(resumed_request, indent=2)
        )
        resumed_texts = "\n".join(_user_texts(resumed_request))
        snapshot_text = json.dumps(compaction.get("compacted_messages"))

        leaked = [marker for marker in pre_markers if marker in resumed_texts]
        assert not leaked, (
            "cold resume sent the pre-compaction turns back to the model: "
            f"{leaked} present in the post-restart request; compaction snapshot carried "
            f"{[m for m in pre_markers if m in snapshot_text]}; rebuilt transcript carried "
            f"{[m for m in pre_markers if m in transcript]}; summary present in snapshot: "
            f"{summary.split()[0] in snapshot_text}"
        )
        assert summary.split()[0] in resumed_texts, (
            "the compaction summary was not in the resumed context"
        )
