"""Requester identity across persistent SDK callbacks and the real turn context.

The nearest adapter process fixture scripts MockExecutor events; its setup cannot
retain a real SDK tool callback across executions. These scenarios need controlled
callback/injection barriers and reuse the real scaffold's parsing, context creation,
SSE serialization and teardown rather than the adjacent recording-context stand-ins.

The OpenAI scenario runs the actual SDK in-process with a mocked HTTP model transport.
The Claude scenario retains a real MCP handler in a simulated persistent reader task.
Neither validates a live provider or the SDK harness subprocess startup path; that
requires a separate process regression before landing.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from openai import AsyncOpenAI
from starlette.responses import StreamingResponse

from omnigent.inner.claude_sdk_executor import _build_mcp_tools
from omnigent.inner.executor import (
    Executor,
    ExecutorConfig,
    InjectionDeliveryAmbiguous,
    ToolCallRequest,
    TurnComplete,
)
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter
from omnigent.runtime.harnesses._scaffold import MessageEvent, PolicyVerdictPayload, TurnContext
from omnigent.runtime.requester import (
    REQUESTER_FIELD,
    InputRequester,
    ToolRequester,
    current_tool_requester,
)
from tests._helpers.polling import poll_until_async


class _CallbackExecutor(Executor):
    """Keep one executor alive while the test controls callback and injection delivery."""

    def __init__(self) -> None:
        self.configs: asyncio.Queue[ExecutorConfig] = asyncio.Queue()
        self.finish = asyncio.Event()
        self.injected = asyncio.Event()
        self.finish_injection = asyncio.Event()
        self.injection_outcome: bool | BaseException = True

    async def run_turn(self, messages, tools, system_prompt, config=None):
        assert config is not None
        self.configs.put_nowait(config)
        await self.finish.wait()
        yield TurnComplete(response="done")

    async def enqueue_session_message(self, session_key, content):
        self.injected.set()
        await self.finish_injection.wait()
        if isinstance(self.injection_outcome, BaseException):
            raise self.injection_outcome
        return self.injection_outcome


def _message(origin: InputRequester | None, **fields: Any) -> MessageEvent:
    return MessageEvent.model_validate(
        {
            "type": "message",
            "role": "user",
            "model": "test-agent",
            "content": "test input",
            **({REQUESTER_FIELD: origin.model_dump()} if origin is not None else {}),
            **fields,
        }
    )


@asynccontextmanager
async def _turn(
    adapter: ExecutorAdapter, executor: _CallbackExecutor, origin: InputRequester | None
) -> AsyncIterator[tuple[TurnContext, asyncio.Queue[dict[str, Any]], ExecutorConfig]]:
    """Use scaffold decoding, context construction, and SSE teardown for each execution."""
    executor.finish.clear()
    response = await adapter._start_or_inject_turn(
        _message(origin).to_create_request(), session_id="test-session"
    )
    assert isinstance(response, StreamingResponse)
    ctx = adapter._active_turn_ctx
    assert ctx is not None
    events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def read_stream() -> None:
        async for frame in response.body_iterator:
            assert isinstance(frame, bytes)
            events.put_nowait(json.loads(frame.decode().split("data: ", 1)[1]))

    task = asyncio.create_task(read_stream())
    try:
        config = await asyncio.wait_for(executor.configs.get(), timeout=3)
        yield ctx, events, config
    finally:
        ctx._cancel_pending()
        executor.finish.set()
        await asyncio.wait_for(task, timeout=3)


async def _next_call(events: asyncio.Queue[dict[str, Any]]) -> dict[str, Any]:
    async with asyncio.timeout(3):
        while True:
            event = await events.get()
            item = event.get("item", {})
            if item.get("type") == "function_call" and item.get("status") == "action_required":
                return item


async def test_persistent_claude_callback_cannot_inherit_either_turns_requester() -> None:
    executor = _CallbackExecutor()
    adapter = ExecutorAdapter(executor_factory=lambda: executor)
    owner = InputRequester(user_id="owner", account_generation="owner-generation")
    collaborator = InputRequester(user_id="collaborator", account_generation="other-generation")
    calls: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    results: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async with _turn(adapter, executor, owner) as (first, _, config):
        assert config.requester == owner
        first_requester = first.requester.snapshot()
        bridge = executor._tool_executor
        [tool] = _build_mcp_tools(
            [{"name": "echo", "parameters": {"type": "object", "properties": {}}}], bridge
        )

        async def persistent_reader() -> None:
            assert current_tool_requester.get() == first_requester
            while True:
                results.put_nowait(await tool.handler(await calls.get()))

        token = current_tool_requester.set(first_requester)
        try:
            reader = asyncio.create_task(persistent_reader())
        finally:
            current_tool_requester.reset(token)

    try:
        async with _turn(adapter, executor, collaborator) as (second, events, config):
            assert config.requester == collaborator
            assert executor._tool_executor is bridge
            adapter._translate_event(
                ToolCallRequest(name="mcp__omnigent__echo", metadata={"call_id": "observed"}),
                second,
            )
            # Model arguments and inherited task context both contain plausible authority.
            calls.put_nowait({REQUESTER_FIELD: second.requester.snapshot().model_dump()})
            item = await _next_call(events)
            bound = ToolRequester.model_validate(item[REQUESTER_FIELD])
            assert bound.origin.user_id is None
            assert bound.execution_id not in {
                first_requester.execution_id,
                second.requester.execution_id,
            }
            assert item["call_id"] == "observed"
            assert second._complete_tool(item["call_id"], '{"ok": true}')
            result = await asyncio.wait_for(results.get(), timeout=3)
            assert json.loads(result["content"][0]["text"]) == {"ok": True}
            assert adapter._executor is executor
    finally:
        reader.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reader


async def test_delayed_bound_callback_cannot_rebind_to_a_new_execution() -> None:
    executor = _CallbackExecutor()
    adapter = ExecutorAdapter(executor_factory=lambda: executor)
    async with _turn(adapter, executor, InputRequester(user_id="first")) as (first, _, _):
        original = first.requester.snapshot()

    async with _turn(adapter, executor, InputRequester(user_id="second")) as (second, events, _):
        adapter._translate_event(
            ToolCallRequest(name="echo", metadata={"call_id": "reused-id"}), second
        )
        result = await executor._tool_executor("echo", {}, call_id="reused-id", requester=original)
        assert result["code"] == "runner_turn_context_desync"
        assert not first._pending_tool_calls
        assert not second._pending_tool_calls
        assert list(adapter._pending_mcp_call_ids) == ["reused-id"]
        assert adapter._executor is executor
        assert adapter._current_ctx is second

        dispatch = asyncio.create_task(
            executor._tool_executor(
                "echo", {}, call_id="reused-id", requester=second.requester.snapshot()
            )
        )
        item = await _next_call(events)
        assert ToolRequester.model_validate(item[REQUESTER_FIELD]).origin.user_id == "second"
        assert second._complete_tool("reused-id", "{}")
        assert await dispatch == {}
        second.cancelled.set()
        cancelled = await executor._tool_executor("echo", {})
        assert cancelled["code"] == "runner_turn_context_desync"
        assert not second._pending_tool_calls


async def test_bound_parallel_calls_keep_exact_ids_and_immutable_requesters() -> None:
    executor = _CallbackExecutor()
    adapter = ExecutorAdapter(executor_factory=lambda: executor)
    async with _turn(adapter, executor, InputRequester(user_id="first")) as (ctx, events, _):
        original = ctx.requester.snapshot()
        for call_id in ("one", "two"):
            adapter._translate_event(
                ToolCallRequest(name="echo", metadata={"call_id": call_id}), ctx
            )
        first_dispatch = asyncio.create_task(
            executor._tool_executor("echo", {}, call_id="two", requester=original)
        )
        first_item = await _next_call(events)
        ctx.requester.consume(InputRequester(user_id="different"))
        second_dispatch = asyncio.create_task(
            executor._tool_executor("echo", {}, call_id="one", requester=original)
        )
        second_item = await _next_call(events)
        assert (first_item["call_id"], second_item["call_id"]) == ("two", "one")
        assert ToolRequester.model_validate(first_item[REQUESTER_FIELD]) == original
        assert ToolRequester.model_validate(second_item[REQUESTER_FIELD]).origin.user_id is None
        assert not adapter._pending_mcp_call_ids
        assert ctx._complete_tool("one", '{"order": 1}')
        assert ctx._complete_tool("two", '{"order": 2}')
        assert await first_dispatch == {"order": 2}
        assert await second_dispatch == {"order": 1}


@pytest.mark.parametrize("outcome", [True, False, "ambiguous", "error", "cancelled"])
@pytest.mark.parametrize("injection_user", ["first", "different", None])
async def test_injection_authority_is_restricted_until_delivery_is_known(
    outcome: bool | str, injection_user: str | None
) -> None:
    executor = _CallbackExecutor()
    adapter = ExecutorAdapter(executor_factory=lambda: executor)
    executor.injection_outcome = {
        "ambiguous": InjectionDeliveryAmbiguous("delivery reply lost"),
        "error": RuntimeError("delivery failed"),
        "cancelled": asyncio.CancelledError(),
    }.get(outcome, outcome)
    async with _turn(adapter, executor, InputRequester(user_id="first")) as (ctx, events, _):
        original = ctx.requester.snapshot()
        response = await adapter._start_or_inject_turn(
            _message(
                InputRequester(user_id=injection_user) if injection_user else None,
                injection_id="injection",
            ).to_create_request()
        )
        assert response.status_code == 204
        await asyncio.wait_for(executor.injected.wait(), timeout=3)
        assert ctx.requester.snapshot().origin.user_id is None
        dispatch = asyncio.create_task(
            executor._tool_executor("echo", {}, call_id="pending", requester=original)
        )
        item = await _next_call(events)
        assert ToolRequester.model_validate(item[REQUESTER_FIELD]).origin.user_id is None
        assert ctx._complete_tool("pending", "{}")
        assert await dispatch == {}
        executor.finish_injection.set()
        await poll_until_async(lambda: not ctx.requester._pending, timeout_s=3)
        expected = "first" if outcome is False or injection_user == "first" else None
        assert ctx.requester.snapshot().origin.user_id == expected
        assert original.origin.user_id == "first"


async def test_native_requester_only_resolves_on_existing_codex_executor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.harnesses.codex_native.execution import CodexExecutionIdentity
    from omnigent.inner.codex_native_executor import CodexNativeExecutor
    from omnigent.runtime.requester import ExecutionRequester

    def unexpected_factory() -> Executor:
        pytest.fail("requester lookup must not construct an executor")

    adapter = ExecutorAdapter(executor_factory=unexpected_factory)
    identity = CodexExecutionIdentity("session", "thread", "turn")
    assert await adapter.native_tool_requester(identity.to_wire()) is None
    adapter._executor = _CallbackExecutor()
    assert await adapter.native_tool_requester(identity.to_wire()) is None

    executor = CodexNativeExecutor(bridge_dir=tmp_path)
    expected = ExecutionRequester(InputRequester(user_id="origin")).snapshot()
    resolve = AsyncMock(return_value=expected)
    monkeypatch.setattr(executor, "resolve_tool_requester", resolve)
    adapter._executor = executor
    assert await adapter.native_tool_requester({"thread_id": "thread"}) is None
    resolve.assert_not_called()
    assert await adapter.native_tool_requester(identity.to_wire()) is expected
    resolve.assert_awaited_once_with(identity)


@pytest.mark.parametrize("injection_user", ["first", "different", None])
async def test_codex_injection_forwards_the_protected_input_requester(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, injection_user: str | None
) -> None:
    from omnigent.inner.codex_native_executor import CodexNativeExecutor

    controller = _CallbackExecutor()
    executor = CodexNativeExecutor(bridge_dir=tmp_path)
    monkeypatch.setattr(executor, "run_turn", controller.run_turn)
    enqueue = AsyncMock(return_value=True)
    monkeypatch.setattr(executor, "enqueue_session_message", enqueue)
    adapter = ExecutorAdapter(executor_factory=lambda: executor)
    origin = InputRequester(user_id=injection_user) if injection_user else None
    async with _turn(adapter, controller, InputRequester(user_id="first")):
        await adapter._start_or_inject_turn(
            _message(origin, injection_id="steer").to_create_request()
        )
        await poll_until_async(lambda: enqueue.await_count == 1, timeout_s=3)
        assert enqueue.call_args.args == (adapter._session_key, "test input")
        forwarded = enqueue.call_args.kwargs["requester"]
        assert isinstance(forwarded, InputRequester)
        assert forwarded.user_id == injection_user
        if origin is not None:
            assert forwarded == origin


async def test_real_openai_sdk_cached_tools_use_each_runs_execution() -> None:
    from omnigent.inner.openai_agents_sdk_executor import OpenAIAgentsSDKExecutor

    requests: list[dict[str, Any]] = []
    release_delayed_call = asyncio.Event()
    delayed_call: asyncio.Task[dict[str, Any]] | None = None

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal delayed_call
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            state = executor._session_states[adapter._session_key]
            callback = state.agent.tools[0].on_invoke_tool

            async def invoke_later() -> dict[str, Any]:
                await release_delayed_call.wait()
                return await callback(None, "{}")

            delayed_call = asyncio.create_task(invoke_later())
        elif len(requests) == 3:
            release_delayed_call.set()
        tool_result = body["messages"][-1]["role"] == "tool"
        delta = (
            {"content": "done"}
            if tool_result
            else {
                "role": "assistant",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": f"provider-call-{len(requests)}",
                        "type": "function",
                        "function": {"name": "echo", "arguments": "{}"},
                    }
                ],
            }
        )
        chunk = {
            "id": f"completion-{len(requests)}",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "test-model",
            "choices": [
                {
                    "index": 0,
                    "delta": delta,
                    "finish_reason": "stop" if tool_result else "tool_calls",
                }
            ],
        }
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n",
        )

    async with AsyncOpenAI(
        api_key="test-key",
        base_url="http://model.test/v1",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    ) as client:
        executor = OpenAIAgentsSDKExecutor(client=client, model="test-model", use_responses=False)
        executor._policy_evaluator = AsyncMock(
            return_value=PolicyVerdictPayload(action="POLICY_ACTION_ALLOW")
        )
        adapter = ExecutorAdapter(executor_factory=lambda: executor)
        history: list[dict[str, str]] = []
        callbacks = []
        execution_ids = set()
        try:
            for user in ("first", "second", None):
                history.append({"role": "user", "content": f"input from {user}"})
                origin = InputRequester(user_id=user) if user else None
                response = await adapter._start_or_inject_turn(
                    _message(
                        origin,
                        content=list(history),
                        tools=[
                            {
                                "type": "function",
                                "name": "echo",
                                "parameters": {"type": "object", "properties": {}},
                            }
                        ],
                    ).to_create_request(),
                    session_id="sdk-session",
                )
                assert isinstance(response, StreamingResponse)
                ctx = adapter._active_turn_ctx
                assert ctx is not None
                dispatched = []
                terminal = None
                async with asyncio.timeout(10):
                    async for frame in response.body_iterator:
                        event = json.loads(frame.decode().split("data: ", 1)[1])
                        terminal = event["type"]
                        item = event.get("item", {})
                        if item.get("status") == "action_required":
                            dispatched.append(item)
                            assert ctx._complete_tool(item["call_id"], '{"ok": true}')
                assert terminal == "response.completed"
                [item] = dispatched
                requester = ToolRequester.model_validate(item[REQUESTER_FIELD])
                assert requester.origin.user_id == user
                assert requester.execution_id == ctx.requester.execution_id
                execution_ids.add(requester.execution_id)
                state = executor._session_states[adapter._session_key]
                callbacks.append(state.agent.tools[0].on_invoke_tool)
                history.append({"role": "assistant", "content": "done"})
            assert len(execution_ids) == 3
            assert callbacks[0] is callbacks[1] is callbacks[2]
            assert len(requests) == 6
            assert delayed_call is not None
            assert (await delayed_call)["code"] == "runner_turn_context_desync"
        finally:
            if delayed_call is not None:
                delayed_call.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await delayed_call
            await adapter.on_shutdown()
