"""Usage tests for Codex forwarder."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.harnesses.codex_native import forwarder as fwd
from tests.harnesses.codex_native.forwarder._support import (
    _posted_cumulative_tokens,
    _RecordingClient,
    _Tokens,
    _usage_event,
)

# Frame sequences recorded from the official Codex app-server binaries against a
# mock Responses API. ``None`` marks where the child session is registered.
_PARENT_FRAMES = [
    _usage_event("thread_parent", (1000, 0, 100), (1000, 0, 100)),
    _usage_event("thread_parent", (2500, 1000, 110), (1500, 1000, 10)),
]
_PARENT_AFTER_FORK = _usage_event("thread_parent", (4500, 2000, 160), (2000, 1000, 50))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("events", "expected_child_posts"),
    [
        pytest.param(
            [
                None,
                *_PARENT_FRAMES,
                _PARENT_AFTER_FORK,
                _usage_event("thread_child", (2800, 1000, 140), (300, 0, 30)),
            ],
            [(300, 0, 30)],
            id="codex-0.149-fork-first-frame-adds-own-request",
        ),
        pytest.param(
            [
                None,
                *_PARENT_FRAMES,
                _usage_event("thread_child", (2500, 1000, 110), (1500, 1000, 10)),
                _PARENT_AFTER_FORK,
                _usage_event("thread_child", (2800, 1000, 140), (300, 0, 30)),
            ],
            [(0, 0, 0), (300, 0, 30)],
            id="codex-0.149-resumed-fork-repeats-parent-totals",
        ),
        pytest.param(
            [
                _usage_event("thread_parent", (1000, 0, 100), (1000, 0, 100)),
                _usage_event("thread_parent", (6000, 4000, 110), (5000, 4000, 10)),
                _usage_event("thread_child", (6000, 4000, 110), (5000, 4000, 10)),
                None,
                _usage_event("thread_child", (7000, 4000, 210), (1000, 0, 100)),
            ],
            [(1000, 0, 100)],
            id="thread-fork-seed-arrives-before-registration",
        ),
        pytest.param(
            [
                None,
                *_PARENT_FRAMES,
                _usage_event("thread_child", (300, 0, 30), (300, 0, 30)),
                _usage_event("thread_child", (700, 200, 60), (400, 200, 30)),
            ],
            [(300, 0, 30), (700, 200, 60)],
            id="codex-0.153-fresh-child",
        ),
    ],
)
async def test_child_usage_excludes_totals_inherited_from_forked_thread(
    tmp_path: Path, events: list[dict | None], expected_child_posts: list[_Tokens]
) -> None:
    """A forked Codex child posts only its own usage; the parent's stays unchanged.

    Forked threads start with the source thread's cumulative totals. Posting them
    as the child's usage double-counts the parent in the session's subtree cost.
    """
    client = _RecordingClient()
    state = fwd._CodexForwarderState(parent_session_id="conv_parent", mcp_startup_settled=True)
    parent_usage = fwd._SessionUsageCoalescer(client, "conv_parent")
    tracker = fwd._CodexElicitationTaskTracker()
    try:
        for event in events:
            if event is None:
                state.note_child_thread("thread_child", "conv_child")
                continue
            await fwd._handle_event(
                client,
                session_id="conv_parent",
                bridge_dir=tmp_path,
                event=event,
                usage_coalescer=parent_usage,
                elicitation_tracker=tracker,
                expected_thread_id="thread_parent",
                forwarder_state=state,
            )
    finally:
        await tracker.close()
        await state.close_child_streams()

    assert _posted_cumulative_tokens(client.posts, "conv_child") == expected_child_posts
    parent_totals = [
        tuple(event["params"]["tokenUsage"]["total"].values())
        for event in events
        if event is not None and event["params"]["threadId"] == "thread_parent"
    ]
    assert _posted_cumulative_tokens(client.posts, "conv_parent") == parent_totals


# ── sub-agent usage pricing: seed the child coalescer's model ──────────


@pytest.mark.asyncio
async def test_usage_coalescer_seeded_model_rides_along_so_child_usage_prices() -> None:
    """A coalescer seeded with a model attaches it to its token post.

    Codex sub-agent (child-thread) usage is recorded on a coalescer created on
    the child-event path, where ``forwarder_state`` (the usual model source) is
    intentionally ``None`` — so ``record()`` receives no model. Without the
    constructor seed the token post carries no ``model``, the server leaves the
    child's ``total_cost_usd`` unpriced (``None``), and the sub-agent's spend
    drops out of the parent's subtree cost — letting it run past the budget.
    The seeded model must ride along on every token post so the server can
    price the cumulative tokens.
    """
    client = _RecordingClient()
    coalescer = fwd._SessionUsageCoalescer(client, "conv_child", model="gpt-5.5")
    # Mirror the child path exactly: a usage frame with NO model in record().
    coalescer.record({"tokenUsage": {"total": {"inputTokens": 46003, "outputTokens": 4141}}})
    await coalescer.flush()

    assert len(client.posts) == 1  # one external_session_usage post
    url, body = client.posts[0]
    assert url == "/v1/sessions/conv_child/events"
    assert body["type"] == "external_session_usage"
    data = body["data"]
    # The seeded model rides along — this is what lets the server price the
    # tokens into the child's total_cost_usd (the whole point of the fix).
    assert data["model"] == "gpt-5.5"
    # The cumulative token counts the server prices from are present.
    assert data["cumulative_input_tokens"] == 46003
    assert data["cumulative_output_tokens"] == 4141


@pytest.mark.asyncio
async def test_usage_coalescer_unseeded_omits_model() -> None:
    """Without a seed (and no model via record), the post carries no model.

    This is the pre-fix behavior that left a sub-agent's cost unpriced; the
    test pins the contrast so a regression dropping the seed is caught (the
    post would silently go back to model-less and the budget gap would return).
    """
    client = _RecordingClient()
    coalescer = fwd._SessionUsageCoalescer(client, "conv_child")  # no model seed
    coalescer.record({"tokenUsage": {"total": {"inputTokens": 100, "outputTokens": 5}}})
    await coalescer.flush()

    assert len(client.posts) == 1
    _url, body = client.posts[0]
    assert "model" not in body["data"]
