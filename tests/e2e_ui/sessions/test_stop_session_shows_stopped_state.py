"""Browser e2e: stopping a host-bound session from the sidebar visibly marks it stopped.

The sidebar kebab offers "Stop session" only for host-spawned (or claude/devin-
native) sessions, so this test brings a real ``omnigent host`` daemon online
against the live server and creates the session on it. After the confirm the
runner is torn down (the control), and the open session view is expected to
say the session is stopped; without a stopped affordance the page stays
identical to a running session.
"""

from __future__ import annotations

import os
import re
import time
import uuid
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
import yaml
from playwright.sync_api import Page, expect

from tests._helpers.host import isolated_host
from tests._helpers.session import bundle_files, post_session_bundle

_HOST_ONLINE_TIMEOUT_S = 120.0
_RUNNER_BOOT_TIMEOUT_S = 120.0
_STOP_LANDS_TIMEOUT_S = 30.0
# Liveness is poll-driven (~10 s per poll); allow several polls after the stop lands.
_STOPPED_INDICATION_TIMEOUT_MS = 40_000
_STOPPED_TEXT = re.compile(r"\bstopped\b", re.IGNORECASE)
_HOST_BADGE_NAME = re.compile(r"^Host .+, (online|offline)$")


def _agent_bundle(mock_llm_server_url: str) -> bytes:
    spec = {
        "name": "hello_world",
        "prompt": "You are a friendly assistant. Say hello and answer questions.",
        "executor": {
            "harness": "openai-agents",
            "model": "gpt-4o-mini",
            "auth": {
                "type": "api_key",
                "api_key": "mock-key",
                "base_url": f"{mock_llm_server_url}/v1",
            },
        },
        "os_env": {"type": "caller_process", "cwd": ".", "sandbox": {"type": "none"}},
    }
    return bundle_files({"agent.yaml": yaml.safe_dump(spec).encode()})


def _runner_online(client: httpx.Client, session_id: str) -> bool | None:
    response = client.get("/health", params={"session_id": session_id})
    response.raise_for_status()
    return response.json().get("session", {}).get("runner_online")


def _wait_for(check: Callable[[], bool], description: str, timeout: float) -> float:
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if check():
            return time.monotonic() - start
        time.sleep(0.5)
    raise AssertionError(f"Timed out after {timeout:.0f}s waiting for {description}")


def _mark(label: str, **data: object) -> None:
    print(f"TIMELINE {time.time():.3f} {label} {data}", flush=True)


def _visible_state(page: Page) -> dict[str, object]:
    badge = page.get_by_role("button", name=_HOST_BADGE_NAME)
    composer = page.get_by_label("Message the agent")
    return {
        "composer_placeholder": composer.get_attribute("placeholder"),
        "host_badge": badge.first.get_attribute("aria-label") if badge.count() else None,
        "visible_stopped_matches": page.get_by_text(_STOPPED_TEXT).filter(visible=True).count(),
    }


@pytest.mark.timeout(480)
def test_stop_session_shows_stopped_state(
    request: pytest.FixtureRequest,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    base_env = {
        "PATH": os.environ["PATH"],
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }
    with (
        httpx.Client(
            base_url=live_server,
            trust_env=False,
            timeout=30.0,
            headers={"x-omnigent-background-session-titles": "off"},
        ) as client,
        isolated_host(
            tmp_path / "host",
            live_server,
            base_env=base_env,
            env={"OMNIGENT_SKIP_ONBOARD": "1", "OMNIGENT_NO_UPDATE_CHECK": "1"},
            name=f"stop-feedback-{uuid.uuid4().hex[:8]}",
        ) as host,
    ):
        host_row = host.wait_online(client, timeout=_HOST_ONLINE_TIMEOUT_S, poll_interval=1)
        _mark("host_online", host_id=host.host_id, name=host_row.get("name"))
        created = post_session_bundle(
            client.post,
            "/v1/sessions",
            _agent_bundle(mock_llm_server_url),
            metadata={
                "host_id": host.host_id,
                "workspace": str(workspace),
                "title": "Stop feedback repro",
            },
        )
        created.raise_for_status()
        session_id = created.json()["session_id"]
        boot_s = _wait_for(
            lambda: _runner_online(client, session_id) is True,
            "the host-spawned runner to come online",
            _RUNNER_BOOT_TIMEOUT_S,
        )
        _mark("session_created", session_id=session_id, runner_online_after_s=round(boot_s, 1))

        # The recorded page starts only after the non-browser setup is complete.
        page: Page = request.getfixturevalue("page")
        _mark("page_created")
        try:
            with page.expect_response(
                lambda r: "/health" in r.url and "session_id" in r.url and r.status == 200,
                timeout=30_000,
            ):
                page.goto(f"{live_server}/c/{session_id}")
            expect(page.get_by_placeholder("Send a message…")).to_be_visible(timeout=30_000)
            expect(page.get_by_role("button", name=_HOST_BADGE_NAME)).to_be_visible(timeout=30_000)
            page.wait_for_timeout(2_000)
            _mark("session_running", **_visible_state(page))

            row = page.locator(f'li[data-sidebar-session-id="{session_id}"]')
            expect(row).to_be_visible()
            row.hover()
            row.get_by_test_id("conversation-actions").click()
            _mark("kebab_open")
            page.get_by_test_id("stop-conversation").click()
            expect(page.get_by_role("dialog")).to_contain_text("Stop session?")
            page.wait_for_timeout(1_500)
            _mark("dialog_open")
            with page.expect_response(
                lambda r: (
                    r.url.endswith(f"/v1/sessions/{session_id}/events")
                    and r.request.method == "POST"
                    and (r.request.post_data_json or {}).get("type") == "stop_session"
                )
            ) as stop:
                page.get_by_test_id("stop-session-confirm").click()
            assert stop.value.ok, f"stop_session failed: {stop.value.status} {stop.value.text()}"
            expect(page.get_by_role("dialog")).to_have_count(0)
            _mark("stop_confirmed", status=stop.value.status)

            page.wait_for_timeout(5_000)
            _mark("five_seconds_after_confirm", **_visible_state(page))

            torn_down_s = _wait_for(
                lambda: _runner_online(client, session_id) is False,
                "the runner to be torn down after Stop session",
                _STOP_LANDS_TIMEOUT_S,
            )
            _mark("runner_torn_down", after_confirm_s=round(torn_down_s + 5, 1))

            stopped_indication = page.get_by_text(_STOPPED_TEXT).filter(visible=True)
            expect(stopped_indication).not_to_have_count(0, timeout=_STOPPED_INDICATION_TIMEOUT_MS)
        finally:
            if not page.is_closed():
                _mark("final_state", **_visible_state(page))
            # Finalize the recording on the asserted state, before the host shuts down.
            page.context.close()
