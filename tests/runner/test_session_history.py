"""Unit tests for runner session-history helpers."""

from __future__ import annotations

import pytest

from omnigent.runner.session_history import _trailing_user_message_id
from omnigent.util.json_types import JsonObject


def _message(item_id: str, role: str) -> JsonObject:
    return {"id": item_id, "type": "message", "role": role, "content": []}


@pytest.mark.parametrize(
    ("items", "expected"),
    [
        pytest.param([_message("user_1", "user")], "user_1", id="user-message-last"),
        pytest.param(
            [_message("user_1", "user"), {"id": "reasoning_1", "type": "reasoning"}],
            "user_1",
            id="skips-non-input-items",
        ),
        pytest.param(
            [_message("user_1", "user"), _message("assistant_1", "assistant")],
            None,
            id="answered",
        ),
        pytest.param(
            [_message("user_1", "user"), {"id": "call_1", "type": "function_call"}],
            None,
            id="tool-call-last",
        ),
        pytest.param(
            [_message("user_1", "user"), {"id": "compaction_1", "type": "compaction"}],
            None,
            id="compacted-away",
        ),
        pytest.param([], None, id="empty"),
    ],
)
def test_trailing_user_message_id(items: list[JsonObject], expected: str | None) -> None:
    """Only a user message that converted history ends with is reported.

    :param items: Stored session items in order.
    :param expected: The id the replayed history ends with, if any.
    """
    assert _trailing_user_message_id(items) == expected
