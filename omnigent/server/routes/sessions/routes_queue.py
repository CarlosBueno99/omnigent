"""Read and manage messages accepted for a later session turn."""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from omnigent.entities.conversation import (
    Conversation,
    ConversationItem,
    MessageData,
    SlashCommandData,
)
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import LEVEL_EDIT, LEVEL_READ, AuthProvider
from omnigent.server.routes._auth_helpers import get_user_id, require_access_and_level
from omnigent.server.routes._content_type import require_json_content_type
from omnigent.server.routes._errors import session_not_found
from omnigent.server.startup_inputs import is_queued_input, publish_queue_changed
from omnigent.stores.conversation_store import MAX_PENDING_INPUTS, ConversationStore
from omnigent.stores.permission_store import PermissionStore


class QueueAction(BaseModel):
    action: Literal["steer", "retry", "reorder"]
    before_id: str | None = None


def queue_item(item: ConversationItem) -> dict[str, Any]:
    """Keep storage options private while returning the editable input."""
    options = getattr(item.data, "startup_options", None)
    result: dict[str, Any] = {
        "id": item.id,
        "type": options.event_type if options and options.event_type else item.type,
        "status": "failed" if options and options.requires_retry else "in_progress",
        "created_by": item.created_by,
        "data": {
            **item.data.model_dump(mode="json", exclude={"startup_options"}),
            "stable_id": item.id,
        },
    }
    if options is not None:
        if options.reply_draft is not None:
            result["data"]["reply_draft"] = options.reply_draft
        if options.agent_id is not None:
            result["data"]["agent_id"] = options.agent_id
    return result


def register_queue_routes(
    router: APIRouter,
    *,
    conversation_store: ConversationStore,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> None:
    async def authorize(request: Request, session_id: str, *, edit: bool) -> Conversation:
        access = await require_access_and_level(
            get_user_id(request, auth_provider),
            session_id,
            LEVEL_EDIT if edit else LEVEL_READ,
            permission_store,
            conversation_store,
        )
        conv = access.conversation or await asyncio.to_thread(
            conversation_store.get_conversation, session_id
        )
        if conv is None:
            raise session_not_found()
        return conv

    async def editable_item(session_id: str, item_id: str) -> ConversationItem:
        item = await asyncio.to_thread(conversation_store.get_item, session_id, item_id)
        if item is None:
            raise OmnigentError("This queued message was removed.", code=ErrorCode.NOT_FOUND)
        if not is_queued_input(item) or item.status != "completed":
            raise OmnigentError(
                "This message has already left the queue. Refresh the session.",
                code=ErrorCode.CONFLICT,
            )
        return item

    @router.get("/sessions/{session_id}/queue")
    async def list_queue(
        request: Request,
        session_id: str,
        after: int | None = Query(default=None, ge=0),
        limit: int = Query(default=32, ge=1, le=MAX_PENDING_INPUTS),
    ) -> dict[str, Any]:
        await authorize(request, session_id, edit=False)
        items, cursor = await asyncio.to_thread(
            conversation_store.list_pending_inputs_page, session_id, after=after, limit=limit
        )
        return {
            "data": [
                queue_item(item)
                for item in items
                if is_queued_input(item) and item.status == "completed"
            ],
            "next_cursor": cursor,
        }

    @router.delete("/sessions/{session_id}/queue/{item_id}")
    async def remove_queue_item(request: Request, session_id: str, item_id: str) -> dict[str, Any]:
        await authorize(request, session_id, edit=True)
        item = await editable_item(session_id, item_id)
        assert isinstance(item.data, (MessageData, SlashCommandData))
        removed = await asyncio.to_thread(
            conversation_store.transition_pending_input,
            session_id,
            item_id,
            expected=item.status,
            status=None,
            queued_only=True,
            expected_options=item.data.startup_options,
        )
        if not removed:
            raise OmnigentError(
                "The queue changed. Refresh and try again.", code=ErrorCode.CONFLICT
            )
        publish_queue_changed(session_id)
        return queue_item(item)

    @router.patch(
        "/sessions/{session_id}/queue/{item_id}",
        dependencies=[Depends(require_json_content_type)],
    )
    async def change_queue_item(
        request: Request, session_id: str, item_id: str, body: QueueAction
    ) -> dict[str, bool]:
        conv = await authorize(request, session_id, edit=True)
        item = await editable_item(session_id, item_id)
        if body.action == "reorder":
            changed = await asyncio.to_thread(
                conversation_store.reorder_pending_input,
                session_id,
                item_id,
                before_id=body.before_id,
            )
        else:
            assert isinstance(item.data, (MessageData, SlashCommandData))
            options = item.data.startup_options
            assert options is not None
            changed = await asyncio.to_thread(
                conversation_store.transition_pending_input,
                session_id,
                item_id,
                expected=item.status,
                status="completed",
                queued_only=True,
                expected_options=options,
                options=options.model_copy(
                    update={"wait_for_idle": False, "requires_retry": False}
                ),
            )
        if not changed:
            raise OmnigentError(
                "The queue changed. Refresh and try again.", code=ErrorCode.CONFLICT
            )
        publish_queue_changed(session_id)
        await request.app.state.startup_inputs.wake_session(request.app, conv)
        return {"updated": True}
