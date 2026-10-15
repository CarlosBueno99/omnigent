"""A throttled agent-spec fetch must not fail a new session after a server restart.

The server restarts, dropping the host and runner tunnels. While the runners
reconnect, the API front end (stood in for by :mod:`_spec_fetch_throttle`) answers
``GET /v1/sessions/{id}/agent/contents`` with HTTP 429 for a bounded window. A user
who starts a new session during that window must still get the agent's reply: the
runner rides out the transient 429 instead of failing the turn with
``spec_resolver_failed``.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._helpers.server_runner import ServerRunner, server_runner
from tests.e2e_ui.conftest import _TEST_AGENT_YAML, configure_mock_llm
from tests.e2e_ui.sessions import _spec_fetch_throttle as edge

_BOOTSTRAP = (
    "from tests.e2e_ui.sessions._spec_fetch_throttle import install; install(); "
    "from omnigent.cli import main; main()"
)
_THROTTLE_WINDOW_S = 15

BEFORE_PROMPT = "Hello before the server restart."
BEFORE_REPLY = "Hi! Everything is working before the restart."
AFTER_PROMPT = "Starting a fresh session during the reconnect burst."
AFTER_REPLY = "Hello from the freshly initialized session."

_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_WORKING = '[data-testid="working-indicator"]'
_SPEC_RESOLVER_LINE = re.compile(r".*(spec_resolver|spec resolve|re-assign session).*")


@dataclass
class ThrottledStack:
    """A real server behind the throttle stand-in, with one online host."""

    stack: ServerRunner
    client: httpx.Client
    host_id: str
    agent_id: str
    flag: Path
    counter: Path

    @property
    def base_url(self) -> str:
        return self.stack.base_url

    def arm_throttle(self) -> None:
        """Start answering spec fetches with HTTP 429 for the throttle window."""
        self.flag.write_text(str(time.time()))

    def throttled_paths(self) -> list[str]:
        return edge.served_429s(self.counter)

    def wait_for_host_online(self, *, timeout: float) -> None:
        _wait_until(
            lambda: _online_host_id(self.client) == self.host_id,
            timeout=timeout,
            what=f"host {self.host_id} online",
        )

    def logs(self) -> str:
        """Captured stdout plus the rolling process logs of server, host and runner."""
        paths = set(self.stack.root.glob("*.log"))
        for home in (self.stack.runner_home, self.stack.server_home):
            paths.update((home / ".omnigent" / "logs").rglob("*.log"))
        return "\n".join(path.read_text(errors="replace") for path in sorted(paths))

    def spec_resolver_log_lines(self) -> list[str]:
        return [line for line in self.logs().splitlines() if _SPEC_RESOLVER_LINE.match(line)]


def _wait_until(predicate: Callable[[], bool], *, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except (httpx.HTTPError, ValueError):
            pass
        time.sleep(0.5)
    raise AssertionError(f"{what} did not happen within {timeout:.0f}s")


def _online_host_id(client: httpx.Client) -> str | None:
    hosts = client.get("/v1/hosts").json().get("hosts", [])
    return next((h["host_id"] for h in hosts if h.get("status") == "online"), None)


def start_throttled_stack(root: Path, mock_llm_url: str) -> Iterator[ThrottledStack]:
    """Start server (behind the throttle edge) and a real host; yield once both are up."""
    agent_yaml = root / "hello_world.yaml"
    agent_yaml.write_text(_TEST_AGENT_YAML)
    flag = root / "throttle.flag"
    counter = root / "throttled-spec-fetches.json"
    model_env = {"OPENAI_BASE_URL": f"{mock_llm_url}/v1", "OPENAI_API_KEY": "mock-key"}
    with (
        server_runner(
            root / "stack",
            server_bootstrap=_BOOTSTRAP,
            server_env={
                "OMNIGENT_BUILTIN_AGENT_DIRS": str(agent_yaml),
                edge.FLAG_ENV: str(flag),
                edge.COUNTER_ENV: str(counter),
                edge.WINDOW_ENV: str(_THROTTLE_WINDOW_S),
                **model_env,
            },
        ) as stack,
        httpx.Client(base_url=stack.base_url, trust_env=False, timeout=30.0) as client,
    ):
        stack.start_host(env={"OMNIGENT_RUNNER_ZYGOTE": "0", **model_env})
        _wait_until(lambda: _online_host_id(client) is not None, timeout=90, what="host online")
        host_id = _online_host_id(client)
        assert host_id is not None
        agents = client.get("/v1/agents", params={"limit": 100}).json()["data"]
        agent_id = next((a["id"] for a in agents if a["name"] == "hello_world"), None)
        assert agent_id is not None, f"hello_world agent not registered; agents: {agents}"
        yield ThrottledStack(stack, client, host_id, agent_id, flag, counter)


@pytest.fixture
def throttled_stack(
    built_spa: None, mock_llm_server_url: str, tmp_path: Path
) -> Iterator[ThrottledStack]:
    yield from start_throttled_stack(tmp_path, mock_llm_server_url)


def open_landing(page: Page, env: ThrottledStack) -> None:
    """Open the app with the host's workspace remembered so Send is enabled."""
    page.add_init_script(
        "window.localStorage.setItem('omnigent:recent-workspaces', "
        f"JSON.stringify({json.dumps({env.host_id: [str(env.stack.workspace)]})}))"
    )
    page.goto(f"{env.base_url}/")
    expect(page.get_by_test_id("new-chat-landing-input")).to_be_visible(timeout=30_000)


