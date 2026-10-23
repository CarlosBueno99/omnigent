"""Browser e2e: project config default reasoning effort for native harnesses.

Project settings (``web/src/shell/ProjectSettingsDialog.tsx``) store a default
model for Claude Code / Codex / Pi; this suite guards the sibling **Effort**
default. ``test_project_settings_effort_default_round_trips`` picks Claude
Code + "High" and checks the stored ``config.effort`` and the reopened dialog.
``test_project_effort_default_seeds_new_session`` starts a session from the
project's sidebar entry against a real host whose Claude Code CLI talks to the
mock model, and checks the composer label plus the created session's
persisted ``reasoning_effort``.

Ladder validation, explicit-pick precedence and the Codex per-model ladder are
covered in ``ProjectSettingsDialog.test.tsx`` and
``NewChatDialog.projectPrefill.test.tsx``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._helpers.host import isolated_host
from tests.e2e_ui.sessions.test_project_settings_model_default import (
    _create_project,
    _get_project_config,
    _open_project_settings,
    _pick_default_agent,
    _resolve_builtin_agent,
)

_CLAUDE_MOCK_MODEL = "claude-sonnet-4-20250514"


class _Beats:
    """Pause between journey steps and log their offsets when recording.

    Without a recording directory this is a no-op, so the suite stays fast.
    """

    def __init__(self, page: Page, name: str) -> None:
        self._page = page
        self._dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
        self._name = name
        self._start = time.monotonic()
        self._steps: list[dict[str, object]] = []

    def __call__(self, step: str) -> None:
        if not self._dir:
            return
        self._steps.append({"at": round(time.monotonic() - self._start, 2), "step": step})
        self._page.wait_for_timeout(2_000)
        Path(self._dir, f"{self._name}.steps.json").write_text(json.dumps(self._steps, indent=1))


def _pick_select_option(page: Page, trigger_test_id: str, label: str, beat: _Beats) -> None:
    """Open a Project settings Radix Select and choose the option *label*."""
    trigger = page.get_by_test_id(trigger_test_id)
    expect(trigger).to_be_enabled()
    trigger.click()
    option = page.get_by_role("option", name=label, exact=True)
    expect(option).to_be_visible()
    beat(f"{trigger_test_id} list open")
    option.click()
    expect(trigger).to_contain_text(label)


def _session_effort(base_url: str, session_id: str) -> str | None:
    """Return the session's persisted ``reasoning_effort`` (``None`` = unset)."""
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=15.0)
    resp.raise_for_status()
    effort = resp.json().get("reasoning_effort")
    return effort if isinstance(effort, str) else None


def test_project_settings_effort_default_round_trips(
    request: pytest.FixtureRequest,
    seeded_session: tuple[str, str],
) -> None:
    """Pick Claude Code, then Effort "High", Save, and reopen the dialog.

    The stored config must carry ``effort: "high"`` beside the agent, and the
    reopened dialog must seed the control back to "High".
    """
    base_url, session_id = seeded_session
    agent = _resolve_builtin_agent(base_url, "claude-native-ui")
    project = f"Project {uuid.uuid4().hex[:6]}"
    project_id = _create_project(base_url, project)

    page: Page = request.getfixturevalue("page")
    beat = _Beats(page, "cuj-1")
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_role("button", name=f"Project actions for {project}")).to_be_visible()
    beat("app loaded")

    _open_project_settings(page, project)
    beat("settings open")
    _pick_default_agent(page, agent["id"])

    effort = page.get_by_test_id("project-settings-effort")
    expect(effort).to_be_visible(timeout=5_000)
    expect(effort).to_contain_text("No default")
    beat("claude picked, effort field visible")
    _pick_select_option(page, "project-settings-effort", "High", beat)
    beat("high picked")
    page.get_by_test_id("project-settings-save").click()
    expect(page.get_by_test_id("project-settings-save")).to_have_count(0)

    config = _get_project_config(base_url, project_id)
    assert config.get("agent_id") == agent["id"], config
    assert config.get("effort") == "high", f"saved config carries no effort: {config!r}"

    beat("saved")

    _open_project_settings(page, project)
    expect(page.get_by_test_id("project-settings-effort")).to_contain_text("High")
    beat("reopened shows high")


