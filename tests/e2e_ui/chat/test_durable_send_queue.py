"""A shared send queue completes in order with every browser closed."""

from __future__ import annotations

from pathlib import Path

import httpx
from playwright.sync_api import BrowserType, expect

from tests._helpers.browser import user_page
from tests._helpers.polling import poll_until


def test_shared_queue_runs_after_browsers_close(
    browser_type: BrowserType,
    browser_type_launch_args: dict,
    browser_context_args: dict,
    paused_mid_turn_session: tuple[str, str, str],
    tmp_path: Path,
) -> None:
    """Observe on desktop and phone, close both, then verify durable FIFO delivery."""
    base_url, session_id, mock_url = paused_mid_turn_session
    session_url = f"{base_url}/c/{session_id}"
    queue_url = f"{base_url}/v1/sessions/{session_id}/queue"
    messages = ["Queue first: review the changes.", "Queue second: summarize the result."]
    context_options = {**browser_context_args, "record_video_dir": str(tmp_path)}

    def persisted_messages() -> list[dict]:
        response = httpx.get(
            f"{base_url}/v1/sessions/{session_id}/items", params={"limit": 100}, timeout=10
        )
        response.raise_for_status()
        return [item for item in response.json()["data"] if item["type"] == "message"]

    def queued_texts() -> list[str]:
        response = httpx.get(queue_url, timeout=10)
        response.raise_for_status()
        return [item["data"]["content"][0]["text"] for item in response.json()["data"]]

    with (
        browser_type.launch(**browser_type_launch_args) as creator,
        browser_type.launch(**browser_type_launch_args) as observer,
        user_page(
            creator, headers={}, **{**context_options, "viewport": {"width": 1440, "height": 960}}
        ) as desktop,
        user_page(
            observer,
            headers={},
            **{
                **context_options,
                "viewport": {"width": 390, "height": 844},
                "is_mobile": True,
                "has_touch": True,
            },
        ) as phone,
    ):
        desktop.goto(session_url)
        phone.goto(session_url)
        composer = desktop.get_by_label("Message the agent")
        expect(composer).to_be_visible(timeout=30_000)
        composer.fill("Keep this turn open while I queue follow-ups.")
        desktop.get_by_role("button", name="Send", exact=True).click()
        poll_until(lambda: httpx.get(f"{mock_url}/gate/pending", timeout=5).json()["pending"])
        for message in messages:
            composer.fill(message)
            with desktop.expect_response(
                lambda response: response.request.method == "POST" and response.url == queue_url
            ) as accepted:
                desktop.get_by_role("button", name="Send", exact=True).click()
            assert accepted.value.ok and accepted.value.json().get("item_id")
        for page in (desktop, phone):
            strip = page.get_by_test_id("composer-queued-strip")
            for message in messages:
                expect(strip.get_by_text(message, exact=True)).to_be_visible(timeout=30_000)
        assert queued_texts() == messages
        assert all(message not in str(persisted_messages()) for message in messages)
        desktop.screenshot(path=str(tmp_path / "shared-queue-desktop.png"))
        phone.screenshot(path=str(tmp_path / "shared-queue-phone.png"))

    # No browser remains to flush a local queue or rescue a failed send.
    response = httpx.post(f"{mock_url}/gate/release", timeout=5)
    response.raise_for_status()
    assert response.json()["released"] is True
    poll_until(
        lambda: sum(item["role"] == "assistant" for item in persisted_messages()) >= 3,
        timeout_s=90,
    )
    delivered = [
        block["text"]
        for item in persisted_messages()
        if item["role"] == "user"
        for block in item["content"]
        if block.get("text") in messages
    ]
    assert delivered == messages
    assert [item["role"] for item in persisted_messages()] == ["user", "assistant"] * 3
    assert queued_texts() == []

    with (
        browser_type.launch(**browser_type_launch_args) as fresh,
        user_page(fresh, headers={}, **context_options) as page,
    ):
        page.goto(session_url)
        for reload in (False, True):
            if reload:
                page.reload()
            for message in messages:
                bubble = page.locator('[data-testid="message-bubble"][data-role="user"]').filter(
                    has_text=message
                )
                expect(bubble).to_have_count(1, timeout=30_000)
            expect(page.get_by_test_id("composer-queued-strip")).to_have_count(0)
        page.screenshot(path=str(tmp_path / "reopened-after-queue-completed.png"))
    assert queued_texts() == []
