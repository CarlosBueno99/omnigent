"""Claude Code's queue log replays to the prompts it has not taken yet."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnigent.harnesses.claude_native.submit_verification import (
    capture_submit_boundary,
    queued_prompts,
)
from omnigent.util.json_types import JsonObject

_SESSION = "11111111-2222-4333-8444-555555555555"


def _queue(operation: str, content: str | None = None, **fields: str) -> JsonObject:
    record: JsonObject = {"type": "queue-operation", "operation": operation, "sessionId": _SESSION}
    if content is not None:
        record["content"] = content
    return {**record, **fields}


@pytest.mark.parametrize(
    "before,after,expected",
    [
        pytest.param(
            [],
            [_queue("enqueue", "/a"), _queue("enqueue", "b"), _queue("remove", "b")],
            ["/a"],
            id="absorbed",
        ),
        pytest.param(
            [],
            [_queue("enqueue", "/a"), _queue("enqueue", "b"), _queue("dequeue")],
            ["b"],
            id="dequeue-takes-oldest",
        ),
        pytest.param(
            [],
            [_queue("enqueue", "/a"), _queue("enqueue", "b"), _queue("popAll", "/a")],
            ["b"],
            id="pop-all",
        ),
        pytest.param(
            [],
            [_queue("enqueue", "/a"), {**_queue("enqueue", "/b"), "sessionId": "other"}],
            ["/a"],
            id="other-session",
        ),
        pytest.param(
            [_queue("enqueue", "/a")],
            [_queue("remove", "/a"), _queue("enqueue", "/b")],
            ["/b"],
            id="before-boundary",
        ),
        pytest.param(
            [],
            [_queue("enqueue"), _queue("enqueue", "/b"), _queue("remove")],
            ["/b"],
            id="non-text",
        ),
    ],
)
def test_queued_prompts_replays_the_log_after_the_boundary(
    tmp_path: Path, before: list[JsonObject], after: list[JsonObject], expected: list[str]
) -> None:
    transcript = tmp_path / "native.jsonl"
    transcript.write_text("".join(json.dumps(r) + "\n" for r in before), encoding="utf-8")
    boundary = capture_submit_boundary(transcript, _SESSION)
    assert boundary is not None
    with transcript.open("a", encoding="utf-8") as handle:
        handle.writelines(json.dumps(r) + "\n" for r in after)

    assert queued_prompts(boundary) == expected
