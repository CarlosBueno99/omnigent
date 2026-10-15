"""Attribution of a host-reported runner death on the server's debug-log row."""

from __future__ import annotations

import logging

import pytest

from omnigent.server.routes.host_tunnel import log_runner_exited

_REFUSAL = (
    "runner tunnel rejected by server (HTTP 401 persisted across 3 attempts); "
    "run `omnigent login https://ws.example.com` to re-authenticate"
)
_EXITED = "runner process exited with code 1 (log on host: ~/.omnigent/logs/runner/r.log)"


@pytest.mark.parametrize(
    ("error", "had_connected", "category", "phase"),
    [
        # Current hosts lead the report with the runner's refusal line.
        (f"{_REFUSAL}\n{_EXITED}", False, "config", "runner_launch"),
        # Older hosts ship the raw log tail; the refusal is still its last line.
        (
            f"{_EXITED}\n--- runner log tail ---\nasyncio.exceptions.CancelledError\n"
            f"error: {_REFUSAL}",
            None,
            "config",
            "runner_launch",
        ),
        # A fatal protocol close can refuse a runner that had connected: not a launch failure.
        (f"{_REFUSAL}\n{_EXITED}", True, "config", "unknown"),
        (
            f"{_EXITED}\n--- runner log tail ---\nModuleNotFoundError: No module named 'x'",
            False,
            "runner",
            "unknown",
        ),
    ],
    ids=["refusal-first", "refusal-in-tail", "refusal-after-connect", "unexplained-crash"],
)
def test_runner_exited_row_attributes_declared_refusals_to_config(
    caplog: pytest.LogCaptureFixture,
    error: str,
    had_connected: bool | None,
    category: str,
    phase: str,
) -> None:
    caplog.set_level(logging.WARNING, logger="omnigent.server.routes.host_tunnel")

    log_runner_exited(
        "host_a", "runner_1", error, session_id="conv_1", had_connected=had_connected
    )

    row = next(r for r in caplog.records if getattr(r, "event_name", None) == "runner_exited")
    assert row.attributes["error_category"] == category
    assert row.attributes["error_impact"] == "blocking"
    assert row.attributes["error_phase"] == phase
