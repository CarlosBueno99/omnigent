"""Refresh checked-in E2E costs from downloaded successful-run timing artifacts."""

from __future__ import annotations

import argparse
import json
import statistics
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path


def measured_durations(root: Path) -> dict[str, float]:
    reports = sorted(root.rglob("junit.xml"))
    if not reports:
        raise ValueError(f"No JUnit reports found under {root}")
    samples: dict[str, list[float]] = defaultdict(list)
    for report in reports:
        cases = ET.parse(report).findall(".//testcase")
        if not cases or any(
            case.find("failure") is not None or case.find("error") is not None for case in cases
        ):
            raise ValueError(f"Expected a completed successful shard: {report}")
        completed = 0
        for progress in sorted(report.parent.rglob("progress-gw*.log")):
            starts: dict[str, float] = {}
            for line in progress.read_text().splitlines():
                timestamp, event, nodeid, *_ = line.split("\t")
                if event == "START":
                    starts[nodeid] = float(timestamp)
                elif event == "END":
                    elapsed = float(timestamp) - starts.pop(nodeid)
                    if elapsed < 0:
                        raise ValueError(f"Clock moved backwards in {progress}")
                    samples[nodeid].append(elapsed)
                    completed += 1
            if starts:
                raise ValueError(f"Incomplete tests in {progress}")
        if completed != len(cases):
            raise ValueError(f"Worker timings do not cover all {len(cases)} tests in {report}")
    return {
        nodeid: round(statistics.median(values), 3) for nodeid, values in sorted(samples.items())
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifacts", type=Path)
    parser.add_argument("--source-run", action="append", required=True)
    parser.add_argument("--output", type=Path, default=Path("tests/e2e/durations.json"))
    args = parser.parse_args()
    durations = measured_durations(args.artifacts)
    profile = {
        "version": 1,
        "default_duration": 10.0,
        "source": {
            "date": datetime.now(UTC).date().isoformat(),
            "measurement": (
                "Median worker START-to-END elapsed seconds, including setup, call and teardown, "
                "from successful hosted runs."
            ),
            "runs": args.source_run,
        },
        "durations": durations,
    }
    args.output.write_text(json.dumps(profile, indent=2) + "\n")
    print(f"Wrote {len(durations)} measured test costs to {args.output}")


if __name__ == "__main__":
    main()
