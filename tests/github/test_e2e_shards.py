"""Duration sharding preserves pytest selection and its worker boundaries."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from scripts.update_e2e_durations import measured_durations
from tests.helpers.e2e_shards import assign_shards, read_durations

pytest_plugins = ["pytester"]
ROOT = Path(__file__).resolve().parents[2]


def test_expensive_tests_are_spread_and_unknown_tests_are_retained() -> None:
    durations = {"slow-a": 100, "slow-b": 90, "slow-c": 80, "slow-d": 70, "stale": 900}
    nodeids = ["slow-a", "slow-b", "slow-c", "slow-d", "new", "zero-a", "zero-b"]
    durations.update({"zero-a": 0, "zero-b": 0})
    result = assign_shards(nodeids, durations, 4, 10)
    assert set(result) == set(nodeids)
    assert len({result[name] for name in nodeids[:4]}) == 4
    assert result["new"] == result["slow-d"]
    assert result == assign_shards(list(reversed(nodeids)), durations, 4, 10)
    assert assign_shards(["a", "b", "c", "d"], {}, 2, 0) == {"a": 0, "b": 1, "c": 0, "d": 1}
    assert assign_shards([], {}, 2, 10) == {}
    with pytest.raises(ValueError, match="positive"):
        assign_shards(nodeids, durations, 0, 10)


def test_checked_in_profile_is_valid() -> None:
    durations, default = read_durations(ROOT / "tests/e2e/durations.json")
    assert default > 0
    assert durations and all(nodeid.startswith("tests/e2e/") for nodeid in durations)


@pytest.mark.parametrize("fault", [None, "failed", "missing_end", "missing_worker", "backwards"])
def test_profile_refresh_uses_complete_worker_elapsed_times(
    tmp_path: Path, fault: str | None
) -> None:
    for run, elapsed in enumerate((6, 10, 8)):
        directory = tmp_path / f"run{run}"
        directory.mkdir()
        outcome = "<failure/>" if fault == "failed" else ""
        (directory / "junit.xml").write_text(
            f"<testsuites><testsuite><testcase>{outcome}</testcase></testsuite></testsuites>"
        )
        if fault != "missing_worker":
            end = 99 if fault == "backwards" else 100 + elapsed
            records = "100\tSTART\ttests/e2e/test_example.py::test_case\n"
            if fault != "missing_end":
                records += f"{end}\tEND\ttests/e2e/test_example.py::test_case\t12345\n"
            (directory / "progress-gw0.log").write_text(records)
            (directory / "progress-main.log").write_text(records)
    if fault:
        with pytest.raises(ValueError):
            measured_durations(tmp_path)
    else:
        assert measured_durations(tmp_path) == {"tests/e2e/test_example.py::test_case": 8}
        output = tmp_path / "refreshed.json"
        subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/update_e2e_durations.py"),
                str(tmp_path),
                "--source-run",
                "https://example.test/run",
                "--output",
                str(output),
            ],
            check=True,
        )
        assert read_durations(output) == ({"tests/e2e/test_example.py::test_case": 8}, 10)


@pytest.mark.parametrize("invalid", [-1, float("inf"), float("nan"), True, "10", None])
def test_invalid_duration_is_rejected(tmp_path: Path, invalid) -> None:
    path = tmp_path / "durations.json"
    path.write_text(
        json.dumps({"version": 1, "default_duration": 10, "durations": {"x": invalid}})
    )
    with pytest.raises(ValueError, match="finite, nonnegative"):
        read_durations(path)


def _configure(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv(
        "PYTHONPATH", os.pathsep.join([str(ROOT), os.environ.get("PYTHONPATH", "")])
    )
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    pytester.makeini("[pytest]\nmarkers = nightly: scheduled test\n")
    pytester.makeconftest("""
import json
import os
from pathlib import Path

def pytest_collection_finish(session):
    worker = os.environ.get('PYTEST_XDIST_WORKER', 'main')
    shard = session.config.getoption('--shard-id')
    nodeids = [item.nodeid for item in session.items]
    Path(f'plan-{shard}-{worker}.json').write_text(json.dumps(nodeids))
""")
    pytester.makepyfile(
        test_cases="""
import pytest

@pytest.mark.parametrize('case', range(17))
def test_case(case):
    pass

class TestClass:
    def test_one(self):
        pass
    def test_two(self):
        pass

def test_failure():
    assert False, 'a real failure must keep failing its shard'

@pytest.mark.nightly
def test_nightly():
    pass

def test_excluded():
    assert False

def test_skip():
    pytest.skip('synthetic skipped case')
