"""The "Open sub-agents in the Workspace panel" setting, driven in the browser.

The child is a real sub-agent session whose model call is parked on the mock
LLM's gate, so it is genuinely working while the browser picks it.
"""

from __future__ import annotations

import contextlib
import re
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

from omnigent.entities import NewConversationItem, ResourceEventData
from tests._helpers.session import bind_session_runner
from tests.e2e_ui.conftest import (
    configure_mock_llm,
    seed_committed_items,
    seed_committed_turn,
)

_SETTING_KEY = "omnigent:open-subagents-in-panel"
_CHILD_NAME = "auth-audit"
_PARENT_PROMPT = "Audit the login flow and tell me what you find."
_PARENT_REPLY = "I delegated the audit to a researcher sub-agent."
_CHILD_REPLY = "Audit finished: refresh tokens rotate correctly."
_FOLLOWUP_REPLY = "Yes, the logout path revokes the refresh token too."
_VIEWPORT = {"width": 1440, "height": 900}

pytestmark = pytest.mark.browser_context_args(viewport=_VIEWPORT, record_video_size=_VIEWPORT)


@dataclass(frozen=True)
class BusySubagent:
    base_url: str
    parent_id: str
    child_id: str
    mock_url: str
    nonce: str


def _child_busy(base_url: str, parent_id: str, child_id: str) -> bool:
    response = httpx.get(f"{base_url}/v1/sessions/{parent_id}/child_sessions", timeout=10.0)
    response.raise_for_status()
    payload = response.json()
    children = payload.get("data", payload) if isinstance(payload, dict) else payload
    return any(child.get("id") == child_id and child.get("busy") for child in children)


def _release_gate(mock_url: str) -> None:
    response = httpx.post(f"{mock_url}/gate/release", timeout=5.0)
    response.raise_for_status()


def _send_user_message(base_url: str, session_id: str, text: str) -> None:
    httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": text}]},
        },
        timeout=30.0,
    ).raise_for_status()


@pytest.fixture
def busy_subagent(
    seeded_session: tuple[str, str],
    runner_id: str,
    mock_llm_server_url: str,
) -> Iterator[BusySubagent]:
    """A parent with one researcher child whose first turn is held on the mock gate."""
    base_url, parent_id = seeded_session
    nonce = uuid.uuid4().hex[:8]
    seed_committed_turn(parent_id, prompt=_PARENT_PROMPT, reply=_PARENT_REPLY)
    agent_id = httpx.get(f"{base_url}/v1/sessions/{parent_id}", timeout=10.0).json()["agent_id"]
    created = httpx.post(
        f"{base_url}/v1/sessions",
        json={
            "agent_id": agent_id,
            "parent_session_id": parent_id,
            "sub_agent_name": "researcher",
            "title": f"researcher:{_CHILD_NAME}",
        },
        timeout=30.0,
    )
    created.raise_for_status()
    child_id = created.json()["id"]
    try:
        bind_session_runner(httpx.patch, base_url, child_id, runner_id, timeout=10.0)
        seed_committed_items(
            parent_id,
            [
                NewConversationItem(
                    type="resource_event",
                    response_id=f"subagent_{nonce}",
                    data=ResourceEventData(
                        event_type="session.subagent.delegated",
                        resource_id=child_id,
                        resource_type="session",
                        resource={"title": _CHILD_NAME},
                    ),
                )
            ],
        )
        configure_mock_llm(
            mock_llm_server_url,
            [{"text": _CHILD_REPLY, "block": True}],
            match=f"audit-{nonce}",
        )
        configure_mock_llm(
            mock_llm_server_url,
            [{"text": _FOLLOWUP_REPLY}] * 3,
            match=f"followup-{nonce}",
        )
        _send_user_message(base_url, child_id, f"audit-{nonce}: check token refresh")
        deadline = time.monotonic() + 60
        while not _child_busy(base_url, parent_id, child_id):
            assert time.monotonic() < deadline, "the sub-agent never started working"
            time.sleep(0.5)
        yield BusySubagent(base_url, parent_id, child_id, mock_llm_server_url, nonce)
    finally:
        # A turn left parked on the gate would wedge the shared runner.
        with contextlib.suppress(httpx.HTTPError):
            _release_gate(mock_llm_server_url)
        httpx.delete(f"{base_url}/v1/sessions/{child_id}", timeout=10.0)


def _open_page(request: pytest.FixtureRequest, *, setting_on: bool) -> Page:
    """Create the recorded page only after all server-side setup is done."""
    page: Page = request.getfixturevalue("page")
    if setting_on:
        page.add_init_script(f"window.localStorage.setItem('{_SETTING_KEY}', 'on')")
    return page


def _open_general_settings(page: Page, base_url: str) -> Locator:
    page.goto(f"{base_url}/settings/general")
    toggle = page.get_by_test_id("open-subagents-in-panel-toggle")
    expect(toggle).to_be_visible(timeout=30_000)
    toggle.scroll_into_view_if_needed()
    return toggle


def _pick_from_composer_indicator(page: Page) -> None:
    page.get_by_test_id("subagent-task-pill").first.click()
    popover = page.get_by_role("dialog")
    row = popover.get_by_role("link", name=re.compile(_CHILD_NAME))
    expect(row).to_be_visible()
    expect(popover).to_contain_text("Working")
    page.wait_for_timeout(800)
    row.click()


def _workspace(page: Page) -> Locator:
    return page.get_by_role("complementary", name="Workspace")


