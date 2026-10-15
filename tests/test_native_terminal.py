"""Tests for shared native terminal helpers."""

from __future__ import annotations

import click
import httpx
import pytest

from omnigent.native import native_terminal


@pytest.mark.asyncio
async def test_bind_session_runner_patches_encoded_session_path() -> None:
    """
    ``bind_session_runner`` patches the encoded session resource.

    A regression here would either bind the wrong session when ids
    contain path separators, or drop the ``runner_id`` body that the
    Omnigent server expects before launching a native terminal.
    """
    seen: dict[str, object] = {}

    async def _handler(request: httpx.Request) -> httpx.Response:
        """
        Capture the outbound request sent by the helper.

        :param request: HTTP request produced by
            :func:`native_terminal.bind_session_runner`.
        :returns: Successful empty JSON response.
        """
        seen["method"] = request.method
        seen["path"] = request.url.raw_path
        seen["json"] = request.read()
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(
        base_url="https://example.databricks.com",
        transport=httpx.MockTransport(_handler),
    ) as client:
        await native_terminal.bind_session_runner(client, "conv/a b", "runner_abc")

    assert seen["method"] == "PATCH"
    assert seen["path"] == b"/v1/sessions/conv%2Fa%20b"
    assert seen["json"] == b'{"runner_id":"runner_abc"}'


@pytest.mark.asyncio
async def test_bind_session_runner_retries_429_with_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runner binding retries rate limits and honors ``Retry-After``."""
    attempts = 0
    sleeps: list[float] = []

    async def _handler(request: httpx.Request) -> httpx.Response:
        """Rate limit two attempts before accepting the binding."""
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "30"}, request=request)
        if attempts == 2:
            return httpx.Response(429, request=request)
        return httpx.Response(200, json={}, request=request)

    async def _fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(native_terminal, "_sleep", _fake_sleep)
    async with httpx.AsyncClient(
        base_url="https://example.databricks.com",
        transport=httpx.MockTransport(_handler),
    ) as client:
        await native_terminal.bind_session_runner(client, "conv_abc", "runner_abc")

    assert attempts == 3
    assert sleeps == [10.0, 1.0]


@pytest.mark.asyncio
async def test_bind_session_runner_surfaces_429_after_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The final rate-limit response remains user-facing after the retry budget."""
    attempts = 0
    sleeps: list[float] = []

    async def _handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(
            429,
            headers={"Retry-After": "30"},
            json={"error_code": "RESOURCE_EXHAUSTED"},
            request=request,
        )

    async def _fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(native_terminal, "_sleep", _fake_sleep)
    async with httpx.AsyncClient(
        base_url="https://example.databricks.com",
        transport=httpx.MockTransport(_handler),
    ) as client:
        with pytest.raises(click.ClickException, match="RESOURCE_EXHAUSTED"):
            await native_terminal.bind_session_runner(client, "conv_abc", "runner_abc")

    assert attempts == 5
    assert sleeps == [10.0] * 4


class _FakeRetryClock:
    """Fake sleep and monotonic clock for the 429 retry loop."""

    def __init__(self) -> None:
        self.now = 1_000.0
        self.sleeps: list[float] = []

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay


def _fake_retry_clock(monkeypatch: pytest.MonkeyPatch) -> _FakeRetryClock:
    clock = _FakeRetryClock()
    monkeypatch.setattr(native_terminal, "_sleep", clock.sleep)
    monkeypatch.setattr(native_terminal, "_monotonic", lambda: clock.now)
    return clock


