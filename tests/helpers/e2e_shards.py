"""Partition the collected E2E suite using estimated elapsed test costs."""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from pathlib import Path

import pytest


def assign_shards(
    nodeids: Sequence[str],
    durations: dict[str, float],
    num_shards: int,
    default_duration: float,
) -> dict[str, int]:
    """Place expensive tests first, preserving every collected test."""
    if num_shards < 1:
        raise ValueError("num_shards must be positive")
    loads = [0.0] * num_shards
    counts = [0] * num_shards
    assignments = {}
    for nodeid in sorted(nodeids, key=lambda name: (-durations.get(name, default_duration), name)):
        shard = min(range(num_shards), key=lambda index: (loads[index], counts[index], index))
        assignments[nodeid] = shard
        loads[shard] += durations.get(nodeid, default_duration)
        counts[shard] += 1
    return assignments


def read_durations(path: Path) -> tuple[dict[str, float], float]:
    """Reject malformed profiles instead of silently changing the test plan."""
    profile = json.loads(path.read_text())
    if not isinstance(profile, dict) or profile.get("version") != 1:
        raise ValueError("expected a version 1 duration profile")
    durations = profile.get("durations")
    default_duration = profile.get("default_duration")
    if not isinstance(durations, dict) or not all(isinstance(key, str) for key in durations):
        raise ValueError("durations must map node IDs to seconds")
    for value in [default_duration, *durations.values()]:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise ValueError("durations must be finite, nonnegative seconds")
    if default_duration == 0:
        raise ValueError("default_duration must be positive")
    return durations, default_duration


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("duration shards")
    group.addoption("--shard-id", type=int, default=0)
    group.addoption("--num-shards", type=int, default=1)
    group.addoption("--shard-durations", type=Path, required=True)


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    shard = config.getoption("--shard-id")
    count = config.getoption("--num-shards")
    if count < 1 or not 0 <= shard < count:
        raise pytest.UsageError("require --num-shards >= 1 and 0 <= --shard-id < --num-shards")
    try:
        durations, default_duration = read_durations(config.getoption("--shard-durations"))
    except (OSError, ValueError) as error:
        raise pytest.UsageError(f"Invalid shard duration profile: {error}") from error
    assignments = assign_shards(
        [item.nodeid for item in items], durations, count, default_duration
    )
    selected = [item for item in items if assignments[item.nodeid] == shard]
    config.hook.pytest_deselected(
        items=[item for item in items if assignments[item.nodeid] != shard]
    )
    items[:] = selected
