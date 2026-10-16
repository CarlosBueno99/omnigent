"""A codex (SDK) turn throttled with HTTP 429 must fail as a rate limit and offer Retry.

The real codex app-server exhausts its own retries against the mock ``/v1/responses`` and
reports ``exceeded retry limit, last status: 429``: that text-only failure must reach the
web UI as ``rate_limit_exceeded`` without launch-time stderr, and Retry must then work.
"""

from __future__ import annotations

import re
import shutil
import time
import uuid
from typing import Any

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._helpers.session import bind_session_runner, post_session_bundle
from tests.e2e_ui.conftest import (
    _ensure_runner_online,
    _server_state,
    configure_mock_llm,
    reset_mock_llm,
)
from tests.e2e_ui.sessions.test_codex_multistep_turn_usage import _build_codex_bundle, _send

pytestmark = pytest.mark.skipif(
    shutil.which("codex") is None,
    reason="the codex CLI binary is required for the headless-codex rate-limit e2e",
)

_RATE_LIMIT_HEADLINE = "The model's rate limit was reached. You can retry this turn."
_RECOVERED_TEXT = "Recovered after the rate limit cleared."
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
# Codex retries a 429 a few times per turn; queue enough refusals for all of them.
_RATE_LIMIT_RESPONSES = 16
_TURN_SETTLE_TIMEOUT_S = 180.0


def _error_fields(item: dict[str, Any]) -> dict[str, Any]:
    nested = item.get("data")
    return nested if isinstance(nested, dict) else item


def _settled_turn_error(base_url: str, session_id: str) -> dict[str, Any]:
    """Poll the transcript until the turn persists a failure item."""
    deadline = time.monotonic() + _TURN_SETTLE_TIMEOUT_S
    while time.monotonic() < deadline:
        resp = httpx.get(
            f"{base_url}/v1/sessions/{session_id}/items",
            params={"limit": 200, "order": "asc"},
            timeout=10.0,
        )
        resp.raise_for_status()
        for item in resp.json().get("data", []):
            fields = _error_fields(item)
            if item.get("type") == "error" and str(fields.get("level") or "") != "info":
                return fields
        time.sleep(0.5)
    raise AssertionError(f"no error item persisted within {_TURN_SETTLE_TIMEOUT_S:.0f}s")


@pytest.mark.timeout(600)
def test_codex_rate_limited_turn_offers_retry(
    request: pytest.FixtureRequest,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    try:
        runner_id = str(_server_state["runner_id"])
        uid = uuid.uuid4().hex[:6]
        model = f"mock-codex-429-{uid}"
        token = f"ratelimit-{uid}"
        configure_mock_llm(
            mock_llm_server_url,
            [{"error": "rate limit exceeded", "status_code": 429}] * _RATE_LIMIT_RESPONSES,
            key=model,
            match=token,
        )
        bundle = _build_codex_bundle(f"codex-rate-limit-{uid}", model)
        # The explicit title keeps background title inference off the scripted queue.
        create_resp = post_session_bundle(
            httpx.post,
            f"{live_server}/v1/sessions",
            bundle,
            metadata={"title": "Codex rate limit retry"},
            timeout=30.0,
        )
        create_resp.raise_for_status()
        session_id = create_resp.json()["session_id"]
        bind_session_runner(httpx.patch, live_server, session_id, runner_id, timeout=10.0)

        # Create the page only after setup so a recording starts at the journey.
        page = request.getfixturevalue("page")
        assert isinstance(page, Page)
        try:
            page.goto(f"{live_server}/c/{session_id}")
            _send(page, f"Please greet me. {token}")

            error = _settled_turn_error(live_server, session_id)
            pill = page.get_by_test_id("error-pill").first
            expect(pill).to_be_visible(timeout=15_000)
            expect(pill.get_by_test_id("error-headline")).to_have_text(
                _RATE_LIMIT_HEADLINE, timeout=10_000
            )
            retry = page.get_by_role("button", name=re.compile(r"^Retry")).first
            expect(retry).to_be_visible(timeout=10_000)
            pill.click()
            expect(pill).to_contain_text("429", timeout=10_000)

            message = str(error.get("message") or "")
            assert error.get("code") == "rate_limit_exceeded", error
            assert "429" in message, message
            assert "stderr:" not in message, message

            # The provider recovers; Retry continues the turn in place.
            configure_mock_llm(mock_llm_server_url, [{"text": _RECOVERED_TEXT}], key=model)
            retry.click()
            expect(page.locator(_ASSISTANT).filter(has_text=_RECOVERED_TEXT).first).to_be_visible(
                timeout=240_000
            )
        finally:
            httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
            reset_mock_llm(mock_llm_server_url)
    finally:
        if respawned is not None:
            respawned.terminate()
            try:
                respawned.wait(timeout=5)
            except Exception:  # best-effort teardown
                respawned.kill()
                respawned.wait(timeout=5)
