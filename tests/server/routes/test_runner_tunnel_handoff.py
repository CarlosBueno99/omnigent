"""A replica that hands a runner's tunnel to another replica leaves that runner's turns alone."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Iterator
from types import TracebackType
from typing import Any, cast

import pytest

from omnigent.entities import Conversation
from omnigent.runtime import session_stream
from omnigent.server import shutdown_state
from omnigent.server.routes import sessions
from omnigent.server.routes._sessions import orchestration
from omnigent.stores.conversation_store import ConversationStore

_TASK_TIMEOUT_S = 10.0
_RUNNER = "runner_0123456789abcdef"


@pytest.fixture(autouse=True)
def _reset_shutdown_state() -> Iterator[None]:
    shutdown_state.reset_for_tests()
    yield
    shutdown_state.reset_for_tests()


class _UnavailableStore:
    """Store whose reads fail, as on a draining replica losing its backends."""

    def __init__(self) -> None:
        self.labels: dict[str, dict[str, str]] = {}

    def set_labels(self, conversation_id: str, updates: dict[str, str]) -> None:
        self.labels.setdefault(conversation_id, {}).update(updates)

    def get_runner_liveness(self, conversation_id: str) -> None:
        raise ConnectionError("live-state backend unavailable")

    def get_conversation(self, conversation_id: str) -> None:
        raise ConnectionError("live-state backend unavailable")


class _DropStreamResponse:
    """Runner stream that sends its ready heartbeat, then loses the tunnel."""

    def __init__(self, gate: asyncio.Event) -> None:
        self._gate = gate

    async def __aenter__(self) -> _DropStreamResponse:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback

    def raise_for_status(self) -> None:
        """The stream represents a successful HTTP response."""

    async def aiter_text(self) -> AsyncIterator[str]:
        yield 'data: {"type": "session.heartbeat"}\n\n'
        await self._gate.wait()
        raise ConnectionError("tunnel closed before request completed")


class _DropRunnerClient:
    def __init__(self, gate: asyncio.Event) -> None:
        self._gate = gate

    def stream(self, method: str, path: str, *, timeout: Any) -> _DropStreamResponse:
        del method, path, timeout
        return _DropStreamResponse(self._gate)


def _event_attributes(
    caplog: pytest.LogCaptureFixture, event_name: str
) -> list[dict[str, object]]:
    return [
        cast("dict[str, object]", getattr(record, "attributes", {}))
        for record in caplog.records
        if getattr(record, "event_name", None) == event_name
    ]


@pytest.mark.parametrize("handed_off", [True, False])
async def test_disconnect_decision_leaves_a_handed_off_turn_to_its_new_replica(
    caplog: pytest.LogCaptureFixture,
    handed_off: bool,
) -> None:
    """A cached mid-turn status alone fails the turn unless this replica handed it off."""
    caplog.set_level(logging.INFO)
    session_id = "handoff-decision"
    snapshot = Conversation(
        id=session_id,
        root_conversation_id=session_id,
        created_at=0,
        updated_at=0,
        runner_id=_RUNNER,
        live_status="running",
    )
    sessions._session_status_cache[session_id] = "running"
    if handed_off:
        shutdown_state.mark_runner_handed_off(_RUNNER, reason="rebalance")
    try:
        requires_failure = await orchestration._runner_disconnect_requires_failure(
            session_id,
            cast(ConversationStore, _UnavailableStore()),
            origin="runner_offline_sweep",
            snapshot=snapshot,
        )
    finally:
        sessions._session_status_cache.pop(session_id, None)

    assert requires_failure is not handed_off
    [decision] = _event_attributes(caplog, "runner_disconnect_decision")
    assert decision["decision"] == ("handed_off" if handed_off else "failed_mid_turn")
    assert decision["runner_handoff_reason"] == ("rebalance" if handed_off else None)
    assert decision["server_shutting_down"] is False


@pytest.mark.parametrize("handed_off", [True, False])
async def test_handoff_suppresses_even_an_authoritative_exit_report(
    caplog: pytest.LogCaptureFixture,
    handed_off: bool,
) -> None:
    """A handoff defers even an authoritative idle exit; without it the turn fails before start.

    An idle top-level session under ``fail_idle_top_level`` reaches
    ``failed_before_start`` on an authoritative ``runner_failed_to_start`` exit.
    The handoff decision short-circuits ahead of that policy, so the moved turn
    is left to its new replica while the no-handoff control still fails.
    """
    caplog.set_level(logging.INFO)
    session_id = "handoff-exit"
    snapshot = Conversation(
        id=session_id,
        root_conversation_id=session_id,
        created_at=0,
        updated_at=0,
        runner_id=_RUNNER,
        live_status="idle",
    )
    if handed_off:
        shutdown_state.mark_runner_handed_off(_RUNNER, reason="rebalance")

    requires_failure = await orchestration._runner_disconnect_requires_failure(
        session_id,
        cast(ConversationStore, _UnavailableStore()),
        origin="runner_offline_sweep",
        snapshot=snapshot,
        fail_idle_top_level=True,
        error_code="runner_failed_to_start",
    )

    assert requires_failure is not handed_off
    [decision] = _event_attributes(caplog, "runner_disconnect_decision")
    assert decision["decision"] == ("handed_off" if handed_off else "failed_before_start")


@pytest.mark.parametrize("handed_off", [True, False])
async def test_relay_does_not_fail_a_turn_handed_to_another_replica(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    handed_off: bool,
) -> None:
    """The relay's give-up keeps a handed-off running turn even when liveness reads fail."""
    monkeypatch.setattr(orchestration, "RUNNER_DISCONNECT_GRACE_S", 0.0)
    caplog.set_level(logging.INFO)
    sessions._runner_relay_tasks.clear()
    session_id = "fedcba9876543210fedcba9876543210"
    gate = asyncio.Event()
    try:
        handle = await sessions._ensure_runner_relay_ready(
            session_id,
            _RUNNER,
            _DropRunnerClient(gate),  # type: ignore[arg-type]
            conversation_store=_UnavailableStore(),  # type: ignore[arg-type]
        )
        assert handle is not None
        sessions._session_status_cache[session_id] = "running"
        if handed_off:
            shutdown_state.mark_runner_handed_off(_RUNNER, reason="rebalance")

        gate.set()
        await asyncio.wait_for(handle.task, timeout=_TASK_TIMEOUT_S)

        assert handle.task.exception() is None
        status = sessions._session_status_cache.get(session_id)
        assert (status != "failed") is handed_off
        [outcome] = _event_attributes(caplog, "runner_stream_disconnected")
        assert outcome["decision"] == ("handed_off" if handed_off else "failed_mid_turn")
        assert outcome["runner_handoff_reason"] == ("rebalance" if handed_off else None)
    finally:
        gate.set()
        handle = sessions._runner_relay_tasks.get(session_id)
        if handle is not None and not handle.task.done():
            handle.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
                await asyncio.wait_for(handle.task, timeout=_TASK_TIMEOUT_S)
        sessions._runner_relay_tasks.clear()
        sessions._session_status_cache.pop(session_id, None)
        session_stream.close(session_id)
