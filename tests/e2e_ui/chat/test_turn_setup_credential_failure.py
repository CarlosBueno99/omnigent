"""Turn setup when the runner's server-callback credential source fails.

Reported journey: every turn in every session failed immediately with
``turn setup failed: Host credential service could not supply workspace
credentials`` until the host was hard-restarted. The message belongs to an
external credential service; what this repository owns is how the runner reacts
when its ``_RunnerDatabricksAuth`` token factory raises ``DatabricksAuthError``
on a server callback during turn setup: the turn must fail as an actionable
credential error that names a remedy (restart the host), not as the opaque
generic ``runner_error``.

Stand-in: a real runner starts through a bootstrap that overrides its auth flow.
While the marker is absent it authenticates normally (credential-less locally);
once the marker exists it raises the reported ``DatabricksAuthError`` on the
turn-setup server callbacks, while a cached token still serves the synchronous
history read (``/items``) so the failure surfaces inside the background
turn-setup wrapper exactly as reported rather than as an ingest-time 500. Why
the real service went stale, and why only a host restart cleared it, are unknown
and not reproduced.
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
GENERIC_RUNNER_ERROR_HEADLINE = "Something went wrong setting up the turn on the host."

_MARKER_ENV = "OMNIGENT_REPRO_CREDENTIAL_FAULT_MARKER"
_MESSAGE_ENV = "OMNIGENT_REPRO_CREDENTIAL_FAULT_MESSAGE"
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

# Runs as the runner process. Stands in for the host credential service: it
# raises on turn-setup callbacks once the marker exists, but lets the history
# read (/items) through so the failure lands in the background turn-setup
# wrapper, surfacing the reported "turn setup failed:" error.
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
    print("repro stand-in installed: host credential service marker", marker, flush=True)
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
    pill.first.locator('button[aria-expanded="false"]').first.click()
    content = pill.first.get_by_test_id("error-message-content")
    expect(content).to_be_visible(timeout=10_000)
    return {"outcome": "error", "headline": headline, "message": content.inner_text()}


def runner_log_excerpt(stack: ServerRunner) -> str:
    text = stack.log_path("runner").read_text(errors="replace")
    keys = (
        "repro stand-in",
        "turn setup failed",
        "DatabricksAuthError",
        CREDENTIAL_FAILURE_MESSAGE,
    )
    return "\n".join(line for line in text.splitlines() if any(k in line for k in keys))


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
        second_session = create_bound_session(stack.base_url, stack.runner_id)
        print(
            f"sessions: control={control_session} affected={affected_session} "
            f"second={second_session}"
        )

        runner_pid = stack.runner.pid
        print(f"runner pid (before any turn): {runner_pid}")

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

            # A follow-up turn on the already-warm control session succeeds: its
            # setup callbacks were served from cache, so no credential callback is
            # made. The failure is specific to turn-setup callbacks that still need
            # a fresh credential, such as a session's first turn.
            warm_followup = settle_turn(
                control_page, send_scripted_turn(control_page, mock_llm_server_url)
            )
            print(f"warm-session follow-up turn (fault active): {warm_followup}")

            # A second, independent session fails its first turn identically while
            # the fault is active - the reported "every session" symptom.
            second_page = _open_session(context, stack.base_url, second_session)
            observed_second = settle_turn(
                second_page, send_scripted_turn(second_page, mock_llm_server_url)
            )
            _screenshot(second_page, "02b-second-session-turn")
            print(f"second session first turn (fault active): {observed_second}")
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
        print(runner_log_excerpt(stack))
        runner_pid_after = stack.runner.pid
        print(f"runner pid (after recovery): {runner_pid_after}")

    # No runner restart occurred across the control, affected, second-session and
    # recovery turns: the stand-in recovers on marker removal, unlike the reported
    # need for a hard host restart (that persistence is not reproduced here).
    assert runner_pid_after == runner_pid, (runner_pid, runner_pid_after)
    # The credential failure aborts the turn the same way in every session.
    assert observed["outcome"] == "error", observed
    assert observed_second["outcome"] == "error", observed_second
    assert observed_second["headline"] == observed["headline"], (observed, observed_second)
    # A credential failure is surfaced as its own actionable error naming a
    # remedy, not the opaque generic runner_error.
    assert observed["headline"] != GENERIC_RUNNER_ERROR_HEADLINE, observed
    assert observed["headline"] != "Something went wrong", observed
    assert observed["headline"] == "The host couldn't get workspace credentials", observed
    assert "Try this:" in (observed["message"] or ""), observed
    assert " stop`" in (observed["message"] or ""), observed
