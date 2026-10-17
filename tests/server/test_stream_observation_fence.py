"""Paused runner publication must preserve a newer response's replay and live tail."""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest

from omnigent.entities.session_observation import SessionObservation
from omnigent.runtime import inflight_text, session_stream
from omnigent.server import session_live_state
from omnigent.server.routes._sessions import common, orchestration
from omnigent.server.session_observations import SessionProjection
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


@pytest.fixture
def store(db_uri: str) -> Iterator[SqlAlchemyConversationStore]:
    value = SqlAlchemyConversationStore(db_uri)
    common._session_status_cache.clear()
    common._session_active_response_cache.clear()
    inflight_text.reset_for_tests()
    session_live_state.configure(value)
    yield value
    session_live_state.submit("test_barrier", lambda: None).result(timeout=10)
    session_live_state.configure(None)
    common._session_status_cache.clear()
    common._session_active_response_cache.clear()
    inflight_text.reset_for_tests()


class _Frames:
    def __init__(
        self,
        events: list[dict[str, Any]],
        processed: asyncio.Event,
        release_close: asyncio.Event,
    ) -> None:
        self.events = events
        self.processed = processed
        self.release_close = release_close

    async def __aenter__(self) -> _Frames:
        return self

    async def __aexit__(self, *_args: object) -> None:
        pass

    def raise_for_status(self) -> None:
        pass

    async def aiter_text(self) -> AsyncIterator[str]:
        for event in self.events:
            yield "data: " + json.dumps(event) + "\n\n"
        self.processed.set()
        await self.release_close.wait()
        yield "data: [DONE]\n\n"


class _Runner:
    def __init__(
        self,
        events: list[dict[str, Any]],
        processed: asyncio.Event,
        release_close: asyncio.Event,
    ) -> None:
        self.events = events
        self.processed = processed
        self.release_close = release_close

    def stream(self, *_args: object, **_kwargs: object) -> _Frames:
        return _Frames(self.events, self.processed, self.release_close)


@pytest.mark.asyncio
async def test_heartbeat_only_relay_can_disable_persistence_with_a_configured_global_store(
    store: SqlAlchemyConversationStore,
) -> None:
    conv = store.create_conversation(runner_id="runner-heartbeat")
    assert session_live_state.conversation_store() is store
    ready, processed, release_close = asyncio.Event(), asyncio.Event(), asyncio.Event()
    release_close.set()
    await orchestration._relay_runner_stream_once(
        conv.id,
        _Runner([{"type": "session.heartbeat"}], processed, release_close),
        None,
        ready,
        runner_id="runner-heartbeat",
    )
    assert ready.is_set()
    assert processed.is_set()