def test_composer_indicator_opens_subagent_tab_beside_parent(
    request: pytest.FixtureRequest,
    busy_subagent: BusySubagent,
) -> None:
    """CUJ-1: turn the setting on, pick the sub-agent, and chat with it in the tab."""
    sub = busy_subagent
    page = _open_page(request, setting_on=False)

    toggle = _open_general_settings(page, sub.base_url)
    expect(toggle).to_have_attribute("aria-checked", "false")
    page.wait_for_timeout(800)
    toggle.click()
    expect(toggle).to_have_attribute("aria-checked", "true")
    page.wait_for_timeout(800)

    page.goto(f"{sub.base_url}/c/{sub.parent_id}")
    expect(page.get_by_text(_PARENT_REPLY, exact=True)).to_be_visible(timeout=30_000)
    _pick_from_composer_indicator(page)

    parent_path = re.compile(rf"/c/{re.escape(sub.parent_id)}$")
    expect(page).to_have_url(parent_path)
    rail = _workspace(page)
    expect(rail).to_be_visible()
    expect(rail.get_by_role("tab", name=_CHILD_NAME, exact=True)).to_have_attribute(
        "aria-selected", "true"
    )
    pane = rail.get_by_test_id("subagent-tab-pane")
    expect(pane).to_contain_text(f"audit-{sub.nonce}", timeout=30_000)
    expect(pane.get_by_test_id("subagent-tab-open-full")).to_have_attribute(
        "href", f"/c/{sub.child_id}"
    )
    # The parent transcript stays in the main column.
    expect(page.get_by_text(_PARENT_REPLY, exact=True)).to_be_visible()
    page.wait_for_timeout(1000)

    _release_gate(sub.mock_url)
    expect(pane.get_by_text(_CHILD_REPLY, exact=True)).to_be_visible(timeout=60_000)
    page.wait_for_timeout(1000)

    composer = pane.get_by_role("textbox", name="Reply to this session...")
    expect(composer).to_be_enabled(timeout=30_000)
    composer.click()
    composer.press_sequentially(f"followup-{sub.nonce}: does logout revoke it?", delay=25)
    pane.get_by_role("button", name="Send message", exact=True).click()
    expect(pane.get_by_text(_FOLLOWUP_REPLY, exact=True)).to_be_visible(timeout=60_000)
    expect(page).to_have_url(parent_path)
    page.wait_for_timeout(1500)


def test_transcript_and_agents_tab_reuse_one_tab_and_close_keeps_child_running(
    request: pytest.FixtureRequest,
    busy_subagent: BusySubagent,
) -> None:
    """CUJ-2: the notice and Agents row share one tab; closing it does not stop the child."""
    sub = busy_subagent
    page = _open_page(request, setting_on=True)

    page.goto(f"{sub.base_url}/c/{sub.parent_id}")
    notice = page.get_by_test_id("subagent-activity")
    expect(notice).to_contain_text(f"Started {_CHILD_NAME}", timeout=30_000)
    page.wait_for_timeout(800)
    notice.get_by_role("link", name=_CHILD_NAME, exact=True).click()

    parent_path = re.compile(rf"/c/{re.escape(sub.parent_id)}$")
    expect(page).to_have_url(parent_path)
    rail = _workspace(page)
    child_tab = rail.get_by_role("tab", name=_CHILD_NAME, exact=True)
    expect(child_tab).to_have_attribute("aria-selected", "true")
    expect(rail.get_by_test_id("subagent-tab-pane")).to_contain_text(
        f"audit-{sub.nonce}", timeout=30_000
    )
    page.wait_for_timeout(1000)

    agents_tab = rail.get_by_role("tab", name=re.compile("^Agents"))
    agents_tab.click()
    row = rail.locator(f'[data-child-session-id="{sub.child_id}"]')
    expect(row).to_be_visible(timeout=30_000)
    page.wait_for_timeout(800)
    row.click()
    expect(child_tab).to_have_attribute("aria-selected", "true")
    expect(child_tab).to_have_count(1)
    expect(page).to_have_url(parent_path)
    page.wait_for_timeout(1000)

    rail.get_by_role("button", name=f"Close {_CHILD_NAME}", exact=True).click()
    expect(child_tab).to_have_count(0)
    agents_tab.click()
    expect(row.get_by_test_id("subagent-status-avatar")).to_have_attribute(
        "data-activity", "working"
    )
    assert _child_busy(sub.base_url, sub.parent_id, sub.child_id)
    page.wait_for_timeout(1500)


def test_setting_off_keeps_full_screen_navigation(
    request: pytest.FixtureRequest,
    busy_subagent: BusySubagent,
) -> None:
    """CUJ-3: with the default setting, picking a sub-agent replaces the main chat."""
    sub = busy_subagent
    page = _open_page(request, setting_on=False)

    toggle = _open_general_settings(page, sub.base_url)
    expect(toggle).to_have_attribute("aria-checked", "false")
    page.wait_for_timeout(1200)

    page.goto(f"{sub.base_url}/c/{sub.parent_id}")
    expect(page.get_by_text(_PARENT_REPLY, exact=True)).to_be_visible(timeout=30_000)
    _pick_from_composer_indicator(page)

    expect(page).to_have_url(re.compile(rf"/c/{re.escape(sub.child_id)}"))
    expect(page.get_by_role("link", name="Back to parent session")).to_be_visible(timeout=30_000)
    expect(page.get_by_text(_PARENT_REPLY, exact=True)).to_have_count(0)
    expect(page.get_by_test_id("subagent-tab-pane")).to_have_count(0)
    page.wait_for_timeout(1500)
