"""A reconnecting managed host wakes exactly the sessions it interrupted."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from omnigent.db.utils import generate_agent_id
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.server.host_session_recovery import (
    schedule_host_session_recovery,
    wake_interrupted_sessions_on_host,
)
from omnigent.server.managed_hosts import ManagedLaunchTracker
from omnigent.server.routes._sessions import orchestration
from omnigent.server.routes._sessions.common import _intentional_stop_sessions
from omnigent.server.routes._sessions.helpers import (
    _session_status_cache,
    _session_status_error_labels,
    runner_disconnected_error,
)
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.host_store import HostStore

_MANAGED_HOST_ID = "3f866cafac81246fb60ae6ceb1a738da"
_EXTERNAL_HOST_ID = "9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f"


@pytest.fixture(autouse=True)
def _isolate_status_cache() -> Iterator[None]:
    snapshot = dict(_session_status_cache)
    _session_status_cache.clear()
    yield
    _session_status_cache.clear()
    _session_status_cache.update(snapshot)


class _Stores:
    """Stores sharing one database, plus a managed and an external host row."""

    def __init__(self, db_uri: str) -> None:
        self.hosts = HostStore(db_uri)
        self.conversations = SqlAlchemyConversationStore(db_uri)
        self.agent_id = generate_agent_id()
        SqlAlchemyAgentStore(db_uri).create(
            self.agent_id, name=f"wake-agent-{self.agent_id}", bundle_location="test:///bundle"
        )
        self.hosts.register_managed_host(
            host_id=_MANAGED_HOST_ID,
            name="managed-sandbox",
            user_id=RESERVED_USER_LOCAL,
            token="launch-token",
            provider="modal",
            sandbox_id="sb-1",
            token_expires_at=int(time.time()) + 3600,
        )
        self.hosts.upsert_on_connect(_EXTERNAL_HOST_ID, "laptop", RESERVED_USER_LOCAL)

    def session(
        self,
        *,
        host_id: str = _MANAGED_HOST_ID,
        live_status: str = "running",
        runner_id: str | None = "runner_token_gone",
        disconnected: bool = False,
    ) -> str:
        conv = self.conversations.create_conversation(agent_id=self.agent_id)
        self.conversations.set_host_id(conv.id, host_id, workspace="/root/workspace")
        if runner_id is not None:
            assert self.conversations.set_runner_id(conv.id, runner_id)
        self.conversations.set_session_live_status(conv.id, live_status)
        if disconnected:
            labels = _session_status_error_labels(
                runner_disconnected_error(), owner_runner_id=runner_id
            )
            self.conversations.set_labels(conv.id, labels)
        return conv.id


def _app_state(**extra: Any) -> SimpleNamespace:
    return SimpleNamespace(managed_launches=ManagedLaunchTracker(), **extra)


def _router(online: set[str]) -> Any:
    """The one ``RunnerRouter`` query the wake consults."""
    return SimpleNamespace(runner_is_online=lambda runner_id: runner_id in online)


def _record_wakes(monkeypatch: pytest.MonkeyPatch, *, reachable: bool = True) -> list[str]:
    woken: list[str] = []

    async def fake_ensure(*, session_id: str, conv: Any, **_: Any) -> tuple[object | None, Any]:
        woken.append(session_id)
        return (object() if reachable else None), conv

    monkeypatch.setattr(orchestration, "ensure_runner_connected", fake_ensure)
    return woken


async def test_wake_targets_only_interrupted_restorable_sessions(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Running, waiting and disconnected sessions wake; settled or spoken-for ones do not."""
    stores = _Stores(db_uri)
    running = stores.session(live_status="running")
    waiting = stores.session(live_status="waiting")
    disconnected = stores.session(live_status="failed", disconnected=True)
    idle = stores.session(live_status="idle")
    genuine_failure = stores.session(live_status="failed")
    still_online = stores.session(runner_id="runner_token_alive")
    stopped = stores.session(runner_id="runner_token_stopped")
    _intentional_stop_sessions[stopped] = "runner_token_stopped"
    launching = stores.session()
    app_state = _app_state()
    app_state.managed_launches.begin(launching)
    starting = stores.session()
    woken = _record_wakes(monkeypatch)

    try:
        with orchestration._mark_dispatch_in_flight(starting):
            reachable = await wake_interrupted_sessions_on_host(
                _MANAGED_HOST_ID,
                conversation_store=stores.conversations,
                host_store=stores.hosts,
                app_state=app_state,
                runner_router=_router({"runner_token_alive"}),
            )
    finally:
        _intentional_stop_sessions.pop(stopped, None)

    assert sorted(woken) == sorted([running, waiting, disconnected])
    assert sorted(reachable) == sorted([running, waiting, disconnected])
    for untouched in (idle, genuine_failure, still_online, stopped, launching, starting):
        assert untouched not in woken


async def test_wake_leaves_external_and_unknown_hosts_alone(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only hosts the server launched are its to bring back."""
    stores = _Stores(db_uri)
    stores.session(host_id=_EXTERNAL_HOST_ID)
    woken = _record_wakes(monkeypatch)

    for host_id in (_EXTERNAL_HOST_ID, "0000000000000000000000000000dead"):
        assert (
            await wake_interrupted_sessions_on_host(
                host_id,
                conversation_store=stores.conversations,
                host_store=stores.hosts,
                app_state=_app_state(),
                runner_router=_router(set()),
            )
            == []
        )
    assert woken == []


async def test_wake_reports_only_runners_that_came_back(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wake that times out or raises is logged and does not stop the others."""
    stores = _Stores(db_uri)
    first = stores.session()
    second = stores.session()
    third = stores.session()
    attempts: list[str] = []

    async def flaky_ensure(*, session_id: str, conv: Any, **_: Any) -> tuple[object | None, Any]:
        attempts.append(session_id)
        if session_id == first:
            raise RuntimeError("host refused the launch")
        if session_id == second:
            return None, conv
        return object(), conv

    monkeypatch.setattr(orchestration, "ensure_runner_connected", flaky_ensure)

    reachable = await wake_interrupted_sessions_on_host(
        _MANAGED_HOST_ID,
        conversation_store=stores.conversations,
        host_store=stores.hosts,
        app_state=_app_state(),
        runner_router=_router(set()),
    )

    assert sorted(attempts) == sorted([first, second, third])
    assert reachable == [third]


async def test_schedule_runs_one_wake_per_host_at_a_time(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A host that reconnects again mid-wake is served by the wake already running."""
    stores = _Stores(db_uri)
    session_id = stores.session()
    release = asyncio.Event()
    attempts: list[str] = []

    async def held_ensure(*, session_id: str, conv: Any, **_: Any) -> tuple[object, Any]:
        attempts.append(session_id)
        await release.wait()
        return object(), conv

    monkeypatch.setattr(orchestration, "ensure_runner_connected", held_ensure)
    kwargs: dict[str, Any] = {
        "conversation_store": stores.conversations,
        "host_store": stores.hosts,
        "app_state": _app_state(),
        "runner_router": _router(set()),
    }

    first = schedule_host_session_recovery(_MANAGED_HOST_ID, **kwargs)
    assert first is not None
    await asyncio.sleep(0)
    while not attempts:
        await asyncio.sleep(0.01)
    assert schedule_host_session_recovery(_MANAGED_HOST_ID, **kwargs) is None
    release.set()
    assert await first == [session_id]

    second = schedule_host_session_recovery(_MANAGED_HOST_ID, **kwargs)
    assert second is not None and second is not first
    assert await second == [session_id]
    assert attempts == [session_id, session_id]
