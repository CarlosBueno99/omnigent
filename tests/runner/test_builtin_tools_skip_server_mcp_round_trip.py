"""Built-in tools dispatch locally instead of taking a server ``/mcp`` round trip.

A runner-dispatched session hands every locally dispatched tool a
``ProxyMcpManager``, and ``execute_tool`` used to route anything with an
``mcp_manager`` through ``call_tool`` — a ``POST /v1/sessions/{id}/mcp``
that tunnels straight back to the same runner. Built-in ``sys_*`` tools run
on the runner either way, so they now execute locally and enforce
TOOL_CALL/TOOL_RESULT policy through the server's ``/policies/evaluate``
endpoint (which short-circuits when no policy applies to the phase).
Spec-declared MCP-server tools, and the server-intercepted
``sys_advise_models``, keep the ``/mcp`` round trip.
"""

from __future__ import annotations

import json
from typing import Any, cast

import httpx
import pytest

from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.runner import tool_dispatch
from omnigent.runner.mcp_manager import RunnerMcpManager
from omnigent.runner.proxy_mcp_manager import ProxyMcpManager
from omnigent.runner.tool_dispatch import execute_tool
from omnigent.spec.types import AgentSpec, ExecutorSpec

pytestmark = pytest.mark.asyncio

_SESSION_ID = "conv_omni12775"
_LOCAL_OUTPUT = "ran locally on the runner"
_TUNNELED_OUTPUT = "served by the server /mcp round trip"


def _recording_proxy_client(paths: list[str]) -> httpx.AsyncClient:
    def _handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "content": [{"type": "text", "text": _TUNNELED_OUTPUT}],
                    "isError": False,
                },
            },
        )

    return httpx.AsyncClient(
        transport=httpx.MockTransport(_handler),
        base_url="http://ap-server",
    )


def _claude_sdk_spec() -> AgentSpec:
    return AgentSpec(
        spec_version=1,
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-sdk"}),
        os_env=OSEnvSpec(type="caller_process", sandbox=OSEnvSandboxSpec(type="none")),
    )


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("sys_os_read", json.dumps({"path": "notes.txt"})),
        ("sys_os_shell", json.dumps({"command": "true"})),
    ],
)
async def test_builtin_tool_does_not_round_trip_through_server_mcp(
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    arguments: str,
) -> None:
    async def _fake_os_env(name: str, *_a: Any, **_kw: Any) -> str:
        del name
        return _LOCAL_OUTPUT

    monkeypatch.setattr(tool_dispatch, "_execute_os_env_tool", _fake_os_env)

    paths: list[str] = []
    client = _recording_proxy_client(paths)
    proxy = ProxyMcpManager(_SESSION_ID, client)
    try:
        output = await execute_tool(
            tool_name=tool_name,
            arguments=arguments,
            agent_spec=_claude_sdk_spec(),
            conversation_id=_SESSION_ID,
            mcp_manager=cast(RunnerMcpManager, proxy),
            effective_harness="claude-sdk",
        )
    finally:
        await client.aclose()

    mcp_posts = [p for p in paths if p == f"/v1/sessions/{_SESSION_ID}/mcp"]
    assert mcp_posts == [], (
        f"built-in {tool_name!r} took a server /mcp round trip "
        f"(POSTed to {mcp_posts}); it must dispatch locally on the runner"
    )
    assert output == _LOCAL_OUTPUT, (
        f"built-in {tool_name!r} output came from the server proxy "
        f"({output!r}); it must execute locally"
    )


def _recording_policy_client(
    calls: list[tuple[str, str]],
    *,
    tool_call: dict[str, Any] | None = None,
    tool_result: dict[str, Any] | None = None,
) -> httpx.AsyncClient:
    """Server client that records ``/policies/evaluate`` calls by phase.

    ``calls`` receives ``(path, phase)`` per request. ``tool_call`` and
    ``tool_result`` override the verdict for that phase; both default to ALLOW.
    """

    def _handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        phase = body["event"]["type"]
        calls.append((request.url.path, phase))
        if phase == "PHASE_TOOL_CALL":
            verdict = tool_call or {"result": "POLICY_ACTION_ALLOW"}
        else:
            verdict = tool_result or {"result": "POLICY_ACTION_ALLOW"}
        return httpx.Response(200, json=verdict)

    return httpx.AsyncClient(
        transport=httpx.MockTransport(_handler),
        base_url="http://ap-server",
    )


