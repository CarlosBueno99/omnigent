"""A prompt hook can be processing a draft without having accepted it."""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from omnigent.harnesses.claude_native import bridge, delivery_diagnostics
from omnigent.util.json_types import JsonObject
from tests.harnesses.claude_native.test_claude_native_bridge import _composer_pane
from tests.harnesses.claude_native.test_claude_native_bridge import (
    _trust_tmp_bridge_parent as _trust_tmp_bridge_parent,
)
from tests.harnesses.claude_native.test_hook_block_submit_retries import _blocked_prompt_entry

_NATIVE_SESSION = "11111111-2222-4333-8444-555555555555"
_CONTENT = "synthetic delayed prompt"


def _install_submit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, outcome: str, hook_kind: str = "fresh"
) -> SimpleNamespace:
    transcript = tmp_path / "native.jsonl"
    transcript.write_text('{"type":"system","content":"prefix"}\n')
    bridge.record_hook_event(
        tmp_path,
        {
            "hook_event_name": "SessionStart",
            "session_id": _NATIVE_SESSION,
            "transcript_path": str(transcript),
        },
    )
    (tmp_path / "bridge.json").write_text('{"active_session_id":"web-original"}')
    hook: JsonObject = {
        "hook_event_name": "UserPromptSubmit",
        "session_id": _NATIVE_SESSION,
        "transcript_path": str(transcript),
        "prompt": _CONTENT,
    }
    if hook_kind == "different-prompt":
        hook["prompt"] = "another local input"
    elif hook_kind == "same-prefix":
        hook["prompt"] = _CONTENT + " suffix"
    elif hook_kind == "wrong-session":
        hook["session_id"] = "other-native"
    elif hook_kind == "wrong-transcript":
        hook["transcript_path"] = str(tmp_path / "other.jsonl")
    elif hook_kind == "child":
        hook["agent_id"] = "native-child"
    elif hook_kind == "sidechain":
        hook["isSidechain"] = True
    elif hook_kind == "wrong-event":
        hook["hook_event_name"] = "Stop"
    elif hook_kind == "wrapped":
        hook["prompt"] = f'<pasted_content id="abcd">\n{_CONTENT}\n</pasted_content>'
    hook_log = tmp_path / "hooks.jsonl"
    if hook_kind == "stale":
        bridge.record_hook_event(tmp_path, hook)
    elif hook_kind == "partial-boundary":
        with hook_log.open("a") as handle:
            handle.write('{"unfinished":')
    elif hook_kind == "initially-absent":
        hook_log.unlink()
    state = SimpleNamespace(
        elapsed=0.0,
        enters=0,
        pane=_composer_pane(),
        completed=False,
        commands=[],
        after_submit=[],
        hook=hook,
        transcript=transcript,
    )

    def sleep(seconds: float) -> None:
        state.elapsed += seconds

    def capture(*_args: object) -> str:
        if state.enters and state.elapsed >= 30 and not state.completed:
            state.completed = True
            if outcome == "allow":
                state.pane = _composer_pane()
            elif outcome in {"block", "block-empty"}:
                with transcript.open("a") as handle:
                    handle.write(
                        json.dumps(
                            _blocked_prompt_entry(
                                "Synthetic refusal.", _CONTENT, native_session_id=_NATIVE_SESSION
                            )
                        )
                        + "\n"
                    )
                if outcome == "block-empty":
                    state.pane = _composer_pane()
        return state.pane

    def run_tmux(_socket: str, *args: str) -> None:
        state.commands.append(args)
        if state.enters:
            state.after_submit.append(args)
        if args[0] == "paste-buffer":
            state.pane = _composer_pane(_CONTENT)
        if args[-1] != "Enter":
            return
        state.enters += 1
        if hook_kind in {"stale", "none"}:
            return
        if state.enters == 1:
            if hook_kind == "replaced-file":
                replacement = tmp_path / "replacement"
                replacement.write_bytes(hook_log.read_bytes())
                replacement.replace(hook_log)
            elif hook_kind == "rewritten-prefix":
                hook_log.write_bytes(
                    hook_log.read_bytes().replace(b"SessionStart", b"SessionOther")
                )
            elif hook_kind == "truncated":
                hook_log.write_bytes(b"")
        serialized = json.dumps({"payload": hook})
        if hook_kind == "malformed":
            serialized = "{broken}"
        with hook_log.open("a") as handle:
            handle.write(serialized + ("" if hook_kind == "partial-record" else "\n"))

    monkeypatch.setattr(
        bridge,
        "time",
        SimpleNamespace(monotonic=lambda: state.elapsed, sleep=sleep, time=lambda: state.elapsed),
    )
    monkeypatch.setattr(delivery_diagnostics, "time", bridge.time)
    monkeypatch.setattr(bridge, "_run_tmux", run_tmux)
    monkeypatch.setattr(bridge, "_capture_pane", capture)
    monkeypatch.setattr(bridge, "_wait_for_claude_prompt_ready", lambda *_args, **_kw: None)
    monkeypatch.setattr(bridge, "_restore_occupied_input", lambda *_args, **_kw: None)
    monkeypatch.setattr(
        bridge,
        "_wait_for_tmux_info",
        lambda *_args, **_kw: {"socket_path": "/unused/socket", "tmux_target": "main"},
    )
    monkeypatch.setattr(bridge, "_PASTE_SETTLE_S", 0)
    return state


