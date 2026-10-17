"""An Omnigent model switch in a claude-native pane must keep the user's Claude default."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from urllib.parse import urlparse

import pytest
from playwright.sync_api import expect

from tests.e2e_ui.conftest import _server_state, native_claude_mock_session_context
from tests.e2e_ui.messages.test_message_render_parity import _ensure_chat_view
from tests.e2e_ui.messages.test_native_claude_render_parity import (
    _open_terminal_view,
    _pane_text,
    _wait_terminal_connected,
)
from tests.helpers.ui_configuration import _CLAUDE_MOCK_MODEL

pytestmark = pytest.mark.timeout(300)

_PICKER_MODELS = {"default": _CLAUDE_MOCK_MODEL, "haiku": "claude-3-5-haiku-20241022"}
_SWITCH_SETTLE_S = 20.0


def _claude_user_settings_path() -> Path:
    # The runner shares this process's Claude config dir (same CLAUDE_CONFIG_DIR when prepared).
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    return Path(config_dir) / "settings.json"


def _saved_model(settings_path: Path) -> str | None:
    if not settings_path.exists():
        return None
    return json.loads(settings_path.read_text(encoding="utf-8") or "{}").get("model")


def test_web_model_picker_switch_keeps_the_users_claude_default_model(
    request: pytest.FixtureRequest,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    settings_path = _claude_user_settings_path()
    original_bytes = settings_path.read_bytes() if settings_path.exists() else None
    try:
        with native_claude_mock_session_context(
            live_server, mock_llm_server_url, tmp_path_factory, claude_models=_PICKER_MODELS
        ) as (base_url, session_id):
            page = request.getfixturevalue("page")
            page.goto(f"{base_url}/c/{session_id}")
            _open_terminal_view(page)
            _wait_terminal_connected(page)
            _ensure_chat_view(page)

            page.get_by_test_id("composer-config-gear").click()
            page.get_by_test_id("composer-agent-edit").click()
            rows = page.locator('[data-testid^="composer-agent-model-"][role="menuitemcheckbox"]')
            expect(rows.first).to_be_visible(timeout=30_000)
            unchecked = page.locator(
                '[data-testid^="composer-agent-model-"][role="menuitemcheckbox"][aria-checked="false"]'
            )
            if unchecked.count() == 0 and _server_state.get("workflow_owned"):
                pytest.skip("the prepared provider offers no second Claude model to switch to")
            target = unchecked.last
            target_id = target.get_attribute("data-testid") or ""
            model_before = _saved_model(settings_path)
            print("settings before switch:", settings_path, repr(model_before))

            with page.expect_response(
                lambda response: (
                    response.request.method == "PATCH"
                    and urlparse(response.url).path == f"/v1/sessions/{session_id}"
                ),
                timeout=90_000,
            ) as switching:
                target.click()
            print("switch to", repr(target_id), "PATCH", switching.value.status)
            assert switching.value.ok, switching.value.text()

            deadline = time.monotonic() + _SWITCH_SETTLE_S
            while time.monotonic() < deadline and _saved_model(settings_path) == model_before:
                time.sleep(0.5)
            model_after = _saved_model(settings_path)
            print("settings after switch:", repr(model_after))
            assert model_after == model_before, (
                f"switching the session to {target_id!r} rewrote the user's Claude default model "
                f"in {settings_path}: {model_before!r} -> {model_after!r}\n"
                f"pane:\n{_pane_text(base_url, session_id)}"
            )
    finally:
        if original_bytes is None:
            settings_path.unlink(missing_ok=True)
        else:
            settings_path.write_bytes(original_bytes)
