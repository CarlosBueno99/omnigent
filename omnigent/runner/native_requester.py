"""Resolve native call provenance through the existing harness control channel."""

from __future__ import annotations

import logging

import httpx

from omnigent.harnesses.codex_native.execution import CodexExecutionIdentity
from omnigent.runtime.harnesses.process_manager import HarnessProcessManager, NoLiveHarnessError
from omnigent.runtime.requester import ExecutionRequester, InputRequester, ToolRequester

_logger = logging.getLogger(__name__)


async def resolve_codex_requester(
    manager: HarnessProcessManager | None,
    session_id: str,
    execution: CodexExecutionIdentity,
) -> ToolRequester:
    """Query only the live executor; missing or replaced processes have no authority."""
    unknown = ExecutionRequester(InputRequester()).snapshot()
    if manager is None:
        return unknown
    try:
        client = await manager.get_client(session_id, "any")
        response = await client.post(
            f"/v1/sessions/{session_id}/native-requester",
            json={"codex_execution": execution.to_wire()},
            timeout=10,
        )
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict) or body.get("requester") is None:
            return unknown
        return ToolRequester.model_validate(body["requester"])
    except (NoLiveHarnessError, httpx.HTTPError, ValueError):
        _logger.warning("Native requester could not be resolved for session %s", session_id)
        return unknown
