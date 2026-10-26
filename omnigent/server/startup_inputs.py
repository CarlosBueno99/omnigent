"""Persist startup and queued inputs until their runner can accept them."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import weakref
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from starlette.datastructures import Headers

from omnigent.db.db_models import current_workspace_id
from omnigent.entities.conversation import (
    Conversation,
    ConversationItem,
    ErrorData,
    MessageData,
    NewConversationItem,
    SlashCommandData,
    StartupInputOptions,
)
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runtime import pending_inputs, session_stream
from omnigent.server.host_registry import HostConnection, HostRegistry
from omnigent.server.schemas import SessionEventInput, SessionStartupInput
from omnigent.stores.conversation_store import (
    PENDING_INPUT_RESPONSE_ID,
    STARTUP_INPUT_LABEL,
    ConversationNotFoundError,
    ConversationStore,
)
from omnigent.stores.host_store import host_is_live

_logger = logging.getLogger(__name__)
STARTUP_LABEL = STARTUP_INPUT_LABEL


def initial_input_id(session_id: str) -> str:
    """Stable identity for a create request's first input."""
    return uuid5(NAMESPACE_URL, f"initial-input:{session_id}").hex


def input_content(item: ConversationItem) -> list[dict[str, Any]]:
    """Render a saved message or skill invocation as a pending bubble."""
    data = item.data.model_dump()
    if item.type == "slash_command":
        return [
            {"type": "input_text", "text": f"/{data['name']} {data.get('arguments', '')}".strip()}
        ]
    return data.get("content", [])


def is_queued_input(item: ConversationItem) -> bool:
    """Whether this saved input belongs to the user's deferred send queue."""
    return (
        item.response_id == PENDING_INPUT_RESPONSE_ID
        and isinstance(item.data, (MessageData, SlashCommandData))
        and bool(item.data.startup_options and item.data.startup_options.queued)
    )


def is_cancelled_input(item: ConversationItem) -> bool:
    """A stopped submission remains stored only to match a possible late receipt."""
    return isinstance(item.data, (MessageData, SlashCommandData)) and bool(
        item.data.startup_options and item.data.startup_options.cancelled
    )


async def restore_pending_inputs(
    store: ConversationStore, session_id: str, *, conversation: Conversation | None = None
) -> None:
    """Restore native receipt matching after an AP restart, without submitting again."""
    items = await asyncio.to_thread(store.list_pending_inputs, session_id)
    if not items:
        return
    conv = conversation or await asyncio.to_thread(store.get_conversation, session_id)
    for item in items:
        if (
            item.status == "incomplete"
            and pending_inputs.pending_id_for_stable_id(session_id, item.id) is None
        ):
            options = (
                item.data.startup_options
                if isinstance(item.data, (MessageData, SlashCommandData))
                else None
            )
            pending_id = pending_inputs.record(
                session_id,
                input_content(item),
                stable_id=item.id,
                created_by=item.created_by,
                background_titles_enabled=bool(options and options.background_titles_enabled),
            )
            if is_cancelled_input(item) or (
                conv is not None and conv.labels.get(STARTUP_LABEL) == "cancelled"
            ):
                pending_inputs.mark_interrupted(session_id, [pending_id])


@dataclass
class StartupInputContext:
    """The accepted sender's identity, independent of the browser's lifetime."""

    app: Any
    headers: Headers
    user_id: str | None
    input_id: str
    submitted: bool = False
    options: StartupInputOptions | None = None

    async def is_disconnected(self) -> bool:
        return False

    async def claim(self, store: ConversationStore, session_id: str) -> bool:
        # Leave the row queued throughout launch, authorization and policy checks.
        # A submitted native input is never replayed without its transcript receipt.
        self.submitted = await asyncio.to_thread(
            store.transition_pending_input,
            session_id,
            self.input_id,
            expected="completed" if self.options and self.options.queued else "in_progress",
            status="incomplete",
            expected_options=self.options,
            require_head=not (
                self.options and self.options.queued and not self.options.wait_for_idle
            ),
        )
        if self.submitted and self.options and self.options.queued:
            publish_queue_changed(session_id)
        return self.submitted


StartupDispatch = Callable[
    [StartupInputContext, str, SessionEventInput], Coroutine[Any, Any, dict[str, bool | str]]
]


