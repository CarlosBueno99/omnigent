"""Claude Code's queue log replays to the prompts it may not have taken yet."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnigent.harnesses.claude_native.submit_verification import (
    capture_submit_boundary,
    replay_prompt_queue,
)
from omnigent.util.json_types import JsonObject

_SESSION = "11111111-2222-4333-8444-555555555555"


def _queue(operation: str, content: str | None = None, **fields: str) -> JsonObject:
    record: JsonObject = {"type": "queue-operation", "operation": operation, "sessionId": _SESSION}
    if content is not None:
        record["content"] = content
    return {**record, **fields}


@pytest.mark.parametrize(
    "before,after,expected,counted",
    [
        pytest.param(
            [],
            [_queue("enqueue", "/a"), _queue("enqueue", "b"), _queue("remove", "b")],
            ["/a"],
            True,
            id="absorbed",
        ),
        pytest.param(
            [],
            [_queue("enqueue", "/a"), _queue("dequeue")],
            [],
            True,
            id="dequeued",
        ),
        pytest.param(
            [],
            [_queue("enqueue", "/a"), _queue("enqueue", "b"), _queue("dequeue")],
            ["/a", "b"],
            True,
            id="dequeue-by-priority-names-neither",
        ),
        pytest.param(
            [],
            [
                _queue("enqueue", "/a"),
                _queue("enqueue", "b"),
                _queue("dequeue"),
                _queue("remove", "b"),
            ],
            [],
            True,
            id="removal-shows-what-was-dequeued",
        ),
        pytest.param(
            [],
            [
                _queue("enqueue", "/a"),
                _queue("enqueue", "b"),
                _queue("popAll", "/a"),
                _queue("popAll", "b"),
            ],
            [],
            True,
            id="pop-all",
        ),
        pytest.param(
            [],
            [_queue("enqueue", "/a"), _queue("enqueue", "b"), _queue("popOne", "/a")],
            ["b"],
            True,
            id="pop-one",
        ),
        pytest.param(
            [],
            [_queue("enqueue", "/a"), {**_queue("enqueue", "/b"), "sessionId": "other"}],
            ["/a"],
            True,
            id="other-session",
        ),
        pytest.param(
            [_queue("enqueue", "x")],
            [_queue("remove", "x"), _queue("enqueue", "/b"), _queue("dequeue")],
            ["/b"],
            False,
            id="removal-from-before-the-boundary",
        ),
        pytest.param(
            [_queue("enqueue", "x")],
            [_queue("dequeue"), _queue("enqueue", "/b"), _queue("dequeue")],
            ["/b"],
            False,
            id="dequeue-from-before-the-boundary",
        ),
        pytest.param(
            [],
            [_queue("enqueue"), _queue("enqueue", "/b"), _queue("remove")],
            ["/b"],
            True,
            id="non-text",
        ),
    ],
)
def test_replay_keeps_every_prompt_the_log_cannot_rule_out(
    tmp_path: Path,
    before: list[JsonObject],
    after: list[JsonObject],
    expected: list[str],
    counted: bool,
) -> None:
    transcript = tmp_path / "native.jsonl"
    transcript.write_text("".join(json.dumps(r) + "\n" for r in before), encoding="utf-8")
    boundary = capture_submit_boundary(transcript, _SESSION)
    assert boundary is not None
    with transcript.open("a", encoding="utf-8") as handle:
        handle.writelines(json.dumps(r) + "\n" for r in after)

    replay = replay_prompt_queue(boundary)

    assert [prompt.text for prompt in replay.prompts] == expected
    assert replay.counted is counted