@pytest.mark.timeout(600)
def test_project_effort_default_seeds_new_session(
    request: pytest.FixtureRequest,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """A new session started in the project comes up with the project's effort.

    Journey: the project's default agent is Claude Code with effort "high" →
    click "New session in <project>" in the sidebar → the composer's agent
    label shows "High" and the Effort menu has High checked → send a prompt →
    the created session persists ``reasoning_effort == "high"`` and its
    composer still shows "High".
    """
    if shutil.which("claude") is None or shutil.which("tmux") is None:
        pytest.skip("needs the claude CLI and tmux")
    agent = _resolve_builtin_agent(live_server, "claude-native-ui")
    project = f"Project {uuid.uuid4().hex[:6]}"
    project_id = _create_project(live_server, project)
    httpx.patch(
        f"{live_server}/v1/projects/{project_id}",
        json={"config": {"agent_id": agent["id"], "effort": "high"}},
        timeout=10.0,
    ).raise_for_status()
    providers = {
        "e2e-claude": {
            "kind": "key",
            "default": ["anthropic"],
            "anthropic": {
                "base_url": mock_llm_server_url,
                "api_key": "mock-key",
                "models": {"default": _CLAUDE_MOCK_MODEL},
            },
        }
    }
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with (
        httpx.Client(base_url=live_server, timeout=20) as client,
        isolated_host(
            tmp_path / "host",
            live_server,
            config={"providers": providers},
            env={"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"},
            cwd=workspace,
        ) as host,
    ):
        host.wait_online(client, timeout=120, poll_interval=0.5)
        created_id: str | None = None
        page: Page = request.getfixturevalue("page")
        reviewed_key = json.dumps(f"omnigent:imports-reviewed:{host.host_id}")
        page.add_init_script(f"localStorage.setItem({reviewed_key}, 'true')")
        beat = _Beats(page, "cuj-2")
        try:
            page.goto(f"{live_server}/")
            new_in_project = page.get_by_role("link", name=f"New session in {project}", exact=True)
            expect(new_in_project).to_be_visible(timeout=30_000)
            new_in_project.hover()
            beat("sidebar project entry")
            new_in_project.click()

            agent_picker = page.get_by_test_id("new-chat-landing-agent-select")
            expect(agent_picker).to_be_enabled(timeout=60_000)
            expect(agent_picker).to_have_attribute(
                "aria-label", re.compile("Claude Code"), timeout=30_000
            )
            expect(page.get_by_test_id("new-chat-landing-agent-effort-value")).to_have_text(
                "High", timeout=30_000
            )
            beat("composer shows high")

            # Show the seeded level in the composer's Effort menu.
            agent_picker.click()
            page.get_by_test_id(f"new-chat-landing-agent-config-{agent['id']}").click()
            high = page.get_by_test_id("new-chat-landing-agent-effort-high")
            expect(high).to_be_visible(timeout=30_000)
            expect(high).to_have_attribute("aria-checked", "true")
            beat("effort menu high checked")
            page.keyboard.press("Escape")
            page.keyboard.press("Escape")

            landing_input = page.get_by_test_id("new-chat-landing-input")
            landing_input.fill("Say hello.")
            beat("prompt typed")
            submit = page.get_by_test_id("new-chat-landing-submit")
            expect(submit).to_be_enabled(timeout=30_000)
            submit.click()
            page.wait_for_url(lambda url: "/c/" in url and "temp:" not in url, timeout=90_000)
            created_id = page.url.rstrip("/").split("/c/")[-1].split("?")[0]

            deadline = time.time() + 30
            while time.time() < deadline and _session_effort(live_server, created_id) is None:
                time.sleep(0.5)
            assert _session_effort(live_server, created_id) == "high"
            expect(page.get_by_test_id("composer-agent-effort-value")).to_have_text(
                "High", timeout=60_000
            )
            beat("created session shows high")
            beat("hold")
        finally:
            page.context.close()
            if created_id:
                client.delete(f"/v1/sessions/{created_id}")
