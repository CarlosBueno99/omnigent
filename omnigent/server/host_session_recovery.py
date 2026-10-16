"""Resume the sessions a managed host interrupted once that host reconnects."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from omnigent.db.workspace_cache import WorkspaceScopedCache
from omnigent.debug_logging import debug_event
from omnigent.entities import Conversation

if TYPE_CHECKING:
    from omnigent.runner.routing import RunnerRouter
    from omnigent.stores.conversation_store import ConversationStore
    from omnigent.stores.host_store import HostStore

_logger = logging.getLogger(__name__)

# Runner launches issued at once for one reconnecting host.
WAKE_CONCURRENCY = 4

_recovery_tasks: WorkspaceScopedCache[str, asyncio.Task[list[str]]] = WorkspaceScopedCache()


def schedule_host_session_recovery(
    host_id: str,
    *,
    conversation_store: ConversationStore,
    host_store: HostStore,
    app_state: Any,
    runner_router: RunnerRouter | None,
) -> asyncio.Task[list[str]] | None:
    """
    Resume *host_id*'s interrupted sessions in the background.

    The host tunnel's connect callback is bounded, and a wake waits for runner
    launches, so the work runs as a task. A host that reconnects again while
    its previous wake is still running is served by that wake.

    :param host_id: The host that just connected.
    :param conversation_store: Store holding the host's session bindings.
    :param host_store: Store holding the host row.
    :param app_state: ``app.state`` — supplies the registries and launch tracker
        :func:`ensure_runner_connected` needs.
    :param runner_router: The ``RunnerRouter``, or ``None`` for in-process.
    :returns: The scheduled task, or ``None`` when one is already running.
    """
    existing = _recovery_tasks.get(host_id)
    if existing is not None and not existing.done():
        return None
    task = asyncio.create_task(
        wake_interrupted_sessions_on_host(
            host_id,
            conversation_store=conversation_store,
            host_store=host_store,
            app_state=app_state,
            runner_router=runner_router,
        ),
        name=f"host-session-recovery-{host_id}",
    )
    _recovery_tasks[host_id] = task

    def finished(done: asyncio.Task[list[str]]) -> None:
        if _recovery_tasks.get(host_id) is done:
            _recovery_tasks.pop(host_id, None)
        if not done.cancelled() and (error := done.exception()) is not None:
            _logger.error("Failed to resume sessions on host %s", host_id, exc_info=error)

    task.add_done_callback(finished)
    return task


def _needs_wake(conv: Conversation, *, app_state: Any, runner_router: RunnerRouter | None) -> bool:
    from omnigent.server.child_session_recovery import _interrupted, _restorable
    from omnigent.server.routes._sessions.orchestration import _session_is_starting

    if not (_restorable(conv) and _interrupted(conv)):
        return False
    # A message already waiting on the runner relaunches it itself, and so
    # does the session's own managed launch while it is still in flight.
    if _session_is_starting(conv.id):
        return False
    launches = getattr(app_state, "managed_launches", None)
    launch = launches.get(conv.id) if launches is not None else None
    if launch is not None and not launch.settled.is_set():
        return False
    return not (
        conv.runner_id is not None
        and runner_router is not None
        and runner_router.runner_is_online(conv.runner_id)
    )


async def wake_interrupted_sessions_on_host(
    host_id: str,
    *,
    conversation_store: ConversationStore,
    host_store: HostStore,
    app_state: Any,
    runner_router: RunnerRouter | None,
) -> list[str]:
    """
    Relaunch runners for the interrupted sessions bound to a managed host.

    Hosts the server launched are the server's to bring back: after the
    sandbox restarts, nothing relaunches their runners until a user acts on
    the session, so an interrupted turn waits indefinitely. Each wake goes
    through :func:`ensure_runner_connected`; the runner's connect hook then
    asks it to continue the turn. External hosts are left to their users.

    :param host_id: The host that just connected.
    :param conversation_store: Store holding the host's session bindings.
    :param host_store: Store holding the host row.
    :param app_state: ``app.state`` — supplies the registries and launch tracker.
    :param runner_router: The ``RunnerRouter``, or ``None`` for in-process.
    :returns: Ids of the sessions whose runner is reachable again.
    """
    from omnigent.server.routes._sessions.orchestration import ensure_runner_connected

    host = await asyncio.to_thread(host_store.get_host, host_id)
    if host is None or host.sandbox_provider is None:
        return []
    bound = await asyncio.to_thread(conversation_store.list_conversations_by_host_id, host_id)
    candidates = [
        conv
        for conv in bound
        if _needs_wake(conv, app_state=app_state, runner_router=runner_router)
    ]
    if not candidates:
        return []
    _logger.info(
        "Managed host %s reconnected; resuming %d interrupted session(s)",
        host_id,
        len(candidates),
        extra=debug_event(
            "managed_host_session_recovery",
            host_id=host_id,
            session_ids=[conv.id for conv in candidates],
        ),
    )
    slots = asyncio.Semaphore(WAKE_CONCURRENCY)

    async def wake(conv: Conversation) -> str | None:
        async with slots:
            try:
                runner_client, _ = await ensure_runner_connected(
                    session_id=conv.id,
                    conv=conv,
                    app_state=app_state,
                    conversation_store=conversation_store,
                    runner_router=runner_router,
                )
            except Exception:  # noqa: BLE001 - one session's failure must not stop the rest
                _logger.warning(
                    "Failed to wake session %s on reconnected host %s",
                    conv.id,
                    host_id,
                    exc_info=True,
                    extra={"session_id": conv.id},
                )
                return None
        return conv.id if runner_client is not None else None

    results = await asyncio.gather(*(wake(conv) for conv in candidates))
    return [session_id for session_id in results if session_id is not None]
