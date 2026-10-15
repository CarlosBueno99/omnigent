"""A recorded ``page`` stops filming when the test body ends, before fixtures tear down.

Runs nested pytest sessions with recording on (real browser, no live server)."""

from __future__ import annotations

import json
import os
import platform
from pathlib import Path

import pytest

from tests.helpers.ui_recording import extract_frames, fraction_near

pytest_plugins = ["pytester"]

# ``pytester`` points HOME at a temp dir, where Playwright would look for its browsers.
_REAL_HOME = Path.home()

_PLAYWRIGHT_PLUGINS = (
    "-p",
    "pytest_playwright.pytest_playwright",
    "-p",
    "pytest_base_url.plugin",
)

_LATER_FIXTURE_JOURNEY = """
import json
import os
from pathlib import Path

import pytest

REPORT = Path(os.environ["RECORDING_REPORT"])
RAW = Path(os.environ["OMNIGENT_E2E_RECORD_DIR"])
STATE = {}


@pytest.fixture
def native_session():
    yield "session"
    # Torn down before ``page``/``context``, like a fixture deleting its session.
    REPORT.write_text(json.dumps({
        "page_closed": STATE["page"].is_closed(),
        "videos": sorted(path.name for path in RAW.glob("*.webm")),
    }))


def test_journey(page, native_session):
    STATE["page"] = page
    page.set_content("<h1 style='font-size:72px'>live</h1>")
    page.wait_for_timeout(1_000)"""

# The cleanup fixture depends on ``page`` and deletes its seeded data in ``finally``.
_PAGE_DEPENDENT_CLEANUP_JOURNEY = """
import json
import os
from pathlib import Path

import pytest

REPORT = Path(os.environ["RECORDING_REPORT"])
RAW_DIR = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
RAW = Path(RAW_DIR) if RAW_DIR else None
RESULTS = ["file_name e540789f", "fileXname e540789f"]


@pytest.fixture
def search_sessions(page):
    try:
        yield RESULTS
    finally:
        # Like ``httpx.delete`` on the seeded sessions: filmed if the page still records.
        REPORT.write_text(json.dumps({
            "page_closed": page.is_closed(),
            "videos": sorted(path.name for path in RAW.glob("*.webm")) if RAW else [],
        }))
        RESULTS.clear()
        if not page.is_closed():
            page.set_content("<body style='background:#dc2626'>No results found</body>")
            page.wait_for_timeout(1_000)


def test_journey(page, search_sessions):
    page.set_content("<body style='background:#1d4ed8'><ul>" +
                     "".join(f"<li>{row}</li>" for row in search_sessions) + "</ul></body>")
    page.wait_for_timeout(1_000)"""


_JOURNEY_PRELUDE = """
import json
import os
from pathlib import Path

import pytest

REPORT = Path(os.environ["RECORDING_REPORT"])
RAW = Path(os.environ["OMNIGENT_E2E_RECORD_DIR"])
STATE = {}
"""

# Torn down after the body, like a fixture deleting the session the pane shows: it
# reports whether the page still films and, if so, puts the teardown state on screen.
_TEARDOWN_PROBE = """

@pytest.fixture
def native_session():
    yield "session"
    page = STATE["page"]
    REPORT.write_text(json.dumps({
        "page_closed": page.is_closed(),
        "videos": sorted(path.name for path in RAW.glob("*.webm")),
    }))
    if not page.is_closed():
        page.set_content(
            "<body style='background:#dc2626'>Bridge closed: terminal session ended</body>"
        )
        page.wait_for_timeout(1_000)
"""

# The journey films a page it opened itself from the session browser and never closes.
_OWN_PAGE_JOURNEY = (
    _JOURNEY_PRELUDE
    + _TEARDOWN_PROBE
    + """

def test_journey(browser, native_session):
    page = OPEN_PAGE
    STATE["page"] = page
    page.set_content("<body style='background:#1d4ed8'><h1>live</h1></body>")
    page.wait_for_timeout(1_000)"""
)

# A fixture owns the recorded page and closes its context after ``yield``; the probe
# fixture is set up later, so it tears down first.
_FIXTURE_OWNED_PAGE_JOURNEY = (
    _JOURNEY_PRELUDE
    + """

@pytest.fixture
def touch_page(browser):
    context = browser.new_context(has_touch=True)
    try:
        page = context.new_page()
        STATE["page"] = page
        yield page
    finally:
        context.close()
"""
    + _TEARDOWN_PROBE
    + """

def test_journey(touch_page, native_session):
    touch_page.set_content("<body style='background:#1d4ed8'><h1>live</h1></body>")
    touch_page.wait_for_timeout(1_000)"""
)

