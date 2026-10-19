"""PR links survive the real runner stopping and a fresh server process.

Own environment: the test stops its fixture runner and restarts its server.
The model and gh creation response are scripted; the tool, tracking, API,
database, browser, and process lifecycle are real.
"""

from __future__ import annotations

import json
import os
import signal
import time
from collections.abc import Callable
from pathlib import Path
from typing import cast

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    _server_state,
    configure_mock_llm,
    open_right_rail,
    set_fallback_mock_llm,
)
from tests.e2e_ui.github.test_pr_read_comment_no_association import (
    _CREATE_COMMAND,
    _PR_URL,
    pr_probe_runner_id,  # noqa: F401
    pr_probe_session,  # noqa: F401
)


@pytest.mark.browser_context_args(record_video_size={"width": 1280, "height": 720})
@pytest.mark.parametrize("mobile", [False, True], ids=["desktop", "mobile"])
def test_created_pr_survives_runner_and_server_shutdown(
    page: Page,
    pr_probe_session: tuple[str, str, str, Path, Path],  # noqa: F811
    mock_llm_server_url: str,
    _recover_shared_runner: Callable[[], None],
    mobile: bool,
    tmp_path: Path,
) -> None:
    base_url, session_id, model, stub, worktree = pr_probe_session
    command = _CREATE_COMMAND.format(stub=stub, worktree=worktree)
    reply = "Created the pull request."
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "create_pr",
                        "name": "sys_os_shell",
                        "arguments": json.dumps({"command": command}),
                    }
                ]
            },
            {"text": reply},
        ],
        key=model,
    )
    set_fallback_mock_llm(mock_llm_server_url, model, "Done.")
    httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={
            "type": "message",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": "Create a pull request."}],
            },
        },
        timeout=30,
    ).raise_for_status()
    deadline = time.monotonic() + 60
    while True:
        snapshot = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10).json()
        if reply in json.dumps(snapshot) and snapshot["status"] == "idle":
            break
        assert time.monotonic() < deadline, json.dumps(snapshot, indent=2)
        time.sleep(0.1)
    # Captured before any browser or PR-resource request can backfill metadata.
    reported_prs = snapshot.get("pull_requests")
    if mobile:
        page.set_viewport_size({"width": 390, "height": 844})
    else:
        info = httpx.get(f"{base_url}/v1/info").json()
        info["features"] = {
            **info.get("features", {}),
            "canvas": True,
            "sidebar_grouping_ui": True,
        }
        page.route("**/v1/info", lambda route: route.fulfill(json=info))
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_test_id("composer-pr-link")).to_have_accessible_name("#42")
    if not mobile:
        open_right_rail(page)
        page.get_by_role("tab", name="Pull Requests").click()
        expect(page.get_by_role("combobox", name="Session pull request")).to_be_visible()
    page.screenshot(path=str(tmp_path / "online.png"))

    os.kill(int(_server_state["runner_pid"]), signal.SIGKILL)
    try:
        deadline = time.monotonic() + 30
        while (
            httpx.get(f"{base_url}/health", params={"session_id": session_id})
            .json()
            .get("session", {})
            .get("runner_online")
        ):
            assert time.monotonic() < deadline
            time.sleep(0.1)
        # Navigation discards the QueryClient and its previously fetched PR data.
        page.goto("about:blank")
        page.goto(f"{base_url}/c/{session_id}")
        chip = page.get_by_test_id("composer-pr-link")
        expect(chip).to_have_accessible_name("#42", timeout=30_000)
        chip.click()
        link = page.get_by_role("link", name="Open the PR on GitHub")
        expect(link).to_have_attribute("href", _PR_URL)
        expect(page.get_by_text("The workspace is offline.", exact=False)).to_be_visible()
        assert reported_prs and reported_prs[0]["url"] == _PR_URL
        page.screenshot(path=str(tmp_path / "runner-offline.png"))

        cast(Callable[..., None], _server_state["restart_server"])(wait_for_runner=False)
        page.reload()
        expect(page.get_by_test_id("composer-pr-link")).to_have_accessible_name("#42")
        page.get_by_test_id("composer-pr-link").click()
        expect(page.get_by_role("link", name="Open the PR on GitHub")).to_have_attribute(
            "href", _PR_URL
        )
        response = httpx.get(f"{base_url}/v1/sessions/{session_id}/resources/github")
        response.raise_for_status()
        assert response.json()["prs"][0]["url"] == _PR_URL
        page.screenshot(path=str(tmp_path / "server-restarted.png"))
        if not mobile:
            page.get_by_test_id("sidebar-conversation-list").get_by_text(
                "Create a pull request.", exact=True
            ).hover()
            expect(page.get_by_test_id("session-tooltip-pr")).to_contain_text("#42")
            page.screenshot(path=str(tmp_path / "sidebar.png"))
            page.goto(f"{base_url}/canvas")
            canvas_link = page.get_by_test_id("session-card").get_by_role(
                "link", name="Open pull request #42"
            )
            expect(canvas_link).to_have_attribute("href", _PR_URL)
            page.screenshot(path=str(tmp_path / "canvas.png"))
    finally:
        _recover_shared_runner()
