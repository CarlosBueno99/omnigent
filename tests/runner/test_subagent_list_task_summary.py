"""``sys_session_list`` entries must carry the child's generated ``task_summary``,
the label the Subagents panel leads with; otherwise the orchestrator cannot name
a worker the way the panel does. The row mirrors ``GET .../child_sessions``."""

from __future__ import annotations

from omnigent.runner.tool_dispatch import _child_rows_to_entries

_HANDLE = "deploy-profile-page"
_SUMMARY = "Deploy PR41 via web ops"


def _child_row() -> dict[str, object]:
    return {
        "id": "child-1",
        "title": f"worker:{_HANDLE}",
        "session_name": _HANDLE,
        "tool": "worker",
        "task_summary": _SUMMARY,
        "labels": {},
    }


def test_sys_session_list_entry_exposes_task_summary() -> None:
    [entry] = _child_rows_to_entries([_child_row()])

    # The handle already survives; the panel's label must too, or the agent
    # cannot name the worker the way its panel does.
    assert entry.get("title") == _HANDLE
    assert entry.get("task_summary") == _SUMMARY