"""
    )
    path = pytester.path / "durations.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "default_duration": 10,
                "durations": {"test_cases.py::test_case[0]": 100, "deleted::test_stale": 1000},
            }
        )
    )
    return path


@pytest.mark.parametrize("count,markexpr", [(2, ""), (4, "not nightly"), (8, "not nightly")])
def test_real_parallel_shards_cover_filtered_suite_exactly_once(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, count: int, markexpr: str
) -> None:
    path = _configure(pytester, monkeypatch)
    expected = [f"test_cases.py::test_case[{case}]" for case in range(17)]
    expected += ["test_cases.py::TestClass::test_one", "test_cases.py::TestClass::test_two"]
    expected += ["test_cases.py::test_failure"]
    if not markexpr:
        expected += ["test_cases.py::test_nightly"]
    expected += ["test_cases.py::test_skip"]
    selected = []
    failures = 0
    for shard in range(count):
        result = pytester.runpytest_subprocess(
            "-q",
            "-p",
            "no:pytest-shard",
            "-p",
            "tests.helpers.e2e_shards",
            "-p",
            "xdist.plugin",
            "-n2",
            "--dist=loadscope",
            "--max-worker-restart=0",
            f"--shard-durations={path}",
            f"--num-shards={count}",
            f"--shard-id={shard}",
            "-m",
            markexpr,
            "-k",
            "not excluded",
        )
        plans = [
            json.loads((pytester.path / f"plan-{shard}-{worker}.json").read_text())
            for worker in ("gw0", "gw1")
        ]
        assert plans[0] == plans[1]
        plan = plans[0]
        assert plan == [nodeid for nodeid in expected if nodeid in plan]
        failed = int("test_cases.py::test_failure" in plan)
        skipped = int("test_cases.py::test_skip" in plan)
        result.assert_outcomes(passed=len(plan) - failed - skipped, failed=failed, skipped=skipped)
        assert result.ret == failed
        failures += failed
        selected.extend(plan)
    assert failures == 1
    assert len(selected) == len(set(selected))
    assert set(selected) == set(expected)


@pytest.mark.parametrize("fault", ["range", "zero_shards", "missing", "invalid"])
def test_bad_configuration_fails_instead_of_running_an_empty_shard(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    path = _configure(pytester, monkeypatch)
    extra = []
    if fault == "range":
        extra = ["--num-shards=2", "--shard-id=2"]
    elif fault == "zero_shards":
        extra = ["--num-shards=0"]
    elif fault == "missing":
        path.unlink()
    else:
        path.write_text("{")
    result = pytester.runpytest_subprocess(
        "-q",
        "-p",
        "no:pytest-shard",
        "-p",
        "tests.helpers.e2e_shards",
        f"--shard-durations={path}",
        *extra,
    )
    assert result.ret == pytest.ExitCode.USAGE_ERROR


@pytest.mark.parametrize("balanced", [False, True])
def test_shared_action_invokes_only_one_sharding_plugin(tmp_path: Path, balanced: bool) -> None:
    action = yaml.safe_load((ROOT / ".github/actions/e2e-run/action.yml").read_text())
    step = next(step for step in action["runs"]["steps"] if step["name"] == "Run e2e tests")
    suite = tmp_path / "tests/e2e"
    suite.mkdir(parents=True)
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    (suite / "test_cases.py").write_text(
        "import pytest\n@pytest.mark.parametrize('case', range(24))\ndef test_case(case): pass\n"
    )
    shim = tmp_path / "uv"
    shim.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$@" > "$ARGS_OUTPUT"\nshift\n'
        f'exec {shlex.quote(sys.executable)} -m "$@"\n'
    )
    shim.chmod(0o755)
    output = tmp_path / "args.txt"
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", step["run"]],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=dict(
            os.environ,
            PATH=f"{tmp_path}:{os.environ['PATH']}",
            PYTHONPATH=str(ROOT),
            ARGS_OUTPUT=str(output),
            PARALLELISM_INPUT="2",
            SHARD_ID="3",
            NUM_SHARDS="8",
            SHARD_DURATIONS=str(ROOT / "tests/e2e/durations.json") if balanced else "",
            NIGHTLY_FULL="false",
            DEPENDENCY_SMOKE="false",
            E2E_TMP_BASE=str(tmp_path / "e2e"),
        ),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "passed" in result.stdout
    args = output.read_text().splitlines()
    assert "--shard-id=3" in args and "--num-shards=8" in args
    assert args[args.index("--dist=loadscope") - 1] == "2"
    assert args[-2:] == ["-m", "not nightly"]
    assert ("no:pytest-shard" in args) == balanced
    assert ("tests.helpers.e2e_shards" in args) == balanced
    assert ("--shard-durations" in args) == balanced
