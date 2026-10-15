"""Deterministic sleep and clock for the shared HTTP 429 retry loop."""

from __future__ import annotations

import pytest

from omnigent.native import native_terminal


class FakeRetryClock:
    """Records every retry pause and advances a fake monotonic clock by it."""

    def __init__(self) -> None:
        self.now = 1_000.0
        self.sleeps: list[float] = []

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay


def fake_retry_clock(monkeypatch: pytest.MonkeyPatch) -> FakeRetryClock:
    """Replace the retry loop's sleep and clock; return the clock to inspect or advance."""
    clock = FakeRetryClock()
    monkeypatch.setattr(native_terminal, "_sleep", clock.sleep)
    monkeypatch.setattr(native_terminal, "_monotonic", lambda: clock.now)
    return clock