# An async-API page left open on the test's event loop, which outlives the body; the
# probe closes the browser it launched once it has reported.
_ASYNC_OWN_PAGE_JOURNEY = (
    _JOURNEY_PRELUDE
    + """
import pytest_asyncio
from playwright.async_api import async_playwright


@pytest_asyncio.fixture
async def native_session():
    yield "session"
    page = STATE["page"]
    REPORT.write_text(json.dumps({
        "page_closed": page.is_closed(),
        "videos": sorted(path.name for path in RAW.glob("*.webm")),
    }))
    if not page.is_closed():
        await page.set_content(
            "<body style='background:#dc2626'>Bridge closed: terminal session ended</body>"
        )
        await page.wait_for_timeout(1_000)
    await STATE["browser"].close()
    await STATE["playwright"].stop()


@pytest.mark.asyncio
async def test_journey(native_session):
    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch()
    STATE.update(playwright=playwright, browser=browser)
    page = OPEN_PAGE
    STATE["page"] = page
    await page.set_content("<body style='background:#1d4ed8'><h1>live</h1></body>")
    await page.wait_for_timeout(1_000)"""
)

# ``_close_recorded_context`` with an async context whose loop is closed or running.
_UNAVAILABLE_LOOP_PROBE = """
import asyncio

import pytest

from tests.e2e_ui.conftest import _close_recorded_context


class FakeContext:
    pages = ["open"]
    closed = False

    async def close(self):
        self.closed = True


@pytest.mark.parametrize("state", ["closed", "running"])
def test_unavailable_loop(request, state):
    context = FakeContext()
    loop = asyncio.new_event_loop()
    try:
        if state == "closed":
            loop.close()
            _close_recorded_context(request.node, context, loop)
        else:

            async def probe():
                _close_recorded_context(request.node, context, asyncio.get_running_loop())

            loop.run_until_complete(probe())
    finally:
        loop.close()
    assert not context.closed"""


def _browsers_path() -> str:
    """Playwright's browser cache as resolved from the real home, not pytester's."""
    configured = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if configured:
        return configured
    if platform.system() == "Darwin":
        return str(_REAL_HOME / "Library" / "Caches" / "ms-playwright")
    cache_home = os.environ.get("XDG_CACHE_HOME") or str(_REAL_HOME / ".cache")
    return str(Path(cache_home) / "ms-playwright")


def _configure(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    record_dir: Path | None,
) -> Path:
    root = str(Path(__file__).resolve().parents[2])
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([root, os.environ.get("PYTHONPATH", "")]))
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", _browsers_path())
    monkeypatch.delenv("OMNIGENT_E2E_RECORD_DIR", raising=False)
    monkeypatch.delenv("OMNIGENT_COMPAT_SERVER_VERSION", raising=False)
    monkeypatch.delenv("OMNIGENT_COMPAT_SERVER_PYTHON", raising=False)
    if record_dir is not None:
        monkeypatch.setenv("OMNIGENT_E2E_RECORD_DIR", str(record_dir))
    report = tmp_path / "report.json"
    monkeypatch.setenv("RECORDING_REPORT", str(report))
    pytester.makeconftest("""
import pytest

pytest_plugins = ["tests.e2e_ui.conftest"]

@pytest.fixture(scope="session")
def built_spa():
    pytest.fail("recording regression must not build the SPA")

@pytest.fixture(scope="session")
def live_server():
    pytest.fail("recording regression must not start a server")
""")
    return report


@pytest.mark.parametrize("inherited_compat", [False, True])
def test_video_is_finalized_before_a_later_fixture_tears_down(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    inherited_compat: bool,
) -> None:
    if inherited_compat:
        monkeypatch.setenv("OMNIGENT_COMPAT_SERVER_VERSION", "0.16.0")
        monkeypatch.setenv("OMNIGENT_COMPAT_SERVER_PYTHON", "/unused/compat/python")
    raw = tmp_path / "raw"
    report = _configure(pytester, monkeypatch, tmp_path, record_dir=raw)
    pytester.makepyfile(_LATER_FIXTURE_JOURNEY)

    result = pytester.runpytest_subprocess("-q", *_PLAYWRIGHT_PLUGINS)

    result.assert_outcomes(passed=1)
    observed = json.loads(report.read_text())
    assert observed["page_closed"], "page was still filming when the session fixture tore down"
    assert observed["videos"], "no video had been written when the session fixture tore down"
    assert sorted(path.name for path in raw.glob("*.webm")) == observed["videos"]