def test_slow_allowed_hook_waits_for_the_composer_without_repeating_enter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _install_submit(tmp_path, monkeypatch, outcome="allow")
    bridge.inject_user_message(tmp_path, content=_CONTENT)
    assert 30 <= state.elapsed < 31
    assert state.enters == 1


@pytest.mark.parametrize("outcome", ["block", "block-empty"])
def test_slow_blocked_hook_stops_as_unknown_without_repeating_enter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    state = _install_submit(tmp_path, monkeypatch, outcome=outcome)
    with pytest.raises(bridge.ClaudeSubmitBlockedUnconfirmed):
        bridge.inject_user_message(tmp_path, content=_CONTENT)
    assert 30 <= state.elapsed < 31
    assert state.enters == 1
    assert state.after_submit == []


def test_matching_hook_with_a_retained_draft_never_proves_acceptance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _install_submit(tmp_path, monkeypatch, outcome="never")
    with pytest.raises(RuntimeError, match="could not be confirmed") as raised:
        bridge.inject_user_message(tmp_path, content=_CONTENT)
    assert not isinstance(raised.value, bridge.ClaudeMessageNotAccepted)
    assert 120 <= state.elapsed < 121
    assert state.enters == 1
    assert _CONTENT in state.pane


@pytest.mark.parametrize("hook_kind", ["wrapped", "initially-absent"])
def test_fresh_matching_hook_can_use_a_new_log_or_pasted_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hook_kind: str
) -> None:
    state = _install_submit(tmp_path, monkeypatch, outcome="allow", hook_kind=hook_kind)
    bridge.inject_user_message(tmp_path, content=_CONTENT)
    assert state.enters == 1
    assert 30 <= state.elapsed < 31


@pytest.mark.parametrize(
    "hook_kind",
    [
        "none",
        "stale",
        "different-prompt",
        "same-prefix",
        "wrong-session",
        "wrong-transcript",
        "child",
        "sidechain",
        "wrong-event",
        "partial-boundary",
        "partial-record",
        "malformed",
        "replaced-file",
        "rewritten-prefix",
        "truncated",
    ],
)
def test_unmatched_or_unreadable_hook_keeps_the_existing_submit_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hook_kind: str
) -> None:
    state = _install_submit(tmp_path, monkeypatch, outcome="never", hook_kind=hook_kind)
    with pytest.raises(bridge.ClaudeMessageNotAccepted):
        bridge.inject_user_message(tmp_path, content=_CONTENT)
    assert 20 <= state.elapsed < 21
    assert state.enters == 5


@pytest.mark.parametrize("rotation", ["native-session", "transcript", "web-session"])
@pytest.mark.parametrize("before_hook", [False, True])
def test_session_rotation_while_processing_stops_without_touching_the_pane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rotation: str, before_hook: bool
) -> None:
    state = _install_submit(
        tmp_path, monkeypatch, outcome="never", hook_kind="none" if before_hook else "fresh"
    )
    capture = bridge._capture_pane

    def rotate(*args: str) -> str:
        if state.enters and (before_hook or state.elapsed >= 5):
            if rotation == "web-session":
                bridge.write_active_session_id(tmp_path, "web-replacement")
            else:
                bridge.record_hook_event(
                    tmp_path,
                    {
                        "hook_event_name": "SessionStart",
                        "session_id": "native-replacement"
                        if rotation == "native-session"
                        else _NATIVE_SESSION,
                        "transcript_path": str(tmp_path / "replacement.jsonl")
                        if rotation == "transcript"
                        else str(state.transcript),
                    },
                )
            if before_hook:
                state.pane = _composer_pane()
        return capture(*args)

    monkeypatch.setattr(bridge, "_capture_pane", rotate)
    with pytest.raises(RuntimeError, match="session changed") as raised:
        bridge.inject_user_message(tmp_path, content=_CONTENT)
    assert not isinstance(raised.value, bridge.ClaudeMessageNotAccepted)
    assert 0 <= state.elapsed < 1 if before_hook else 5 <= state.elapsed < 6
    assert state.after_submit == []
    if not before_hook:
        assert _CONTENT in state.pane


def test_later_hooks_do_not_renew_the_processing_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _install_submit(tmp_path, monkeypatch, outcome="never")
    capture = bridge._capture_pane

    def append(*args: str) -> str:
        if state.enters:
            with (tmp_path / "hooks.jsonl").open("a") as handle:
                handle.write(json.dumps({"payload": state.hook}) + "\n")
        return capture(*args)

    monkeypatch.setattr(bridge, "_capture_pane", append)
    with pytest.raises(RuntimeError, match="could not be confirmed"):
        bridge.inject_user_message(tmp_path, content=_CONTENT)
    assert 120 <= state.elapsed < 121
    assert state.enters == 1