def choose_agent(page: Page, agent_id: str) -> None:
    picker = page.get_by_test_id("new-chat-landing-agent-select")
    picker.click()
    row = page.get_by_test_id(f"new-chat-landing-agent-{agent_id}")
    if row.count() == 0:
        custom = page.get_by_test_id("new-chat-landing-custom-agents")
        if custom.count() > 0:
            custom.hover()
    if row.count() == 0:
        more = page.get_by_test_id("new-chat-landing-harness-more")
        if more.count() > 0:
            more.click()
    row.click()
    if picker.get_attribute("aria-expanded") == "true":
        page.keyboard.press("Escape")
    expect(picker).to_have_attribute("aria-expanded", "false")


def start_session(page: Page, prompt: str) -> str:
    """Submit the landing composer and return the new session id."""
    page.get_by_test_id("new-chat-landing-input").fill(prompt)
    page.get_by_test_id("new-chat-landing-submit").click()
    page.wait_for_url(re.compile(r"/c/(?!temp)[0-9a-f]+"), timeout=60_000)
    match = re.search(r"/c/([0-9a-f]+)", page.url)
    assert match is not None, page.url
    return match.group(1)


def expect_reply(page: Page, reply: str, *, timeout_ms: int) -> None:
    expect(page.locator(_ASSISTANT).filter(has_text=reply)).to_be_visible(timeout=timeout_ms)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=timeout_ms)


@pytest.mark.timeout(600)
def test_new_session_rides_out_throttled_spec_fetch_after_server_restart(
    request: pytest.FixtureRequest,
    mock_llm_server_url: str,
    throttled_stack: ThrottledStack,
) -> None:
    env = throttled_stack
    for prompt, reply in ((BEFORE_PROMPT, BEFORE_REPLY), (AFTER_PROMPT, AFTER_REPLY)):
        configure_mock_llm(mock_llm_server_url, [{"text": reply}] * 3, match=prompt)

    page: Page = request.getfixturevalue("page")
    open_landing(page, env)
    choose_agent(page, env.agent_id)

    # A user is already chatting normally before the deployment restarts.
    first_session = start_session(page, BEFORE_PROMPT)
    expect_reply(page, BEFORE_REPLY, timeout_ms=180_000)
    assert env.client.get(f"/v1/sessions/{first_session}").json()["runner_id"]

    # The deployment restarts; its front end rate-limits the reconnecting
    # runner's spec fetches with HTTP 429 for a moment before passing through.
    env.arm_throttle()
    env.stack.restart_server()
    env.wait_for_host_online(timeout=120)

    # The user starts a fresh session during that window and sends a message.
    env.arm_throttle()
    open_landing(page, env)
    choose_agent(page, env.agent_id)
    new_session = start_session(page, AFTER_PROMPT)

    reply = page.locator(_ASSISTANT).filter(has_text=AFTER_REPLY)
    pill = page.get_by_test_id("error-pill")
    try:
        expect(reply.or_(pill).first).to_be_visible(timeout=120_000)
    finally:
        throttled = env.throttled_paths()
        print(f"new session: {new_session}")
        print(f"throttled spec fetches: {throttled}")
        print("spec-resolver log lines:\n" + "\n".join(env.spec_resolver_log_lines()[-20:]))

    assert any(new_session in path for path in throttled), (
        f"the new session's spec fetch was never throttled; inconclusive run; "
        f"throttled fetches: {throttled}"
    )
    assert pill.count() == 0, (
        f"the new session's first message failed instead of being answered; "
        f"error pill: {pill.first.inner_text().strip()!r}; throttled fetches: {throttled}"
    )
    expect_reply(page, AFTER_REPLY, timeout_ms=120_000)
    expect(pill).to_have_count(0)
