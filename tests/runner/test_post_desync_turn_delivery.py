"""The next message after a dropped runner<->harness stream must start a fresh turn.

A harness that missed its dropped stream still holds that turn's context and
answers a fresh delivery with ``204`` (sessions-native injection), which the
runner reports as ``harness rejected turn delivery ... with status 204``. These
tests drive the real runner against the real ``HarnessApp`` start/inject and
interrupt logic; only the in-process transport is substituted so the stream
can be severed deterministically.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from starlette.responses import StreamingResponse

from omnigent.errors import OmnigentError
from omnigent.runner import create_runner_app
from omnigent.runtime.harnesses._scaffold import (
    HarnessApp,
    MessageEvent,
    TurnContext,
    _format_sse_event,
)
from omnigent.runtime.harnesses.process_manager import NoLiveHarnessError
from omnigent.server.schemas import CreateResponseRequest, OutputTextDeltaEvent
from omnigent.spec.types import AgentSpec
from tests.runner.conftest import _FakeProcessManager
from tests.runner.helpers import NullServerClient

_CONV_ID = "ac1dbeef245985f541fd686eb2a32b73"
_AGENT_ID = "965906f5d9fb596610dda599a80faaee"
_INPUT_STABLE_ID = "7f3a9c1e5b2d4f6a8c0e1d2b3a4f5c6d"
_RETRY_HINT = "Please retry your message"
_REJECTION_LOG = "harness rejected turn delivery for"


class _EchoHarness(HarnessApp):
    """Minimal harness: a started turn emits one delta and completes."""

    async def run_turn(self, request: CreateResponseRequest, ctx: TurnContext) -> None:
        ctx.emit(OutputTextDeltaEvent(type="response.output_text.delta", delta="ok"))


class _ScaffoldBackedHarnessClient:
    """Runner-facing harness client that routes turns and interrupts into a real scaffold.

    The first delivered turn is dropped mid-stream without iterating it, leaving
    the scaffold's ``_active_turn_ctx`` set as a lost runner stream does.
    """

    def __init__(self, scaffold: HarnessApp, conv_id: str) -> None:
        self.scaffold = scaffold
        self._conv_id = conv_id
        self._drop_next_stream = True
        self.dropped_stream: StreamingResponse | None = None
        self.deliveries = 0
        self.interrupt_statuses: list[int] = []
        self.completed_turns = 0

    def stream(self, method: str, url: str, *, json: dict[str, Any], timeout: Any) -> Any:
        del method, url, timeout
        client = self
        body = json

        class _Ctx:
            async def __aenter__(self) -> _ScaffoldBackedHarnessClient._Handle:
                scaffold = client.scaffold
                client.deliveries += 1
                create_req = MessageEvent.model_validate(body).to_create_request()
                result = await scaffold._start_or_inject_turn(
                    create_req, session_id=client._conv_id
                )
                if not isinstance(result, StreamingResponse):
                    return _ScaffoldBackedHarnessClient._Handle(result.status_code, [], drop=False)
                if client._drop_next_stream:
                    client._drop_next_stream = False
                    client.dropped_stream = result
                    ctx = scaffold._active_turn_ctx
                    assert ctx is not None
                    frames = [
                        _format_sse_event(event).decode()
                        for event in scaffold._initial_envelope_events(
                            ctx, model=create_req.model or "plain-agent", start_seq=0
                        )
                    ]
                    return _ScaffoldBackedHarnessClient._Handle(200, frames, drop=True)
                frames = [
                    chunk if isinstance(chunk, str) else bytes(chunk).decode()
                    async for chunk in result.body_iterator
                ]
                if any('"response.completed"' in frame for frame in frames):
                    client.completed_turns += 1
                return _ScaffoldBackedHarnessClient._Handle(200, frames, drop=False)

            async def __aexit__(self, *_: Any) -> None:
                return None

        return _Ctx()

    class _Handle:
        def __init__(self, status_code: int, frames: list[str], *, drop: bool) -> None:
            self.status_code = status_code
            self._frames = frames
            self._drop = drop

        async def aiter_text(self) -> AsyncIterator[str]:
            for frame in self._frames:
                yield frame
            if self._drop:
                raise httpx.ReadError("runner<->harness transport dropped mid-turn")

    async def post(self, url: str, *, json: dict[str, Any], timeout: Any = None) -> httpx.Response:
        del timeout
        request = httpx.Request("POST", url)
        if json.get("type") != "interrupt":
            return httpx.Response(200, request=request)
        try:
            await self.scaffold._handle_interrupt_event()
            status = 204
        except OmnigentError as exc:
            # The scaffold's exception handler renders errors with their http_status.
            status = exc.http_status
        self.interrupt_statuses.append(status)
        return httpx.Response(status, request=request)


class _DeadHarnessProcessManager(_FakeProcessManager):
    """Reports no live harness until a named-harness lookup respawns a fresh scaffold."""

    def __init__(self, client: _ScaffoldBackedHarnessClient) -> None:
        super().__init__(client)  # type: ignore[arg-type]
        self._scaffold_client = client
        self.process_dead = False

    async def get_client(self, conversation_id: str, harness: str, env: Any = None) -> Any:
        if self.process_dead:
            if harness == "any":
                raise NoLiveHarnessError(f"no live harness for {conversation_id!r}")
            self.process_dead = False
            self._scaffold_client.scaffold = _EchoHarness()
        return await super().get_client(conversation_id, harness, env)


def _make_app(pm: _FakeProcessManager) -> Any:
    spec = AgentSpec(spec_version=1, name="plain-agent")

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    return create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )


def _setup() -> tuple[Any, _ScaffoldBackedHarnessClient]:
    client = _ScaffoldBackedHarnessClient(_EchoHarness(), _CONV_ID)
    return _make_app(_FakeProcessManager(client)), client  # type: ignore[arg-type]


def _message_body(text: str) -> dict[str, Any]:
    return {
        "type": "message",
        "role": "user",
        "agent_id": _AGENT_ID,
        "model": "plain-agent",
        "content": [{"type": "input_text", "text": text}],
        "harness": "openai-agents",
        "input_stable_id": _INPUT_STABLE_ID,
    }


def _parse_sse(buf: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for block in buf.split("\n\n"):
        for line in block.strip().splitlines():
            line = line.strip()
            if line.startswith("data:"):
                with contextlib.suppress(json.JSONDecodeError):
                    events.append(json.loads(line[len("data:") :].strip()))
    return events


async def _post_turn(app: Any, text: str, *, streaming: bool) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    suffix = "?stream=true" if streaming else ""
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        return await client.post(
            f"/v1/sessions/{_CONV_ID}/events{suffix}", json=_message_body(text)
        )


async def _drive_turn(app: Any, text: str) -> list[dict[str, Any]]:
    """Deliver one streamed turn; return the runner's SSE events."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        async with client.stream(
            "POST", f"/v1/sessions/{_CONV_ID}/events?stream=true", json=_message_body(text)
        ) as resp:
            assert resp.status_code == 200, resp.status_code
            buf = ""
            with contextlib.suppress(Exception):
                async for chunk in resp.aiter_text():
                    buf += chunk
    return _parse_sse(buf)