def test_a_late_hook_uses_the_original_submit_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _install_submit(tmp_path, monkeypatch, outcome="never", hook_kind="none")
    capture = bridge._capture_pane
    monkeypatch.setattr(bridge, "_SUBMIT_RETRY_INTERVAL_S", 60)

    def append_late(*args: str) -> str:
        if state.elapsed >= 19:
            with (tmp_path / "hooks.jsonl").open("a") as handle:
                handle.write(json.dumps({"payload": state.hook}) + "\n")
        return capture(*args)

    monkeypatch.setattr(bridge, "_capture_pane", append_late)
    with pytest.raises(RuntimeError, match="could not be confirmed"):
        bridge.inject_user_message(tmp_path, content=_CONTENT)
    assert 120 <= state.elapsed < 121
    assert state.enters == 1


def test_cancellation_while_processing_stops_the_worker_before_any_more_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _install_submit(tmp_path, monkeypatch, outcome="never")
    cancel = threading.Event()
    capture = bridge._capture_pane

    def cancel_after_processing(*args: str) -> str:
        if state.elapsed >= 5:
            cancel.set()
        return capture(*args)

    monkeypatch.setattr(bridge, "_capture_pane", cancel_after_processing)
    with bridge.cancellable_injection(cancel), pytest.raises(bridge.ClaudeInjectionCancelled):
        bridge.inject_user_message(tmp_path, content=_CONTENT)
    assert 5 <= state.elapsed < 6
    assert state.after_submit == []
    assert _CONTENT in state.pane


def test_empty_captures_during_hook_processing_never_prove_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _install_submit(tmp_path, monkeypatch, outcome="never")
    capture = bridge._capture_pane
    monkeypatch.setattr(
        bridge, "_capture_pane", lambda *args: "" if state.elapsed >= 1 else capture(*args)
    )
    with pytest.raises(RuntimeError, match="could not be confirmed"):
        bridge.inject_user_message(tmp_path, content=_CONTENT)
    assert 120 <= state.elapsed < 121
    assert state.enters == 1
    assert state.after_submit == []


@pytest.mark.asyncio
async def test_processing_expiry_remains_unknown_and_preserves_pending_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.inner import claude_native_executor
    from omnigent.inner.executor import ExecutorError
    from omnigent.runtime import pending_inputs

    state = _install_submit(tmp_path, monkeypatch, outcome="never")
    kills: list[Path] = []
    monkeypatch.setattr(
        claude_native_executor, "kill_session", lambda path, **_kw: kills.append(path)
    )
    pending_inputs.reset_for_tests()
    try:
        pending_id = pending_inputs.record(
            "web-original", [{"type": "input_text", "text": _CONTENT}], stable_id="a" * 32
        )
        executor = claude_native_executor.ClaudeNativeExecutor(tmp_path)
        events = [
            event
            async for event in executor.run_turn([{"role": "user", "content": _CONTENT}], [], "")
        ]
        assert len(events) == 1
        assert isinstance(events[0], ExecutorError)
        assert events[0].undelivered is False
        assert "could not be confirmed" in events[0].message
        assert kills == []
        assert state.enters == 1
        assert state.after_submit == []
        assert [item["pending_id"] for item in pending_inputs.snapshot_for("web-original")] == [
            pending_id
        ]
    finally:
        pending_inputs.reset_for_tests()


@pytest.mark.asyncio
async def test_cancelling_a_processing_turn_drains_its_worker_before_unlocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.inner import claude_native_executor

    state = _install_submit(tmp_path, monkeypatch, outcome="never")
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    capture = bridge._capture_pane
    kills: list[Path] = []

    def wait_in_processing(*args: str) -> str:
        if state.elapsed >= 5:
            started.set()
            assert release.wait(5)
        return capture(*args)

    def inject(*args: object, **kwargs: object) -> None:
        try:
            bridge.inject_user_message(*args, **kwargs)
        finally:
            finished.set()

    def reap(path: Path, **_kwargs: object) -> None:
        assert finished.is_set()
        kills.append(path)

    monkeypatch.setattr(bridge, "_capture_pane", wait_in_processing)
    monkeypatch.setattr(claude_native_executor, "inject_user_message", inject)
    monkeypatch.setattr(claude_native_executor, "kill_session", reap)
    executor = claude_native_executor.ClaudeNativeExecutor(tmp_path)

    async def deliver() -> None:
        async for _event in executor.run_turn([{"role": "user", "content": _CONTENT}], [], ""):
            raise AssertionError("cancelled turn completed")

    task = asyncio.create_task(deliver())
    try:
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert executor._inject_lock.locked()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert finished.is_set()
        assert not executor._inject_lock.locked()
        assert state.enters == 1
        assert state.after_submit == []
        assert kills == [tmp_path]
    finally:
        release.set()
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