def test_video_is_finalized_before_a_page_dependent_fixture_cleans_up(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    raw = tmp_path / "raw"
    report = _configure(pytester, monkeypatch, tmp_path, record_dir=raw)
    pytester.makepyfile(_PAGE_DEPENDENT_CLEANUP_JOURNEY)

    result = pytester.runpytest_subprocess("-q", *_PLAYWRIGHT_PLUGINS)

    result.assert_outcomes(passed=1)
    observed = json.loads(report.read_text())
    assert observed["page_closed"], "page was still filming when the fixture deleted its data"
    assert observed["videos"] == sorted(path.name for path in raw.glob("*.webm"))
    assert len(observed["videos"]) == 1
    frames = extract_frames(raw / observed["videos"][0], tmp_path / "frames")
    assert fraction_near(frames[-1], (0x1D, 0x4E, 0xD8)) > 0.5, (
        "the final video frame lost the demonstrated search results before cleanup"
    )


def test_video_option_is_finalized_before_a_page_dependent_fixture_cleans_up(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    report = _configure(pytester, monkeypatch, tmp_path, record_dir=None)
    pytester.makepyfile(_PAGE_DEPENDENT_CLEANUP_JOURNEY)
    output = tmp_path / "pw-output"

    result = pytester.runpytest_subprocess(
        "-q", *_PLAYWRIGHT_PLUGINS, "--video", "on", "--output", str(output)
    )

    result.assert_outcomes(passed=1)
    observed = json.loads(report.read_text())
    assert observed["page_closed"], "page was still filming when the fixture deleted its data"
    assert list(output.rglob("video.webm")), "pytest-playwright did not save its video"


@pytest.mark.parametrize("fails", [False, True], ids=["passing", "failing"])
def test_early_close_preserves_failure_artifact_policy(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fails: bool
) -> None:
    report = _configure(pytester, monkeypatch, tmp_path, record_dir=None)
    pytester.makepyfile(_PAGE_DEPENDENT_CLEANUP_JOURNEY + f"\n    assert not {fails!r}\n")
    output = tmp_path / "pw-output"

    result = pytester.runpytest_subprocess(
        "-q",
        *_PLAYWRIGHT_PLUGINS,
        "--video",
        "retain-on-failure",
        "--tracing",
        "retain-on-failure",
        "--screenshot",
        "only-on-failure",
        "--output",
        str(output),
    )

    result.assert_outcomes(passed=int(not fails), failed=int(fails))
    assert json.loads(report.read_text())["page_closed"]
    for pattern in ("video.webm", "trace.zip", "test-failed-*.png"):
        artifacts = list(output.rglob(pattern))
        assert bool(artifacts) is fails, (pattern, artifacts)
        assert all(path.stat().st_size > 0 for path in artifacts)


def test_marker_recording_stops_before_cleanup(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    report = _configure(pytester, monkeypatch, tmp_path, record_dir=None)
    pytester.makepyfile(
        _PAGE_DEPENDENT_CLEANUP_JOURNEY.replace(
            "def test_journey(",
            f"@pytest.mark.browser_context_args(record_video_dir={str(tmp_path / 'raw')!r})\n"
            "def test_journey(",
        )
    )
    result = pytester.runpytest_subprocess("-q", *_PLAYWRIGHT_PLUGINS)
    result.assert_outcomes(passed=1)
    assert json.loads(report.read_text())["page_closed"]
    assert list((tmp_path / "raw").glob("*.webm"))


@pytest.mark.parametrize(
    "open_page",
    ["browser.new_page()", "browser.new_context().new_page()"],
    ids=["new_page", "new_context"],
)
def test_own_page_video_is_finalized_before_a_later_fixture_tears_down(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, open_page: str
) -> None:
    raw = tmp_path / "raw"
    report = _configure(pytester, monkeypatch, tmp_path, record_dir=raw)
    pytester.makepyfile(_OWN_PAGE_JOURNEY.replace("OPEN_PAGE", open_page))

    result = pytester.runpytest_subprocess("-q", *_PLAYWRIGHT_PLUGINS)

    result.assert_outcomes(passed=1)
    observed = json.loads(report.read_text())
    assert observed["page_closed"], (
        "a page the test opened itself was still filming when the session fixture tore down"
    )
    videos = sorted(path.name for path in raw.glob("*.webm"))
    assert observed["videos"] == videos
    assert len(videos) == 1
    frames = extract_frames(raw / videos[0], tmp_path / "frames")
    assert fraction_near(frames[-1], (0x1D, 0x4E, 0xD8)) > 0.5, (
        "the final video frame shows the teardown state instead of the live page"
    )


def test_page_opted_out_of_recording_is_left_to_its_owner(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    raw = tmp_path / "raw"
    report = _configure(pytester, monkeypatch, tmp_path, record_dir=raw)
    pytester.makepyfile(
        _OWN_PAGE_JOURNEY.replace("OPEN_PAGE", "browser.new_page(record_video_dir=None)")
    )

    result = pytester.runpytest_subprocess("-q", *_PLAYWRIGHT_PLUGINS)

    result.assert_outcomes(passed=1)
    observed = json.loads(report.read_text())
    assert not observed["page_closed"], (
        "a page that opted out of recording was closed before its owner's teardown"
    )
    assert observed["videos"] == []
    assert not list(raw.glob("*.webm"))


def test_fixture_owned_page_video_is_finalized_before_a_later_fixture_tears_down(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    raw = tmp_path / "raw"
    report = _configure(pytester, monkeypatch, tmp_path, record_dir=raw)
    pytester.makepyfile(_FIXTURE_OWNED_PAGE_JOURNEY)

    result = pytester.runpytest_subprocess("-q", *_PLAYWRIGHT_PLUGINS)

    # The owning fixture's own ``context.close()`` after ``yield`` must stay harmless.
    result.assert_outcomes(passed=1, errors=0)
    observed = json.loads(report.read_text())
    assert observed["page_closed"], (
        "a fixture-owned page was still filming when the session fixture tore down"
    )
    videos = sorted(path.name for path in raw.glob("*.webm"))
    assert observed["videos"] == videos
    assert len(videos) == 1
    frames = extract_frames(raw / videos[0], tmp_path / "frames")
    assert fraction_near(frames[-1], (0x1D, 0x4E, 0xD8)) > 0.5, (
        "the final video frame shows the teardown state instead of the live page"
    )


@pytest.mark.parametrize(
    "open_page",
    ["await browser.new_page()", "await (await browser.new_context()).new_page()"],
    ids=["new_page", "new_context"],
)
def test_async_own_page_video_is_finalized_before_a_later_fixture_tears_down(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, open_page: str
) -> None:
    raw = tmp_path / "raw"
    report = _configure(pytester, monkeypatch, tmp_path, record_dir=raw)
    pytester.makepyfile(_ASYNC_OWN_PAGE_JOURNEY.replace("OPEN_PAGE", open_page))

    result = pytester.runpytest_subprocess("-q", *_PLAYWRIGHT_PLUGINS, "-p", "asyncio")

    result.assert_outcomes(passed=1)
    observed = json.loads(report.read_text())
    assert observed["page_closed"], (
        "an async-API page the test opened was still filming when the session fixture tore down"
    )
    videos = sorted(path.name for path in raw.glob("*.webm"))
    assert observed["videos"] == videos
    assert len(videos) == 1
    frames = extract_frames(raw / videos[0], tmp_path / "frames")
    assert fraction_near(frames[-1], (0x1D, 0x4E, 0xD8)) > 0.5, (
        "the final video frame shows the teardown state instead of the live page"
    )


@pytest.mark.parametrize("warnings_as_errors", [False, True], ids=["default", "werror"])
def test_unavailable_async_loop_only_warns(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    warnings_as_errors: bool,
) -> None:
    _configure(pytester, monkeypatch, tmp_path, record_dir=None)
    pytester.makepyfile(_UNAVAILABLE_LOOP_PROBE)
    args = ["-q", *_PLAYWRIGHT_PLUGINS]
    if warnings_as_errors:
        args += ["-W", "error::pytest.PytestWarning"]

    result = pytester.runpytest_subprocess(*args)

    result.assert_outcomes(passed=2, warnings=2)
    result.stdout.fnmatch_lines(
        ["*Could not finalize recording for*its event loop is no longer available*"]
    )


def test_route_cleanup_fixtures_tolerate_early_close(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    report = _configure(pytester, monkeypatch, tmp_path, record_dir=tmp_path / "raw")
    conftest = pytester.path / "conftest.py"
    conftest.write_text(
        conftest.read_text()
        + """
from tests.e2e_ui.chat.test_claude_model_picker import _finish_snapshot_routes
from tests.e2e_ui.chat.test_side_chat_entrypoints import side_chat_forks
from tests.e2e_ui.files.test_files_panel_header import _drop_routes as files_routes
from tests.e2e_ui.files.test_reveal_in_file_manager import _drop_routes as reveal_routes
from tests.e2e_ui.sessions.test_host_badge import _drop_routes as badge_routes
from tests.e2e_ui.sessions.test_host_switch_reattaches_terminal import _drop_routes as switch
from tests.e2e_ui.sessions.test_reconnect_local_host_from_app import _drop_routes as reconnect

@pytest.fixture
def seeded_session():
    return ("http://127.0.0.1", "recording-test")
"""
    )
    pytester.makepyfile(
        _PAGE_DEPENDENT_CLEANUP_JOURNEY.replace(
            "def test_journey(page, search_sessions):",
            "def test_journey(page, search_sessions, side_chat_forks, files_routes):",
        )
    )
    result = pytester.runpytest_subprocess("-q", *_PLAYWRIGHT_PLUGINS)
    result.assert_outcomes(passed=1)
    assert json.loads(report.read_text())["page_closed"]
