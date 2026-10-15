"""The engine must gate agent scheduled-task mutations like ``sys_add_policy``.

``build_policy_engine`` injects ``__ask_on_add_policy`` into every engine and
``any_policies_apply`` never fast-paths ``sys_add_policy``, so an agent cannot
install a policy without the user approving. Scheduled tasks are durable work
created through the same tool relay, so ``sys_scheduled_task_create`` and
``sys_scheduled_task_update`` are held to the same bar on a spec with no
guardrails; reads stay ungated.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.entities import Conversation
from omnigent.policies.types import EvaluationContext
from omnigent.runtime.policies.builder import any_policies_apply, build_policy_engine
from omnigent.runtime.policies.engine import PolicyEngine
from omnigent.spec.parser import parse
from omnigent.spec.types import AgentSpec, Phase, PolicyAction
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)

_TASK_ID = "0123456789abcdef0123456789abcdef"
_CREATE_ARGS = {
    "name": "nightly triage",
    "prompt": "Triage new issues.",
    "rrule": "FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
    "agent_id": "ag_1",
}


def _no_guardrails_engine(
    tmp_path: Path,
    conversation_store: SqlAlchemyConversationStore,
) -> tuple[AgentSpec, Conversation, PolicyEngine]:
    (tmp_path / "config.yaml").write_text("spec_version: 1\nname: scheduler-probe\n")
    spec = parse(tmp_path)
    conv = conversation_store.create_conversation()
    engine = build_policy_engine(
        spec=spec,
        conversation_id=conv.id,
        conversation_store=conversation_store,
    )
    return spec, conv, engine


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "arguments", "expected"),
    [
        ("sys_add_policy", {"name": "shell_gate", "handler": "x.y"}, PolicyAction.ASK),
        ("sys_scheduled_task_list", {}, PolicyAction.ALLOW),
        ("sys_scheduled_task_create", _CREATE_ARGS, PolicyAction.ASK),
        (
            "sys_scheduled_task_update",
            {"scheduled_task_id": _TASK_ID, "state": "paused"},
            PolicyAction.ASK,
        ),
    ],
    ids=["add_policy_control", "list_stays_allowed", "create", "update"],
)
async def test_scheduled_task_mutation_asks_without_guardrails(
    tmp_path: Path,
    conversation_store: SqlAlchemyConversationStore,
    tool_name: str,
    arguments: dict[str, object],
    expected: PolicyAction,
) -> None:
    _spec, _conv, engine = _no_guardrails_engine(tmp_path, conversation_store)
    result = await engine.evaluate(
        EvaluationContext(
            phase=Phase.TOOL_CALL,
            content={"name": tool_name, "arguments": arguments},
            tool_name=tool_name,
        )
    )
    assert result.action == expected, result.reason


@pytest.mark.parametrize(
    "tool_name",
    ["sys_add_policy", "sys_scheduled_task_create", "sys_scheduled_task_update"],
)
def test_no_policy_fast_path_keeps_mutation_gates(
    tmp_path: Path,
    conversation_store: SqlAlchemyConversationStore,
    tool_name: str,
) -> None:
    spec, conv, _engine = _no_guardrails_engine(tmp_path, conversation_store)
    assert any_policies_apply(
        spec=spec,
        conversation_id=conv.id,
        default_policies=None,
        policy_store=None,
        phase=Phase.TOOL_CALL,
        tool_name=tool_name,
        conversation=conv,
    )