_services: weakref.WeakSet[ManagedStartupInputs] = weakref.WeakSet()


def wake_startup_worker(host_id: str) -> None:
    """Prompt a running host worker to look for the next saved input now."""
    key = (current_workspace_id(), host_id)
    for service in _services:
        entry = service._workers.get(key)
        if entry is not None:
            entry[2].set()


def wake_queued_input_worker(session_id: str) -> None:
    """Wake the queue after a runner status or transcript receipt changes."""
    key = (current_workspace_id(), session_id)
    for service in _services:
        entry = service._session_workers.get(key)
        if entry is not None:
            with contextlib.suppress(RuntimeError):
                entry[0].get_loop().call_soon_threadsafe(entry[1].set)


def publish_queue_changed(session_id: str) -> None:
    """Invalidate every connected client's view of the durable queue."""
    session_stream.publish(
        session_id, {"type": "session.queue.updated", "conversation_id": session_id}
    )
    wake_queued_input_worker(session_id)


class ManagedStartupInputs:
    """Recover pending conversation items on the replica connected to their host."""

    def __init__(self, conversations: ConversationStore, hosts: HostRegistry) -> None:
        self.conversations = conversations
        self.hosts = hosts
        _services.add(self)
        self.dispatch: StartupDispatch | None = None
        self._workers: dict[
            tuple[int, str], tuple[HostConnection, asyncio.Task[None], asyncio.Event]
        ] = {}
        self._deliveries: dict[tuple[int, str], asyncio.Task[Any]] = {}
        self._session_workers: dict[tuple[int, str], tuple[asyncio.Task[None], asyncio.Event]] = {}

    async def enqueue(
        self,
        session_id: str,
        event: SessionEventInput,
        *,
        user_id: str | None,
        headers: Headers,
        first: bool = True,
    ) -> ConversationItem:
        from omnigent.server.background_session_titles import background_session_titles_enabled
        from omnigent.server.routes._auth_helpers import attribution_user
        from omnigent.server.routes._sessions.helpers import (
            _build_new_item,
            _stable_id_reuse_is_exact_retry,
        )

        queued = event.delivery == "queued"
        compact = queued and event.type == "compact"
        saved_event = (
            event.model_copy(
                update={
                    "type": "message",
                    "data": {
                        **event.data,
                        "role": "user",
                        "content": [{"type": "input_text", "text": "/compact"}],
                    },
                }
            )
            if compact
            else event
        )
        try:
            SessionStartupInput.model_validate(saved_event.model_dump())
            options = StartupInputOptions(
                model_override=event.model_override,
                tools=event.tools,
                background_titles_enabled=background_session_titles_enabled(headers),
                queued=queued,
                wait_for_idle=queued,
                event_type="compact" if compact else None,
                agent_id=event.data.get("agent_id"),
                reply_draft=event.data.get("reply_draft"),
            )
        except ValueError as exc:
            raise OmnigentError(str(exc), code=ErrorCode.INVALID_INPUT) from exc
        raw_id = event.data.get("stable_id")
        input_id = (
            initial_input_id(session_id)
            if first
            else (
                raw_id
                if isinstance(raw_id, str) and re.fullmatch(r"[0-9a-f]{32}", raw_id)
                else uuid4().hex
            )
        )
        if saved_event.type == "slash_command":
            saved_event = saved_event.model_copy(
                update={"data": {"agent": "pending", "arguments": "", **saved_event.data}}
            )
        item = _build_new_item(
            saved_event, PENDING_INPUT_RESPONSE_ID, created_by=user_id
        ).model_copy(
            # Older servers only submit in_progress startup rows; completed parks
            # deferred input safely throughout a rolling server upgrade.
            update={"stable_id": input_id, "status": "completed" if queued else "in_progress"}
        )
        item.data = item.data.model_copy(update={"startup_options": options})
        saved = await asyncio.to_thread(self.conversations.append, session_id, [item])
        if saved[0].deduplicated:
            existing = saved[0]
            retry = item
            if queued:
                existing = existing.model_copy(
                    update={
                        "created_by": attribution_user(existing.created_by),
                        "data": existing.data.model_copy(
                            update={"startup_options": None, "user_authored": False}
                        ),
                    }
                )
                retry = item.model_copy(
                    update={
                        "created_by": attribution_user(item.created_by),
                        "data": item.data.model_copy(
                            update={"startup_options": None, "user_authored": False}
                        ),
                    }
                )
                if (
                    isinstance(existing.data, SlashCommandData)
                    and isinstance(retry.data, SlashCommandData)
                    and existing.data.kind == retry.data.kind == "skill"
                ):
                    # Dispatch fills in the renderer's agent name after acceptance.
                    retry.data = retry.data.model_copy(update={"agent": existing.data.agent})
                if (
                    compact
                    and isinstance(existing.data, SlashCommandData)
                    and existing.data.kind == "command"
                    and existing.data.name == "compact"
                    and not existing.data.arguments
                ):
                    retry = retry.model_copy(update={"type": existing.type, "data": existing.data})
            if not _stable_id_reuse_is_exact_retry(existing, retry):
                raise OmnigentError(
                    "This input id already belongs to a different message", code=ErrorCode.CONFLICT
                )
        if queued:
            conv = await asyncio.to_thread(self.conversations.get_conversation, session_id)
            if conv is not None and conv.labels.get(STARTUP_LABEL) == "cancelled":
                await asyncio.to_thread(self.conversations.delete_label, session_id, STARTUP_LABEL)
            publish_queue_changed(session_id)
            return saved[0]
        if first:
            await asyncio.to_thread(
                self.conversations.set_labels, session_id, {STARTUP_LABEL: "pending"}
            )
        else:
            conv = await asyncio.to_thread(self.conversations.get_conversation, session_id)
            if conv is not None and STARTUP_LABEL not in conv.labels:
                # The startup finished as this was saved; keep later messages behind it.
                await asyncio.to_thread(
                    self.conversations.set_labels, session_id, {STARTUP_LABEL: "ready"}
                )
        return saved[0]

    async def record_launch_failure(self, session_id: str, reason: str) -> None:
        """
        Mark a failed create launch on the session, for replicas without its tracker.

        No sandbox is left to deliver the saved inputs, so they are failed and
        the transcript says why.
        """
        if await asyncio.to_thread(self.conversations.get_conversation, session_id) is None:
            return
        with contextlib.suppress(ConversationNotFoundError):
            await asyncio.to_thread(
                self.conversations.set_labels, session_id, {STARTUP_LABEL: "failed"}
            )
        undelivered = False
        for item in await asyncio.to_thread(self.conversations.list_pending_inputs, session_id):
            if item.status == "in_progress":
                undelivered |= await asyncio.to_thread(
                    self.conversations.transition_pending_input,
                    session_id,
                    item.id,
                    expected="in_progress",
                    status="failed",
                )
        if undelivered:
            await self._report_failure(
                session_id,
                "launch_failed",
                f"The message could not be delivered: {reason[:1000]}",
            )

    async def accepts(self, conv: Conversation, app: Any) -> bool:
        """Whether a new message must wait behind the session's startup."""
        state = conv.labels.get(STARTUP_LABEL)
        if state not in ("pending", "ready"):
            return False
        # A failed launch can't deliver anything; the message path reports why.
        # The tracker knows on the launching replica, the label everywhere.
        tracker = getattr(app.state, "managed_launches", None)
        launch = tracker.get(conv.id) if tracker is not None else None
        if launch is not None and launch.error is not None:
            return False
        if any(
            not is_queued_input(item) and item.status in ("in_progress", "incomplete")
            for item in await asyncio.to_thread(self.conversations.list_pending_inputs, conv.id)
        ):
            return True
        if state != "pending":
            return False
        # Nothing is saved yet: hold the message only while the launch can
        # still deliver it. A bound host that is gone needs the message path's
        # relaunch instead.
        if launch is not None or conv.host_id is None:
            return True
        host_store = getattr(app.state, "host_store", None)
        host = (
            await asyncio.to_thread(host_store.get_host, conv.host_id)
            if host_store is not None
            else None
        )
        return host is not None and host_is_live(host)

    async def pending_snapshot(
        self,
        session_id: str,
        *,
        items: list[ConversationItem] | None = None,
        conversation: Conversation | None = None,
    ) -> list[dict[str, Any]]:
        conv = conversation or await asyncio.to_thread(
            self.conversations.get_conversation, session_id
        )
        if conv is None or conv.labels.get(STARTUP_LABEL) == "cancelled":
            return []
        if items is None:
            items = await asyncio.to_thread(self.conversations.list_pending_inputs, session_id)
        return [
            {
                "pending_id": pending_inputs.make_pending_id(session_id, item.id),
                "content": input_content(item),
                **({"created_by": item.created_by} if item.created_by else {}),
            }
            for item in items
            if item.status != "failed"
            and not is_cancelled_input(item)
            and (not is_queued_input(item) or item.status == "incomplete")
        ]

    async def wake_session(self, app: Any, conv: Conversation) -> None:
        """Recover this session's queue without depending on a connected browser."""
        if conv.host_id is not None and STARTUP_LABEL in conv.labels:
            self.host_changed(app, conv.host_id)
        items = await asyncio.to_thread(self.conversations.list_pending_inputs, conv.id)
        if any(is_queued_input(item) and not is_cancelled_input(item) for item in items):
            self._start_session_worker(app, conv.id)

    async def runner_changed(self, app: Any, conversations: Sequence[Conversation]) -> None:
        """Restore hostless queues when their runner reconnects after a restart."""
        for conv in conversations:
            if conv.host_id is None:
                await self.wake_session(app, conv)

    def _start_session_worker(self, app: Any, session_id: str) -> None:
        key = (current_workspace_id(), session_id)
        existing = self._session_workers.get(key)
        if existing is not None and not existing[0].done():
            existing[1].set()
            return
        wake = asyncio.Event()

        async def serve() -> None:
            try:
                while True:
                    wake.clear()
                    try:
                        items = await asyncio.to_thread(
                            self.conversations.list_pending_inputs, session_id
                        )
                        if not any(
                            is_queued_input(item) and not is_cancelled_input(item)
                            for item in items
                        ):
                            return
                        await self._recover_session(app, session_id, items=items)
                    except Exception:
                        _logger.exception("Unable to recover queued input for %s", session_id)
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(wake.wait(), timeout=30)
            finally:
                self._session_workers.pop(key, None)

        self._session_workers[key] = (
            asyncio.create_task(serve(), name=f"queued-inputs-{session_id}"),
            wake,
        )

    def host_changed(self, app: Any, host_id: str) -> None:
        key = (current_workspace_id(), host_id)
        connection = self.hosts.get(host_id)
        existing = self._workers.get(key)
        if existing is not None:
            previous, worker, wake = existing
            if connection is previous and not worker.done():
                wake.set()
                return
            worker.cancel()
        if connection is None:
            return
        wake = asyncio.Event()

        async def serve() -> None:
            after = None
            delay = 0.25
            while self.hosts.get(host_id) is connection:
                busy = False
                try:
                    ids = await asyncio.to_thread(
                        self.conversations.list_host_conversation_ids, host_id, after=after
                    )
                    for session_id in ids:
                        if self.hosts.get(host_id) is not connection:
                            return
                        busy |= await self._recover_session(app, session_id, connection)
                    after = ids[-1] if len(ids) == 32 else None
                    # Local-host binding happens before queue acceptance. One
                    # reconnect sweep restores its queues; each then watches its session.
                    if after is None and not connection.registered_with_managed_token:
                        return
                    delay = 0.25 if busy or after else min(delay * 2, 30.0)
                except Exception:
                    _logger.exception("Unable to recover startup messages on host %s", host_id)
                    after = None
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(wake.wait(), timeout=delay)
                wake.clear()

        task = asyncio.create_task(serve(), name=f"startup-inputs-{host_id}")
        self._workers[key] = (connection, task, wake)

        def remove_worker(done: asyncio.Task[None]) -> None:
            if self._workers.get(key, (None, None, None))[1] is done:
                self._workers.pop(key, None)

        task.add_done_callback(remove_worker)

    async def _recover_session(
        self,
        app: Any,
        session_id: str,
        connection: HostConnection | None = None,
        *,
        items: list[ConversationItem] | None = None,
    ) -> bool:
        """Advance one session's startup here; return whether work is in flight."""
        conv = await asyncio.to_thread(self.conversations.get_conversation, session_id)
        if conv is None or (connection is not None and conv.host_id != connection.host_id):
            return False
        if conv.host_id is not None and self.hosts.get(conv.host_id) is None:
            return False
        state = conv.labels.get(STARTUP_LABEL)
        if state == "failed":
            return False
        if state == "pending" and conv.host_id is None and conv.runner_id is None:
            # Provisioning owns launch until a host or runner has been bound.
            return False
        if items is None:
            items = await asyncio.to_thread(self.conversations.list_pending_inputs, session_id)
        if state == "cancelled":
            if not items:
                await self._finish(session_id)
            return False
        # A refused input waits for an explicit retry without holding back the
        # ones behind it. One submitted to a native harness and awaiting its
        # transcript receipt still does: it may already have run.
        active = [
            item for item in items if item.status != "failed" and not is_cancelled_input(item)
        ]
        head = next(
            (item for item in active if item.status == "incomplete"),
            next(iter(active), None),
        )
        if head is not None and is_queued_input(head):
            if connection is not None:
                self._start_session_worker(app, session_id)
                return False
            # A user-selected send-now may pass earlier deferred inputs, but
            # never a submitted input whose delivery is still uncertain.
            if head.status == "completed":
                head = next(
                    (
                        item
                        for item in items
                        if is_queued_input(item)
                        and not is_cancelled_input(item)
                        and isinstance(item.data, (MessageData, SlashCommandData))
                        and item.status == "completed"
                        and item.data.startup_options is not None
                        and not item.data.startup_options.wait_for_idle
                        and not item.data.startup_options.requires_retry
                    ),
                    head,
                )
        if head is not None:
            # Delivered even without the label: a message saved while the label
            # was being cleared still belongs to this session's startup.
            key = (current_workspace_id(), session_id)
            if (
                head.status in ("in_progress", "completed")
                and key not in self._deliveries
                and (head.status == "in_progress" or is_queued_input(head))
            ):
                task = asyncio.create_task(
                    self._deliver(app, session_id, head), name=f"startup-input-{session_id}"
                )
                self._deliveries[key] = task
            return head.status == "in_progress"
        if state == "pending" and conv.runner_id is None and connection is not None:
            # The launch bound this host on a replica without its tunnel, so it
            # could not start the runner; start it here.
            from omnigent.server.routes.sessions import _launch_runner_on_host

            attempt = await _launch_runner_on_host(
                conv, self.conversations, self.hosts, connection
            )
            if attempt.error or attempt.error_code:
                reason = attempt.error or attempt.error_code or "the host refused the launch"
                await self._report_failure(
                    session_id,
                    f"launch_{attempt.runner_id}",
                    f"The agent could not start on the sandbox: {reason[:1000]}",
                )
            return True
        if state is not None and not items:
            # A runner is bound and nothing is left to deliver.
            await self._finish(session_id)
        return False

    async def _queue_ready(self, app: Any, conv: Conversation, item: ConversationItem) -> bool:
        from omnigent.server.routes._sessions.helpers import _compaction_started_at
        from omnigent.server.routes._sessions.orchestration import (
            _probe_runner_live_status,
            _session_is_starting,
        )
        from omnigent.server.routes.sessions import (
            _get_runner_client,
            _session_status_from_cache,
        )

        options = (
            item.data.startup_options
            if isinstance(item.data, (MessageData, SlashCommandData))
            else None
        )
        if options is None or options.requires_retry:
            return False
        if not options.wait_for_idle:
            return True
        if (
            _session_is_starting(conv.id)
            or conv.id in _compaction_started_at
            or _session_status_from_cache(conv.id, conv.live_status) == "failed"
        ):
            return False
        client = await _get_runner_client(conv.id, getattr(app.state, "runner_router", None))
        if client is None:
            # A live host can launch a stopped runner through normal dispatch.
            return (
                conv.host_id is not None
                and _session_status_from_cache(conv.id, conv.live_status) == "idle"
            )
        # Native PTY silence can publish idle in the middle of a turn. The
        # runner's active-turn registry is authoritative for deferred delivery.
        return await _probe_runner_live_status(client, conv.id, conv.runner_id) == "idle"

    async def _finish(self, session_id: str) -> None:
        """Clear a drained session's startup state so later reads skip it."""
        await asyncio.to_thread(self.conversations.delete_label, session_id, STARTUP_LABEL)
        # An input saved while the label was being cleared must still be delivered.
        if await asyncio.to_thread(self.conversations.list_pending_inputs, session_id):
            await asyncio.to_thread(
                self.conversations.set_labels, session_id, {STARTUP_LABEL: "ready"}
            )

    async def _deliver(self, app: Any, session_id: str, item: ConversationItem) -> None:
        if self.dispatch is None:
            return
        key = (current_workspace_id(), session_id)
        task = asyncio.current_task()
        assert task is not None
        if key in self._deliveries and self._deliveries[key] is not task:
            return
        self._deliveries[key] = task
        context: StartupInputContext | None = None
        queue_changed = False
        try:
            conv = await asyncio.to_thread(self.conversations.get_conversation, session_id)
            current = await asyncio.to_thread(self.conversations.get_item, session_id, item.id)
            if (
                conv is None
                or conv.labels.get(STARTUP_LABEL) == "cancelled"
                or current is None
                or current.response_id != PENDING_INPUT_RESPONSE_ID
                or is_cancelled_input(current)
                or current.status != ("completed" if is_queued_input(current) else "in_progress")
            ):
                return
            item = current
            if is_queued_input(item) and not await self._queue_ready(app, conv, item):
                return
            options = (
                item.data.startup_options
                if isinstance(item.data, (MessageData, SlashCommandData))
                else None
            )
            context = StartupInputContext(
                app,
                Headers(
                    {
                        "x-omnigent-background-session-titles": "on"
                        if options and options.background_titles_enabled
                        else "off",
                    }
                ),
                item.created_by,
                item.id,
                options=options,
            )
            queue_changed = is_queued_input(item)
            if options and options.agent_id is not None and options.agent_id != conv.agent_id:
                raise ValueError("The session's agent changed. Edit or remove the queued message.")
            event = SessionEventInput(
                type=options.event_type if options and options.event_type else item.type,
                data={**item.data.model_dump(exclude={"startup_options"}), "stable_id": item.id},
                model_override=options.model_override if options else None,
                tools=options.tools if options else None,
            )
            response = await self.dispatch(context, session_id, event)
            if response.get("denied"):
                raise ValueError(str(response.get("reason", "Input denied by policy")))
            if context.submitted:
                if not response.get("pending_id") and not response.get("item_id"):
                    # Native overlays such as /btw have no transcript echo.
                    from omnigent.server.routes.sessions import (
                        _build_new_item,
                        _publish_input_consumed,
                    )

                    receipt_event = event
                    if options and options.event_type == "compact":
                        receipt_event = SessionEventInput(
                            type="slash_command",
                            data={
                                "agent": conv.harness_override or "agent",
                                "kind": "command",
                                "name": "compact",
                                "arguments": "",
                            },
                        )
                    receipt = _build_new_item(
                        receipt_event, "startup_command", created_by=item.created_by
                    ).model_copy(update={"stable_id": item.id, "replaces_pending_id": item.id})
                    [saved] = await asyncio.to_thread(
                        self.conversations.append, session_id, [receipt]
                    )
                    _publish_input_consumed(
                        session_id,
                        saved,
                        cleared_pending_id=pending_inputs.make_pending_id(session_id, item.id),
                    )
                if not is_queued_input(item):
                    await asyncio.to_thread(
                        self.conversations.set_labels, session_id, {STARTUP_LABEL: "ready"}
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _logger.exception("Unable to deliver saved message for session %s", session_id)
            queue_changed = is_queued_input(item)
            if is_queued_input(item) and (context is None or not context.submitted):
                assert isinstance(item.data, (MessageData, SlashCommandData))
                options = item.data.startup_options
                assert options is not None
                await asyncio.to_thread(
                    self.conversations.transition_pending_input,
                    session_id,
                    item.id,
                    expected="completed",
                    status="completed",
                    expected_options=options,
                    options=options.model_copy(update={"requires_retry": True}),
                )
            elif context is None or not context.submitted:
                await asyncio.to_thread(
                    self.conversations.transition_pending_input,
                    session_id,
                    item.id,
                    expected="in_progress",
                    status="failed",
                )
            uncertain = bool(context and context.submitted)
            await self._report_failure(
                session_id,
                item.id,
                (
                    "Delivery could not be confirmed, so later messages wait behind it. "
                    "Check the terminal; if the agent never got it, press Stop, which "
                    "also clears the waiting messages, then send them again. "
                    if uncertain
                    else "The message is saved but could not be delivered. "
                )
                + str(exc)[:1000],
            )
        finally:
            self._deliveries.pop(key, None)
            if queue_changed:
                publish_queue_changed(session_id)

    async def retry(self, session_id: str) -> bool:
        changed = False
        for item in await asyncio.to_thread(self.conversations.list_pending_inputs, session_id):
            if is_queued_input(item) and item.status == "completed":
                assert isinstance(item.data, (MessageData, SlashCommandData))
                options = item.data.startup_options
                assert options is not None
                if options.requires_retry:
                    changed |= await asyncio.to_thread(
                        self.conversations.transition_pending_input,
                        session_id,
                        item.id,
                        expected="completed",
                        status="completed",
                        expected_options=options,
                        options=options.model_copy(update={"requires_retry": False}),
                    )
            elif item.status == "failed":
                changed |= await asyncio.to_thread(
                    self.conversations.transition_pending_input,
                    session_id,
                    item.id,
                    expected="failed",
                    status="in_progress",
                )
        if changed:
            publish_queue_changed(session_id)
        return changed

    async def cancel(self, session_id: str) -> None:
        items = await asyncio.to_thread(self.conversations.list_pending_inputs, session_id)
        conv = await asyncio.to_thread(self.conversations.get_conversation, session_id)
        if not items and (conv is None or conv.labels.get(STARTUP_LABEL) != "pending"):
            return
        await asyncio.to_thread(
            self.conversations.set_labels, session_id, {STARTUP_LABEL: "cancelled"}
        )
        key = (current_workspace_id(), session_id)
        task = self._deliveries.get(key)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        for item in items:
            if item.status != "incomplete":
                removed = await asyncio.to_thread(
                    self.conversations.transition_pending_input,
                    session_id,
                    item.id,
                    expected=item.status,
                    status=None,
                )
                if removed:
                    continue
                # A different replica may have claimed it after our snapshot.
                current = await asyncio.to_thread(self.conversations.get_item, session_id, item.id)
                if current is None or current.response_id != PENDING_INPUT_RESPONSE_ID:
                    continue
                item = current
            if item.status == "incomplete":
                options = (
                    item.data.startup_options
                    if isinstance(item.data, (MessageData, SlashCommandData))
                    else None
                ) or StartupInputOptions()
                await asyncio.to_thread(
                    self.conversations.transition_pending_input,
                    session_id,
                    item.id,
                    expected="incomplete",
                    status="incomplete",
                    options=options.model_copy(update={"cancelled": True}),
                )
                pending_inputs.mark_interrupted(
                    session_id, [pending_inputs.make_pending_id(session_id, item.id)]
                )
        publish_queue_changed(session_id)

    async def _report_failure(self, session_id: str, failure_id: str, message: str) -> None:
        """Record a startup failure in the transcript and the session's status."""
        from omnigent.server.routes.sessions import _publish_sandbox_status, _publish_status

        if await asyncio.to_thread(self.conversations.get_conversation, session_id) is None:
            return

        await asyncio.to_thread(
            self.conversations.append,
            session_id,
            [
                NewConversationItem(
                    type="error",
                    response_id=f"startup_{failure_id}",
                    stable_id=uuid5(
                        NAMESPACE_URL, f"startup-failure:{session_id}:{failure_id}"
                    ).hex,
                    data=ErrorData(
                        source="execution", code="startup_input_failed", message=message
                    ),
                )
            ],
        )
        _publish_sandbox_status(session_id, "failed", message)
        _publish_status(session_id, "failed", failure_origin="startup_input_failed")

    async def shutdown(self) -> None:
        tasks = (
            [task for _, task, _ in self._workers.values()]
            + [task for task, _ in self._session_workers.values()]
            + list(self._deliveries.values())
        )
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._workers.clear()
        self._session_workers.clear()
