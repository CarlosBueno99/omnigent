"""E2E: a Claude sub-agent's approval stays reachable from its Codex parent's Terminal view."""

from __future__ import annotations

import secrets
import threading

import httpx
import pytest
from playwright.sync_api import expect

from tests._helpers.native_session import create_native_session
from tests._helpers.polling import poll_until

_PENDING_CARD = '[data-testid="approval-card"][data-state="pending"]'
_FULL_ACCESS_ARGS = ["--sandbox", "danger-full-access", "--ask-for-approval", "never"]


def _pending_ids(base_url: str, session_id: str) -> set[str]:
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    resp.raise_for_status()
    return {e.get("elicitation_id") for e in resp.json().get("pending_elicitations") or []}


def _park_bash_permission(base_url: str, session_id: str, elicitation_id: str, sink: dict) -> None:
    try:
        sink["resp"] = httpx.post(
            f"{base_url}/v1/sessions/{session_id}/hooks/permission-request",
            json={
                "hook_event_name": "PermissionRequest",
                "permission_mode": "default",
                "tool_name": "Bash",
                "tool_input": {"command": "git log --oneline upstream-master..HEAD | head -50"},
                "_omnigent_elicitation_id": elicitation_id,
            },
            timeout=120.0,
        )
    except Exception as exc:
        sink["error"] = exc


@pytest.mark.timeout(180)
def test_subagent_approval_is_answerable_from_parent_terminal_view(
    request: pytest.FixtureRequest,
    live_server: str,
) -> None:
    parent_id = create_native_session(
        httpx,
        live_server,
        harness="codex",
        metadata={"title": "Codex commit review", "terminal_launch_args": _FULL_ACCESS_ARGS},
    )["session_id"]
    child_id = create_native_session(
        httpx,
        live_server,
        harness="claude",
        metadata={"title": "Commit range review", "parent_session_id": parent_id},
    )["session_id"]

    elicitation_id = f"elicit_claude_{secrets.token_hex(16)}"
    sink: dict = {}
    worker = threading.Thread(
        target=_park_bash_permission,
        args=(live_server, child_id, elicitation_id, sink),
        daemon=True,
    )
    worker.start()
    try:
        poll_until(lambda: elicitation_id in _pending_ids(live_server, parent_id), timeout_s=30.0)

        page = request.getfixturevalue("page")
        page.goto(f"{live_server}/c/{parent_id}")
        page.get_by_test_id("view-mode-chat").click()
        expect(page.locator(_PENDING_CARD).first).to_be_visible(timeout=30_000)

        page.get_by_test_id("view-mode-terminal").click()
        expect(page.get_by_test_id("main-terminal-view")).to_have_attribute(
            "data-visible", "true", timeout=15_000
        )
        card = page.locator(f"{_PENDING_CARD}:visible").first
        expect(card).to_be_visible(timeout=10_000)
        expect(card).to_contain_text("Bash")
        card.get_by_role("button", name="Approve", exact=True).click()

        worker.join(timeout=30)
        assert "error" not in sink, sink.get("error")
        behavior = sink["resp"].json()["hookSpecificOutput"]["decision"]["behavior"]
        assert behavior == "allow"
    finally:
        if worker.is_alive():
            httpx.post(
                f"{live_server}/v1/sessions/{child_id}/elicitations/{elicitation_id}/resolve",
                json={"action": "decline"},
                timeout=15.0,
            )
            worker.join(timeout=30)