async def _wait_for_turn_slot_release(app: Any) -> None:
    for _ in range(300):
        if _CONV_ID not in app.state.active_turns:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("background turn never released its slot")


async def _run_background_turn(app: Any, text: str) -> None:
    resp = await _post_turn(app, text, streaming=False)
    assert resp.status_code == 202, resp.text
    await _wait_for_turn_slot_release(app)


def _drain_session_events(app: Any) -> list[dict[str, Any]]:
    queue = app.state.session_event_queues.get(_CONV_ID)
    events: list[dict[str, Any]] = []
    while queue is not None and not queue.empty():
        event = queue.get_nowait()
        if isinstance(event, dict):
            events.append(event)
    return events


def _statuses(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in events if e.get("type") == "session.status"]


def _failures(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in events if e.get("type") == "response.failed"]


def _completed(events: list[dict[str, Any]]) -> bool:
    return any(e.get("type") == "response.completed" for e in events)


async def _desync_via_dropped_turn(app: Any, client: _ScaffoldBackedHarnessClient) -> None:
    """Turn 1: the mid-stream transport drop that leaves the harness context stale."""
    turn1 = await _drive_turn(app, "first message")
    assert any(e.get("error", {}).get("code") == "connection_error" for e in _failures(turn1)), (
        turn1
    )
    assert _CONV_ID in app.state.desynced_sessions
    assert client.scaffold._active_turn_ctx is not None
    _drain_session_events(app)


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [True, False], ids=["streamed", "background"])
async def test_next_message_after_midturn_drop_starts_fresh_turn(
    streaming: bool, caplog: pytest.LogCaptureFixture
) -> None:
    app, client = _setup()
    with caplog.at_level(logging.ERROR, logger="omnigent.runner.app"):
        await _desync_via_dropped_turn(app, client)
        if streaming:
            turn2 = await _drive_turn(app, "second message")
            assert not _failures(turn2), f"turn 2 must not fail: {turn2}"
            assert _completed(turn2), turn2
        else:
            await _run_background_turn(app, "second message")
            events = _drain_session_events(app)
            assert not _failures(events), events
            assert _statuses(events)[-1]["status"] != "failed", events

    rejections = [r.getMessage() for r in caplog.records if _REJECTION_LOG in r.getMessage()]
    assert rejections == []
    assert client.interrupt_statuses == [204]
    assert client.completed_turns == 1
    assert _CONV_ID not in app.state.desynced_sessions


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [True, False], ids=["streamed", "background"])
async def test_next_message_after_harness_finished_dropped_turn_completes(
    streaming: bool,
) -> None:
    """With nothing left to interrupt (the harness answers 404), the message still runs."""
    app, client = _setup()
    await _desync_via_dropped_turn(app, client)
    assert client.dropped_stream is not None
    async for _ in client.dropped_stream.body_iterator:
        pass
    assert client.scaffold._active_turn_ctx is None

    if streaming:
        turn2 = await _drive_turn(app, "second message")
        assert not _failures(turn2), f"turn 2 must not fail: {turn2}"
    else:
        await _run_background_turn(app, "second message")
        assert _statuses(_drain_session_events(app))[-1]["status"] != "failed"
    assert client.interrupt_statuses == [404]
    assert client.completed_turns == 1
    assert _CONV_ID not in app.state.desynced_sessions


