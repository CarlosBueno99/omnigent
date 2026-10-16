"""Tests for semantic native-terminal interactivity telemetry."""

from __future__ import annotations

import asyncio
import logging
from collections import deque

import pytest

from omnigent.runner.native.orchestration import _observe_terminal_interactive


class _FakeTerminal:
    """Minimal terminal instance that returns scripted pane captures."""

    diagnostic_id = "terminal-instance-test"

    def __init__(self, screens: list[str], *, running: bool = True) -> None:
        self._screens = deque(screens)
        self._last_screen = screens[-1]
        self.running = running

    async def read(self, *, join_wrapped: bool = False) -> dict[str, object]:
        del join_wrapped
        if self._screens:
            self._last_screen = self._screens.popleft()
        return {"screen": self._last_screen}


@pytest.mark.asyncio
async def test_observer_emits_one_content_free_terminal_interactive_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The first ready frame emits the shared semantic endpoint."""
    terminal = _FakeTerminal(["starting", "composer ready"])

    with caplog.at_level(logging.INFO, logger="omnigent.runner.app"):
        await _observe_terminal_interactive(
            session_id="conv_interactive",
            terminal_id="terminal_claude_main",
            harness="claude-native",
            readiness_signal="claude_composer",
            instance=terminal,  # type: ignore[arg-type]
            is_interactive=lambda pane: pane == "composer ready",
            timeout_s=1.0,
            poll_interval_s=0.0,
        )

    record = next(
        record for record in caplog.records if record.event_name == "terminal_interactive"
    )
    assert record.session_id == "conv_interactive"
    assert record.attributes == {
        "harness": "claude-native",
        "terminal_id": "terminal_claude_main",
        "terminal_instance_id": "terminal-instance-test",
        "readiness_signal": "claude_composer",
    }
    assert "composer ready" not in record.getMessage()


# A launcher wrapper still running its own startup step, with a token on screen.
_SCREEN_TOKEN = "dapi" + "Test1234567890abcd"
_STALLED_SCREEN = f"launcher: refreshing workspace login\nusing token {_SCREEN_TOKEN}\n"


@pytest.mark.asyncio
async def test_observer_records_the_redacted_pane_of_a_stalled_terminal(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pane that never becomes ready is described at the slow mark and at the deadline."""
    monkeypatch.setenv("OMNIGENT_HARNESS_STDERR_ENABLED", "1")
    terminal = _FakeTerminal([_STALLED_SCREEN])

    with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
        await _observe_terminal_interactive(
            session_id="conv_stalled",
            terminal_id="terminal_claude_main",
            harness="claude-native",
            readiness_signal="claude_composer",
            instance=terminal,  # type: ignore[arg-type]
            is_interactive=lambda _pane: False,
            timeout_s=0.05,
            slow_after_s=0.0,
            poll_interval_s=0.0,
        )

    by_event = {
        record.event_name: record
        for record in caplog.records
        if getattr(record, "event_name", None)
        in {"terminal_interactive_slow", "terminal_interactive_unobserved"}
    }
    assert [getattr(r, "event_name", None) for r in caplog.records].count(
        "terminal_interactive_slow"
    ) == 1
    assert by_event["terminal_interactive_unobserved"].attributes["reason"] == "TimeoutError"
    for record in by_event.values():
        tail = record.attributes["terminal_last_output"]
        assert "refreshing workspace login" in tail
        assert _SCREEN_TOKEN not in tail
        # The local runner log renders only the message, so the tail rides on it too.
        assert "refreshing workspace login" in record.getMessage()
        assert _SCREEN_TOKEN not in record.getMessage()


@pytest.mark.parametrize(
    ("capture", "screen", "expected"),
    [
        ("0", _STALLED_SCREEN, {"sign_in_prompt_detected": False}),
        (
            "1",
            "Sign in to continue:\n  https://signin.example.com/device\n  code: HQ7M-2KPD\n",
            {"sign_in_prompt_detected": True},
        ),
    ],
    ids=["capture-disabled", "sign-in-prompt"],
)
@pytest.mark.asyncio
async def test_observer_keeps_pane_text_out_unless_captured_and_not_a_sign_in(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    capture: str,
    screen: str,
    expected: dict[str, object],
) -> None:
    """Pane text needs the capture opt-in, and a sign-in prompt is only flagged."""
    monkeypatch.setenv("OMNIGENT_HARNESS_STDERR_ENABLED", capture)
    terminal = _FakeTerminal([screen])

    with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
        await _observe_terminal_interactive(
            session_id="conv_pending",
            terminal_id="terminal_codex_main",
            harness="codex-native",
            readiness_signal="codex_composer",
            instance=terminal,  # type: ignore[arg-type]
            is_interactive=lambda _pane: False,
            timeout_s=0.0,
            poll_interval_s=0.0,
        )

    record = next(
        record
        for record in caplog.records
        if record.event_name == "terminal_interactive_unobserved"
    )
    pane_keys = {"terminal_last_output", "sign_in_prompt_detected"}
    pane = {k: v for k, v in record.attributes.items() if k in pane_keys and v is not None}
    assert pane == expected


@pytest.mark.asyncio
async def test_observer_cancelled_after_the_slow_mark_keeps_its_record(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A runner lost before the deadline has already described the stalled pane.

    The default slow mark shrinks with a short deadline, so the record lands well
    before the cancellation that stands in for the runner going away.
    """
    monkeypatch.setenv("OMNIGENT_HARNESS_STDERR_ENABLED", "1")
    terminal = _FakeTerminal([_STALLED_SCREEN])

    def slow_records() -> list[logging.LogRecord]:
        return [
            record
            for record in caplog.records
            if getattr(record, "event_name", None) == "terminal_interactive_slow"
        ]

    with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
        task = asyncio.create_task(
            _observe_terminal_interactive(
                session_id="conv_lost",
                terminal_id="terminal_claude_main",
                harness="claude-native",
                readiness_signal="claude_composer",
                instance=terminal,  # type: ignore[arg-type]
                is_interactive=lambda _pane: False,
                timeout_s=1.5,
                poll_interval_s=0.01,
            )
        )
        while not slow_records() and not task.done():
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    (record,) = slow_records()
    assert "refreshing workspace login" in record.attributes["terminal_last_output"]
    assert "refreshing workspace login" in record.getMessage()
    assert not any(
        getattr(record, "event_name", None)
        in {"terminal_interactive", "terminal_interactive_unobserved"}
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_observer_records_unobserved_without_raising(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A stopped pane produces diagnostics without failing session creation."""
    terminal = _FakeTerminal(["starting"], running=False)

    with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
        await _observe_terminal_interactive(
            session_id="conv_stopped",
            terminal_id="terminal_codex_main",
            harness="codex-native",
            readiness_signal="codex_composer",
            instance=terminal,  # type: ignore[arg-type]
            is_interactive=lambda _pane: False,
            timeout_s=1.0,
            poll_interval_s=0.0,
        )

    record = next(
        record
        for record in caplog.records
        if record.event_name == "terminal_interactive_unobserved"
    )
    assert record.attributes["reason"] == "RuntimeError"
