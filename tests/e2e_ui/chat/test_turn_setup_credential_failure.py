"""Turn setup when the runner's server-callback credential source fails.

A real runner starts through a bootstrap that overrides its auth flow: once the
marker file exists, ``_RunnerDatabricksAuth`` raises the reported
``DatabricksAuthError`` on turn-setup server callbacks while a cached token still
serves the synchronous history read (``/items``), so the failure surfaces inside
the background turn-setup wrapper. The turn must fail as an actionable credential
error that names a remedy (restart the host), not as the opaque generic
``runner_error``. This covers the runner-process, server-relay and browser
boundary that the runner dispatch tests cannot reach.
"""

from __future__ import annotations

import os
import textwrap
import uuid
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Browser, BrowserContext, Page, expect

from tests._helpers.server_runner import ServerRunner, server_runner
from tests._helpers.session import bind_session_runner, bundle_files, post_session_bundle
from tests.e2e_ui.conftest import configure_mock_llm, reset_mock_llm, set_fallback_mock_llm

CREDENTIAL_FAILURE_MESSAGE = "Host credential service could not supply workspace credentials"
CREDENTIAL_FAILURE_HEADLINE = "The host couldn't get workspace credentials"

_MARKER_ENV = "OMNIGENT_TEST_CREDENTIAL_FAULT_MARKER"
_MESSAGE_ENV = "OMNIGENT_TEST_CREDENTIAL_FAULT_MESSAGE"
_MODEL = "credential-probe"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_WORKING = '[data-testid="working-indicator"]'
_TURN_TIMEOUT_MS = 90_000

_AGENT_YAML = f"""\
spec_version: 1
name: credential_probe
prompt: |
  You are a precise echo assistant. The user asks for one exact token.
  Reply with that token verbatim and nothing else.

executor:
  model: {_MODEL}
  config:
    harness: openai-agents
"""

# Runs as the runner process, standing in for the host credential service:
# raises on turn-setup callbacks once the marker exists, but lets /items
# through so the failure surfaces as the reported "turn setup failed:" error.
RUNNER_BOOTSTRAP = textwrap.dedent(
    f"""\
    import os, pathlib
    import omnigent.runner._entry as entry
    from omnigent.inner.databricks_executor import DatabricksAuthError

    marker = pathlib.Path(os.environ[{_MARKER_ENV!r}])
    message = os.environ[{_MESSAGE_ENV!r}]

    class StandInRunnerAuth(entry._RunnerDatabricksAuth):
        def auth_flow(self, request):
            if marker.exists() and "/items" not in request.url.path:
                raise DatabricksAuthError(message)
            yield from super().auth_flow(request)

    entry._RunnerDatabricksAuth = StandInRunnerAuth
    print("credential stand-in installed: marker", marker, flush=True)
    entry.main()
    """
)


def mock_env(mock_url: str) -> dict[str, str]:
    return {"OPENAI_BASE_URL": f"{mock_url}/v1", "OPENAI_API_KEY": "mock-key"}


def start_runner_with_credential_stand_in(
    stack: ServerRunner, mock_url: str, marker: Path
) -> None:
    stack.start_runner(
        bootstrap=RUNNER_BOOTSTRAP,
        env={
            **mock_env(mock_url),
            _MARKER_ENV: str(marker),
            _MESSAGE_ENV: CREDENTIAL_FAILURE_MESSAGE,
        },
    )


def create_bound_session(base_url: str, runner_id: str) -> str:
    bundle = bundle_files({"config.yaml": _AGENT_YAML.encode()})
    response = post_session_bundle(httpx.post, f"{base_url}/v1/sessions", bundle, timeout=30.0)
    response.raise_for_status()
    session_id = str(response.json()["session_id"])
    bind_session_runner(httpx.patch, base_url, session_id, runner_id, timeout=10.0)
    return session_id


def send(page: Page, text: str) -> None:
    composer = page.get_by_role("textbox", name="Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def send_scripted_turn(page: Page, mock_url: str) -> str:
    """Send a prompt whose reply is scripted on the mock; return the reply token."""
    nonce = uuid.uuid4().hex[:8]
    marker, token = f"usr-{nonce}", f"ast-{nonce}"
    reset_mock_llm(mock_url)
    configure_mock_llm(mock_url, [{"text": token}], key=marker, match=marker)
    send(page, f"Context marker {marker}. Reply with exactly this token and nothing else: {token}")
    return token


def expect_reply(page: Page, token: str) -> None:
    expect(page.locator(_ASSISTANT, has_text=token).first).to_be_visible(timeout=_TURN_TIMEOUT_MS)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=_TURN_TIMEOUT_MS)


def settle_turn(page: Page, token: str, timeout_ms: int = 60_000) -> dict[str, str | None]:
    """Wait for a reply or an error pill; return what the user sees."""
    pill = page.get_by_test_id("error-pill")
    reply = page.locator(_ASSISTANT, has_text=token)
    expect(pill.or_(reply).first).to_be_visible(timeout=timeout_ms)
    if pill.count() == 0:
        return {"outcome": "reply", "headline": None, "message": None}
    headline = pill.first.get_by_test_id("error-headline").inner_text()
    expander = pill.first.locator('button[aria-expanded="false"]')
    if expander.count() > 0:
        expander.first.click()
    content = pill.first.get_by_test_id("error-message-content")
    expect(content).to_be_visible(timeout=10_000)
    return {"outcome": "error", "headline": headline, "message": content.inner_text()}


def _screenshot(page: Page, name: str) -> None:
    directory = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
    if directory:
        page.screenshot(path=str(Path(directory) / f"{name}.png"))


def _open_session(context: BrowserContext, base_url: str, session_id: str) -> Page:
    page = context.new_page()
    page.goto(f"{base_url}/c/{session_id}")
    return page


@pytest.mark.timeout(900)
def test_credential_failure_during_turn_setup_is_not_a_generic_runner_error(
    browser: Browser,
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    marker = tmp_path / "credential-service.down"
    set_fallback_mock_llm(mock_llm_server_url, _MODEL, "")
    with server_runner(tmp_path / "stack", server_env=mock_env(mock_llm_server_url)) as stack:
        start_runner_with_credential_stand_in(stack, mock_llm_server_url, marker)
        control_session = create_bound_session(stack.base_url, stack.runner_id)
        affected_session = create_bound_session(stack.base_url, stack.runner_id)
        print(f"sessions: control={control_session} affected={affected_session}")

        context = browser.new_context()
        try:
            control_page = _open_session(context, stack.base_url, control_session)
            expect_reply(control_page, send_scripted_turn(control_page, mock_llm_server_url))
            _screenshot(control_page, "01-control-reply")

            marker.touch()
            affected_page = _open_session(context, stack.base_url, affected_session)
            observed = settle_turn(
                affected_page, send_scripted_turn(affected_page, mock_llm_server_url)
            )
            _screenshot(affected_page, "02-affected-turn")
            print(f"affected turn: {observed}")
        finally:
            context.close()

        marker.unlink()
        recovery = browser.new_context()
        try:
            page = _open_session(recovery, stack.base_url, affected_session)
            expect_reply(page, send_scripted_turn(page, mock_llm_server_url))
            _screenshot(page, "03-recovered-reply")
        finally:
            recovery.close()

    assert observed["outcome"] == "error", observed
    assert observed["headline"] == CREDENTIAL_FAILURE_HEADLINE, observed
    message = observed["message"] or ""
    assert CREDENTIAL_FAILURE_MESSAGE in message, observed
    assert "Try this:" in message, observed
    assert " stop`" in message, observed
