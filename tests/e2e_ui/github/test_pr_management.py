"""Flagged PR linking through the real API, with no host or workspace."""

from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Browser, expect

from tests._helpers.browser import user_page
from tests.e2e_ui.collaboration._multi_user_server import (
    ADMIN_EMAIL,
    MultiUserServer,
    create_user_session,
    spawn_multi_user_server,
)
from tests.e2e_ui.conftest import open_right_rail


@pytest.fixture(scope="module")
def pr_server(
    built_spa: None, mock_llm_server_url: str, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[MultiUserServer]:
    yield from spawn_multi_user_server(
        mock_llm_server_url,
        tmp_path_factory.mktemp("pr-management"),
        extra_server_env={"OMNIGENT_FEATURES": "pull_request_management,canvas"},
    )


@pytest.mark.parametrize("mobile", [False, True])
def test_link_prs_without_workspace(
    browser: Browser, pr_server: MultiUserServer, tmp_path: Path, mobile: bool
) -> None:
    headers = {"X-Forwarded-Email": ADMIN_EMAIL}
    title = f"Manual PRs {'mobile' if mobile else 'desktop'}"
    session = create_user_session(pr_server.base_url, headers=headers, metadata={"title": title})
    other = create_user_session(pr_server.base_url, headers=headers)
    url = "https://github.com/acme/app/pull/7"
    other_path = f"{pr_server.base_url}/v1/sessions/{other}/resources/github/prs"
    response = httpx.post(other_path, headers=headers, json={"url": url, "action": "attach"})
    response.raise_for_status()
    with user_page(
        browser,
        headers=headers,
        viewport={"width": 390 if mobile else 1280, "height": 900},
        record_video_dir=str(tmp_path / "video"),
    ) as page:
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on(
            "console",
            lambda message: (
                errors.append(message.text) if "Maximum update depth" in message.text else None
            ),
        )
        page.goto(f"{pr_server.public_url}/c/{session}")
        if mobile:
            page.get_by_test_id("header-conversation-actions").click()
            page.get_by_role("menuitem", name="Pull Requests", exact=True).click()
            panel = page.get_by_test_id("github-panel-drawer")
        else:
            open_right_rail(page)
            panel = page.get_by_role("complementary", name="Workspace")
            panel.get_by_role("tab", name="Pull Requests").click()
        panel.get_by_role("button", name="Link a PR").click()
        panel.get_by_role("textbox", name="Pull request URL").fill(url)
        panel.get_by_role("button", name="Link", exact=True).click()
        expect(panel.get_by_role("list", name="Linked pull requests")).to_contain_text(
            "acme/app #7"
        )
        panel.get_by_role("button", name="Link a PR").click()
        panel.get_by_role("textbox", name="Pull request URL").fill(url.replace("/7", "/8"))
        panel.get_by_role("button", name="Link", exact=True).click()
        expect(panel.get_by_role("list", name="Linked pull requests")).to_contain_text(
            "acme/app #8"
        )
        page.screenshot(path=str(tmp_path / "linked-offline.png"))
        if not mobile:
            expect(page.get_by_test_id("composer-pr-link")).to_have_text("2 PRs")
            page.get_by_role("link", name=title, exact=True).hover()
            expect(page.get_by_test_id("session-tooltip-pr")).to_contain_text("2 linked PRs")
            page.goto(f"{pr_server.public_url}/canvas")
            card = page.get_by_test_id("session-card").filter(has_text=title)
            expect(card).to_contain_text("2 linked PRs")
            expect(card.get_by_role("link", name="Open pull request #7")).to_have_attribute(
                "href", url
            )
            page.screenshot(path=str(tmp_path / "linked-canvas.png"))
            page.goto(f"{pr_server.public_url}/c/{session}")
        page.reload()
        if mobile:
            page.get_by_test_id("header-conversation-actions").click()
            page.get_by_role("menuitem", name="Pull Requests", exact=True).click()
        expect(panel.get_by_role("list", name="Linked pull requests")).to_contain_text(
            "acme/app #7"
        )
        panel.get_by_role("button", name="Link a PR").click()
        panel.get_by_role("textbox", name="Pull request URL").fill(url.replace("/7", "/9"))
        panel.get_by_role("button", name="Link", exact=True).click()
        selected = panel.get_by_role("button", name="acme/app #8 Status unknown", exact=True)
        selected.click()
        panel.get_by_role("button", name="Unlink acme/app #9").click()
        expect(selected).to_have_attribute("aria-pressed", "true")
        panel.get_by_role("button", name="Unlink acme/app #7").click()
        expect(panel.get_by_role("list", name="Linked pull requests")).not_to_contain_text(
            "acme/app #7"
        )
        panel.get_by_role("button", name="Unlink acme/app #8").click()
        expect(panel.get_by_role("list", name="Linked pull requests")).to_contain_text(
            "No linked pull requests"
        )
        assert httpx.get(other_path, headers=headers).json()["prs"][0]["url"] == url
        path = f"{pr_server.base_url}/v1/sessions/{session}/resources/github/prs"
        assert httpx.get(path, headers=headers).json()["prs"] == []
        assert errors == []
    grant = httpx.put(
        f"{pr_server.base_url}/v1/sessions/{session}/permissions",
        headers=headers,
        json={"user_id": "reader@ui.test", "level": 1},
    )
    grant.raise_for_status()
    reader = {"X-Forwarded-Email": "reader@ui.test"}
    assert httpx.get(path, headers=reader).status_code == 200
    assert (
        httpx.post(path, headers=reader, json={"url": url, "action": "attach"}).status_code == 403
    )