def _publish_text(session_id: str, observation: SessionObservation) -> None:
    session_stream.publish(
        session_id,
        {
            "type": "response.in_progress",
            "response": {"id": observation.response_id},
            "session_observation": observation.model_dump(),
        },
    )
    session_stream.publish(
        session_id,
        {
            "type": "response.output_text.delta",
            "delta": "current response prefix",
            "session_observation": observation.model_dump(),
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", [False, True], ids=["owned", "replacement"])
async def test_paused_relay_terminal_preserves_new_response(
    store: SqlAlchemyConversationStore, monkeypatch: pytest.MonkeyPatch, replacement: bool
) -> None:
    conv = store.create_conversation(runner_id="runner-a")
    owner = store.record_session_observation(conv.id, "running", response_id="response-a")
    assert owner is not None
    assert common._session_status_cache.accept(conv.id, SessionProjection(owner, "running"))
    entered, release = asyncio.Event(), asyncio.Event()
    processed, release_close = asyncio.Event(), asyncio.Event()
    original = orchestration._relay_persist_error_once

    async def persist(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(orchestration, "_relay_persist_error_once", persist)
    event = {
        "type": "response.failed",
        "response": {
            "id": "response-a",
            "error": {"code": "runner_error", "message": "synthetic old failure"},
        },
        "session_observation": owner.model_dump(),
    }
    received: list[dict[str, Any]] = []

    async def consume() -> None:
        async for item in session_stream.subscribe(conv.id):
            received.append(item)

    consumer = asyncio.create_task(consume())
    relay = asyncio.create_task(
        orchestration._relay_runner_stream_once(
            conv.id,
            _Runner([event], processed, release_close),
            store,
            runner_id="runner-a",
            owner=owner,
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 10)
        while not session_stream.has_subscribers(conv.id):
            await asyncio.sleep(0)
        current = owner
        if replacement:
            store.replace_runner_id(conv.id, "runner-b")
            current = store.record_session_observation(
                conv.id, "running", response_id="response-b"
            )
            assert current is not None
            assert common._session_status_cache.accept(
                conv.id, SessionProjection(current, "running")
            )
        _publish_text(conv.id, current)
        release.set()
        await asyncio.wait_for(processed.wait(), 10)
        if replacement:
            before_exit = inflight_text.snapshot_for(conv.id)
            assert before_exit and before_exit[0]["response"]["id"] == "response-b"
            assert before_exit[-1]["delta"] == "current response prefix"
        release_close.set()
        await asyncio.wait_for(relay, 10)
        snapshot = inflight_text.snapshot_for(conv.id)
        session_stream.close(conv.id)
        await asyncio.wait_for(consumer, 10)
        if replacement:
            assert snapshot and snapshot[0]["response"]["id"] == "response-b"
            assert snapshot[-1]["delta"] == "current response prefix"
            assert all(item.get("type") != "response.failed" for item in received)
        else:
            assert snapshot == []
            assert any(item.get("type") == "response.failed" for item in received)
        assert store.get_session_observation(conv.id) == current
        errors = [item for item in store.list_items(conv.id).data if item.type == "error"]
        assert len(errors) == 1
    finally:
        release.set()
        release_close.set()
        session_stream.close(conv.id)
        for task in (relay, consumer):
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", [False, True], ids=["owned", "replacement"])
async def test_paused_relay_flush_preserves_new_response(
    store: SqlAlchemyConversationStore, monkeypatch: pytest.MonkeyPatch, replacement: bool
) -> None:
    conv = store.create_conversation(runner_id="runner-a")
    owner = store.record_session_observation(conv.id, "running", response_id="response-a")
    assert owner is not None
    assert common._session_status_cache.accept(conv.id, SessionProjection(owner, "running"))
    entered, release = threading.Event(), threading.Event()
    processed, release_close = asyncio.Event(), asyncio.Event()
    append = store.append

    def paused_append(*args: Any, **kwargs: Any):
        if not entered.is_set():
            entered.set()
            assert release.wait(10)
        return append(*args, **kwargs)

    monkeypatch.setattr(store, "append", paused_append)
    events = [
        {"type": "response.in_progress", "response": {"id": "response-a"}},
        {"type": "response.output_text.delta", "delta": "old persisted segment"},
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "status": "completed",
                "call_id": "old-call",
                "name": "synthetic_tool",
                "agent": "synthetic",
                "arguments": "{}",
            },
        },
    ]
    relay = asyncio.create_task(
        orchestration._relay_runner_stream_once(
            conv.id,
            _Runner(events, processed, release_close),
            store,
            runner_id="runner-a",
            owner=owner,
        )
    )
    try:
        async with asyncio.timeout(10):
            while not entered.is_set():
                await asyncio.sleep(0.01)
        await asyncio.wrap_future(session_live_state.submit("test_barrier", lambda: None))
        current = store.get_session_observation(conv.id)
        if replacement:
            store.replace_runner_id(conv.id, "runner-b")
            current = store.record_session_observation(
                conv.id, "running", response_id="response-b"
            )
            assert current is not None
            assert common._session_status_cache.accept(
                conv.id, SessionProjection(current, "running")
            )
            _publish_text(conv.id, current)
        release.set()
        await asyncio.wait_for(processed.wait(), 10)
        if replacement:
            before_exit = inflight_text.snapshot_for(conv.id)
            assert before_exit and before_exit[0]["response"]["id"] == "response-b"
            assert before_exit[-1]["delta"] == "current response prefix"
        release_close.set()
        await asyncio.wait_for(relay, 10)
        snapshot = inflight_text.snapshot_for(conv.id)
        if replacement:
            assert snapshot and snapshot[0]["response"]["id"] == "response-b"
            assert snapshot[-1]["delta"] == "current response prefix"
        else:
            assert snapshot == []
    finally:
        release.set()
        release_close.set()
        if not relay.done():
            relay.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await relay


def test_store_reconfiguration_resets_all_runtime_observation_floors(
    store: SqlAlchemyConversationStore,
) -> None:
    conv = store.create_conversation(runner_id="runner-a")
    old = store.record_session_observation(conv.id, "running", response_id="old")
    current = store.record_session_observation(conv.id, "running", response_id="current")
    assert old is not None and current is not None
    assert session_stream.learn_observation(conv.id, current)
    assert not session_stream.learn_observation(conv.id, old)
    session_live_state.configure(None)
    assert session_stream.learn_observation(conv.id, old)


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", ["session.status", "response.completed"])
@pytest.mark.parametrize("ownership", ["stale", "owned", "legacy"])
async def test_stale_annotated_relay_event_cannot_remove_a_newer_interrupt_fence(
    store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
    event_type: str,
    ownership: str,
) -> None:
    conv = store.create_conversation(runner_id="runner-a")
    old = store.record_session_observation(conv.id, "running", response_id="response-a")
    assert old is not None
    store.replace_runner_id(conv.id, "runner-b")
    current = store.record_session_observation(conv.id, "running", response_id="response-b")
    assert current is not None
    assert common._session_status_cache.accept(conv.id, SessionProjection(current, "running"))
    observed = old if ownership == "stale" else current
    event: dict[str, Any] = {"type": event_type}
    if event_type == "session.status":
        event.update(status="running", response_id=observed.response_id)
    else:
        event["response"] = {"id": observed.response_id}
    if ownership != "legacy":
        event["session_observation"] = observed.model_dump()
    attempted: list[dict[str, Any]] = []
    received: list[dict[str, Any]] = []
    publish = session_stream.publish

    def record_publish(session_id: str, frame: dict[str, Any]) -> None:
        if frame.get("type") == event_type:
            attempted.append(frame.copy())
        publish(session_id, frame)

    async def consume() -> None:
        async for frame in session_stream.subscribe(conv.id):
            received.append(frame)

    monkeypatch.setattr(session_stream, "publish", record_publish)
    consumer = asyncio.create_task(consume())
    processed, release_close = asyncio.Event(), asyncio.Event()
    release_close.set()
    common._interrupt_fenced_sessions.add(conv.id)
    try:
        async with asyncio.timeout(10):
            while not session_stream.has_subscribers(conv.id):
                await asyncio.sleep(0)
        await orchestration._relay_runner_stream_once(
            conv.id,
            _Runner([event], processed, release_close),
            store,
            runner_id="runner-b",
            owner=current,
        )
        assert (conv.id in common._interrupt_fenced_sessions) is (ownership == "stale")
        session_stream.close(conv.id)
        await asyncio.wait_for(consumer, 10)
        if event_type == "response.completed":
            [terminal] = attempted
            assert SessionObservation.model_validate(terminal["session_observation"]) == observed
            terminals = [frame for frame in received if frame.get("type") == event_type]
            assert bool(terminals) is (ownership != "stale")
    finally:
        common._interrupt_fenced_sessions.discard(conv.id)
        session_stream.close(conv.id)
        if not consumer.done():
            consumer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await consumer