@pytest.mark.asyncio
async def test_next_message_after_harness_process_died_starts_fresh_turn() -> None:
    client = _ScaffoldBackedHarnessClient(_EchoHarness(), _CONV_ID)
    pm = _DeadHarnessProcessManager(client)
    app = _make_app(pm)
    await _desync_via_dropped_turn(app, client)
    pm.process_dead = True

    turn2 = await _drive_turn(app, "second message")

    assert _completed(turn2), turn2
    assert client.interrupt_statuses == []
    assert _CONV_ID not in app.state.desynced_sessions


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [True, False], ids=["streamed", "background"])
@pytest.mark.parametrize("failure", ["connection", "status"])
async def test_unacknowledged_interrupt_fails_readably_and_stays_recoverable(
    monkeypatch: pytest.MonkeyPatch, streaming: bool, failure: str
) -> None:
    app, client = _setup()
    await _desync_via_dropped_turn(app, client)
    stale_ctx = client.scaffold._active_turn_ctx
    original_post = client.post

    async def _failing_interrupt(
        url: str, *, json: dict[str, Any], timeout: Any = None
    ) -> httpx.Response:
        if json.get("type") != "interrupt":
            return await original_post(url, json=json, timeout=timeout)
        if failure == "connection":
            raise httpx.ConnectError("synthetic interrupt connection failure")
        return httpx.Response(503, request=httpx.Request("POST", url))

    monkeypatch.setattr(client, "post", _failing_interrupt)
    resp = await _post_turn(app, "second message", streaming=streaming)
    if streaming:
        assert resp.status_code == 503, resp.text
        assert resp.json()["error"] == "harness_reconciliation_failed"
    else:
        assert resp.status_code == 202, resp.text
        await _wait_for_turn_slot_release(app)

    events = _drain_session_events(app)
    if not streaming:
        [undelivered] = _failures(events)
        assert undelivered["input_stable_id"] == _INPUT_STABLE_ID
        assert undelivered["response"]["error"]["undelivered"] is True
    message = _statuses(events)[-1]["error"]["message"]
    assert _statuses(events)[-1]["status"] == "failed"
    assert _RETRY_HINT in message, message
    assert not message.lstrip().startswith("{"), message
    assert client.deliveries == 1
    assert client.scaffold._active_turn_ctx is stale_ctx
    assert _CONV_ID in app.state.desynced_sessions
    assert _CONV_ID not in app.state.active_turns

    monkeypatch.setattr(client, "post", original_post)
    turn3 = await _drive_turn(app, "third message")
    assert _completed(turn3), turn3
    assert client.interrupt_statuses == [204]
    assert _CONV_ID not in app.state.desynced_sessions


@pytest.mark.asyncio
async def test_cancelling_reconciliation_releases_the_background_turn_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client = _setup()
    await _desync_via_dropped_turn(app, client)
    entered = asyncio.Event()
    release = asyncio.Event()
    original_post = client.post

    async def _blocked_interrupt(
        url: str, *, json: dict[str, Any], timeout: Any = None
    ) -> httpx.Response:
        if json.get("type") == "interrupt":
            entered.set()
            await release.wait()
        return await original_post(url, json=json, timeout=timeout)

    monkeypatch.setattr(client, "post", _blocked_interrupt)
    resp = await _post_turn(app, "second message", streaming=False)
    assert resp.status_code == 202, resp.text
    task = app.state.active_turns[_CONV_ID]
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        assert task.cancelled()
        assert _CONV_ID not in app.state.active_turns
        assert _CONV_ID in app.state.desynced_sessions

        monkeypatch.setattr(client, "post", original_post)
        retry = await _drive_turn(app, "retry after cancellation")
        assert _completed(retry), retry
        assert client.interrupt_statuses == [204]
    finally:
        release.set()
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