async def test_builtin_policy_enforced_via_policies_evaluate_not_mcp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local dispatch still gates built-ins — through /policies/evaluate, not /mcp."""
    executed: list[str] = []

    async def _fake_os_env(name: str, *_a: Any, **_kw: Any) -> str:
        executed.append(name)
        return _LOCAL_OUTPUT

    monkeypatch.setattr(tool_dispatch, "_execute_os_env_tool", _fake_os_env)

    mcp_paths: list[str] = []
    policy_calls: list[tuple[str, str]] = []
    proxy_client = _recording_proxy_client(mcp_paths)
    policy_client = _recording_policy_client(policy_calls)
    proxy = ProxyMcpManager(_SESSION_ID, proxy_client)
    try:
        output = await execute_tool(
            tool_name="sys_os_read",
            arguments=json.dumps({"path": "notes.txt"}),
            agent_spec=_claude_sdk_spec(),
            conversation_id=_SESSION_ID,
            server_client=policy_client,
            mcp_manager=cast(RunnerMcpManager, proxy),
            effective_harness="claude-sdk",
        )
    finally:
        await proxy_client.aclose()
        await policy_client.aclose()

    assert mcp_paths == [], "a built-in must not round trip through server /mcp"
    assert output == _LOCAL_OUTPUT
    assert executed == ["sys_os_read"], "the built-in must execute locally on ALLOW"
    assert [phase for _path, phase in policy_calls] == [
        "PHASE_TOOL_CALL",
        "PHASE_TOOL_RESULT",
    ], f"built-in policy must be enforced via /policies/evaluate; saw {policy_calls}"
    assert all(
        path == f"/v1/sessions/{_SESSION_ID}/policies/evaluate" for path, _phase in policy_calls
    )


async def test_builtin_tool_call_policy_deny_blocks_local_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A TOOL_CALL deny must stop the built-in before it runs on the runner."""
    executed: list[str] = []

    async def _fake_os_env(name: str, *_a: Any, **_kw: Any) -> str:
        executed.append(name)
        return _LOCAL_OUTPUT

    monkeypatch.setattr(tool_dispatch, "_execute_os_env_tool", _fake_os_env)

    mcp_paths: list[str] = []
    policy_calls: list[tuple[str, str]] = []
    proxy_client = _recording_proxy_client(mcp_paths)
    policy_client = _recording_policy_client(
        policy_calls,
        tool_call={"result": "POLICY_ACTION_DENY", "reason": "blocked by test policy"},
    )
    proxy = ProxyMcpManager(_SESSION_ID, proxy_client)
    try:
        output = await execute_tool(
            tool_name="sys_os_read",
            arguments=json.dumps({"path": "secret.txt"}),
            agent_spec=_claude_sdk_spec(),
            conversation_id=_SESSION_ID,
            server_client=policy_client,
            mcp_manager=cast(RunnerMcpManager, proxy),
            effective_harness="claude-sdk",
        )
    finally:
        await proxy_client.aclose()
        await policy_client.aclose()

    assert executed == [], "a denied built-in must not execute locally"
    assert output == json.dumps({"error": "Denied by policy: blocked by test policy"})
    assert mcp_paths == []
    assert [phase for _path, phase in policy_calls] == ["PHASE_TOOL_CALL"], (
        "a TOOL_CALL deny must short-circuit before TOOL_RESULT"
    )


async def test_server_intercepted_builtin_still_round_trips_through_mcp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sys_advise_models has no local branch, so it keeps the server /mcp round trip."""
    mcp_paths: list[str] = []
    policy_calls: list[tuple[str, str]] = []
    proxy_client = _recording_proxy_client(mcp_paths)
    policy_client = _recording_policy_client(policy_calls)
    proxy = ProxyMcpManager(_SESSION_ID, proxy_client)
    try:
        output = await execute_tool(
            tool_name="sys_advise_models",
            arguments=json.dumps({"question": "which model?"}),
            agent_spec=_claude_sdk_spec(),
            conversation_id=_SESSION_ID,
            server_client=policy_client,
            mcp_manager=cast(RunnerMcpManager, proxy),
            effective_harness="claude-sdk",
        )
    finally:
        await proxy_client.aclose()
        await policy_client.aclose()

    assert mcp_paths == [f"/v1/sessions/{_SESSION_ID}/mcp"], (
        "sys_advise_models is intercepted server-side and must keep the /mcp round trip"
    )
    assert output == _TUNNELED_OUTPUT
    assert policy_calls == [], (
        "a proxied built-in enforces policy on the /mcp path, not /policies/evaluate"
    )