@pytest.mark.asyncio
async def test_request_with_429_retry_budget_outlasts_repeated_hints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a budget, short ``Retry-After`` hints are followed past the fallback schedule."""
    attempts = 0

    async def _send() -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts <= 6:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(200)

    clock = _fake_retry_clock(monkeypatch)
    response = await native_terminal.request_with_429_retry(
        _send, delays_s=(0.5, 1.0), max_retry_after_s=5.0, budget_s=10.0
    )

    assert response.status_code == 200
    assert attempts == 7
    assert clock.sleeps == [1.0] * 6


@pytest.mark.asyncio
async def test_request_with_429_retry_budget_repeats_last_fallback_until_spent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without hints the last fallback repeats, and no pause may end after the budget."""
    attempts = 0

    async def _send() -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(429)

    clock = _fake_retry_clock(monkeypatch)
    response = await native_terminal.request_with_429_retry(
        _send, delays_s=(0.5, 1.0, 2.0), max_retry_after_s=5.0, budget_s=7.0
    )

    assert response.status_code == 429
    assert attempts == 5
    assert clock.sleeps == [0.5, 1.0, 2.0, 2.0]


@pytest.mark.asyncio
async def test_request_with_429_retry_budget_counts_request_latency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Slow 429 responses consume the budget even when every pause is short."""
    clock = _fake_retry_clock(monkeypatch)
    attempts = 0

    async def _send() -> httpx.Response:
        nonlocal attempts
        attempts += 1
        clock.now += 4.0
        return httpx.Response(429)

    response = await native_terminal.request_with_429_retry(
        _send, delays_s=(1.0,), max_retry_after_s=5.0, budget_s=10.0
    )

    assert response.status_code == 429
    assert attempts == 3
    assert clock.sleeps == [1.0, 1.0]


@pytest.mark.asyncio
@pytest.mark.parametrize("delays_s", [(0.5, 0.0), ()])
async def test_request_with_429_retry_budget_rejects_a_non_positive_repeat_delay(
    delays_s: tuple[float, ...],
) -> None:
    """A budget cannot repeat a zero or missing delay, which would retry back-to-back."""

    async def _send() -> httpx.Response:
        raise AssertionError("no request should be sent")

    with pytest.raises(ValueError, match="positive"):
        await native_terminal.request_with_429_retry(_send, delays_s=delays_s, budget_s=5.0)


@pytest.mark.asyncio
async def test_bind_session_runner_raises_click_exception_on_http_error() -> None:
    """
    HTTP failures surface as ClickException with server detail.

    Native wrappers call this during CLI setup, so the failure must be
    user-facing instead of leaking a raw ``httpx.Response`` shape.
    """

    attempts = 0

    async def _handler(request: httpx.Request) -> httpx.Response:
        """
        Return a structured API error response.

        :param request: HTTP request produced by
            :func:`native_terminal.bind_session_runner`.
        :returns: HTTP 409 response with JSON error detail.
        """
        nonlocal attempts
        attempts += 1
        return httpx.Response(
            409,
            json={"detail": "runner already bound"},
            request=request,
        )

    async with httpx.AsyncClient(
        base_url="https://example.databricks.com",
        transport=httpx.MockTransport(_handler),
    ) as client:
        with pytest.raises(click.ClickException, match="runner already bound"):
            await native_terminal.bind_session_runner(client, "conv_abc", "runner_abc")

    assert attempts == 1


@pytest.mark.asyncio
async def test_bind_session_runner_raises_click_exception_on_timeout() -> None:
    """
    Read timeouts surface as a "server overloaded" ClickException.

    When the server connects but is too slow to answer, the PATCH raises
    before any response exists. A timeout means reachable-but-slow, so the
    message must say to retry rather than a raw ``httpx.ReadTimeout`` trace.
    """

    async def _handler(request: httpx.Request) -> httpx.Response:
        """
        Simulate the server never sending response headers.

        :param request: HTTP request produced by
            :func:`native_terminal.bind_session_runner`.
        :raises httpx.ReadTimeout: Always, to model a slow/overloaded server.
        """
        raise httpx.ReadTimeout("read timed out", request=request)

    async with httpx.AsyncClient(
        base_url="https://example.databricks.com",
        transport=httpx.MockTransport(_handler),
    ) as client:
        with pytest.raises(click.ClickException, match="responding too slowly"):
            await native_terminal.bind_session_runner(client, "conv_abc", "runner_abc")


@pytest.mark.asyncio
async def test_bind_session_runner_raises_click_exception_on_connect_error() -> None:
    """
    Connection failures surface as an "unreachable" ClickException.

    A connect error (bad URL, reset, DNS failure) means the server was
    never reached, so the message points at the URL/connection rather
    than leaking a raw ``httpx.ConnectError`` stack trace.
    """

    async def _handler(request: httpx.Request) -> httpx.Response:
        """
        Simulate a failed connection attempt.

        :param request: HTTP request produced by
            :func:`native_terminal.bind_session_runner`.
        :raises httpx.ConnectError: Always, to model an unreachable server.
        """
        raise httpx.ConnectError("connection refused", request=request)

    async with httpx.AsyncClient(
        base_url="https://example.databricks.com",
        transport=httpx.MockTransport(_handler),
    ) as client:
        with pytest.raises(click.ClickException, match="Couldn't reach the Omnigent server"):
            await native_terminal.bind_session_runner(client, "conv_abc", "runner_abc")


@pytest.mark.asyncio
async def test_bind_session_runner_treats_connect_timeout_as_unreachable() -> None:
    """
    Connect timeouts point at the URL/connection, not server load.

    ``httpx.ConnectTimeout`` subclasses ``TimeoutException``, but the
    connect phase timing out means the server was never reached, so it
    must not use the "responding too slowly" message.
    """

    async def _handler(request: httpx.Request) -> httpx.Response:
        """
        Simulate the connect phase timing out.

        :param request: HTTP request produced by
            :func:`native_terminal.bind_session_runner`.
        :raises httpx.ConnectTimeout: Always, to model an unreachable server.
        """
        raise httpx.ConnectTimeout("connect timed out", request=request)

    async with httpx.AsyncClient(
        base_url="https://example.databricks.com",
        transport=httpx.MockTransport(_handler),
    ) as client:
        with pytest.raises(click.ClickException, match="Couldn't reach the Omnigent server"):
            await native_terminal.bind_session_runner(client, "conv_abc", "runner_abc")


def test_terminal_attach_url_encodes_path_components_and_switches_scheme() -> None:
    """
    Attach URLs preserve base paths and percent-encode ids.

    This is the shared helper behind the Claude and Codex wrapper-local
    ``_attach_url`` aliases, so a bad URL here breaks both native
    terminal attach paths.
    """
    url = native_terminal.terminal_attach_url(
        "https://example.databricks.com/base/",
        "conv/a b",
        "terminal/main",
    )

    assert (
        url == "wss://example.databricks.com/base/v1/sessions/conv%2Fa%20b"
        "/resources/terminals/terminal%2Fmain/attach"
    )


def test_normalize_extra_args_prefers_extra_args() -> None:
    assert native_terminal.normalize_extra_args(
        extra_args=("--a",), legacy_args=None, legacy_param="pi_args"
    ) == ("--a",)


def test_normalize_extra_args_empty_when_neither_set() -> None:
    assert (
        native_terminal.normalize_extra_args(
            extra_args=None, legacy_args=None, legacy_param="pi_args"
        )
        == ()
    )


def test_normalize_extra_args_legacy_alias_warns_and_is_used() -> None:
    with pytest.warns(DeprecationWarning, match="pi_args"):
        result = native_terminal.normalize_extra_args(
            extra_args=None, legacy_args=("--legacy",), legacy_param="pi_args"
        )
    assert result == ("--legacy",)


def test_normalize_extra_args_extra_args_wins_over_legacy_with_warning() -> None:
    with pytest.warns(DeprecationWarning):
        result = native_terminal.normalize_extra_args(
            extra_args=("--new",), legacy_args=("--old",), legacy_param="pi_args"
        )
    assert result == ("--new",)
