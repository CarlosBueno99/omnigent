"""The native wrappers hand an initial prompt to the TUI through argv."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import click
import pytest

from omnigent.harnesses.claude_native import main as claude_native

_MULTILINE = "line one\nline two\n\n  indented third"


@pytest.fixture(autouse=True)
def _tools_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend claude + tmux are installed so preflight passes."""
    monkeypatch.setattr(claude_native.shutil, "which", lambda command: f"/usr/bin/{command}")
    monkeypatch.setattr(claude_native, "resolve_native_claude_config", lambda spec: None)


def _capture_remote(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Record what ``run_claude_native`` hands the remote launch path."""
    captured: dict[str, Any] = {}

    def _fake_remote(base_url: str, spec_path: Path, **kwargs: Any) -> None:
        captured.update(kwargs)
        captured["base_url"] = base_url

    monkeypatch.setattr(claude_native, "_run_with_remote_server", _fake_remote)
    return captured


def _capture_local(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Record what ``run_claude_native`` hands the local launch path."""
    captured: dict[str, Any] = {}

    def _fake_local(spec_path: Path, **kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(claude_native, "_run_with_local_server", _fake_local)
    return captured


def test_prompt_rides_as_claudes_leading_positional_argument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The initial prompt is positional and precedes the pass-through flags."""
    captured = _capture_remote(monkeypatch)

    claude_native.run_claude_native(
        server="https://example.com/",
        session_id=None,
        extra_args=("--dangerously-skip-permissions",),
        prompt="review the last commit",
    )

    assert captured["claude_args"] == (
        "review the last commit",
        "--dangerously-skip-permissions",
    )


def test_prompt_precedes_variadic_value_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """A trailing variadic value flag must not be able to swallow the prompt.

    Claude Code's ``--mcp-config <configs...>`` consumes every following
    non-flag token as another config path; a prompt forwarded after it is
    rejected as a missing config file. Leading with the prompt makes that
    impossible regardless of which value flags the user passes through.
    """
    captured = _capture_remote(monkeypatch)

    claude_native.run_claude_native(
        server="https://example.com/",
        session_id=None,
        extra_args=("--model", "haiku", "--mcp-config", "mcp.json"),
        prompt="hello",
    )

    assert captured["claude_args"] == ("hello", "--model", "haiku", "--mcp-config", "mcp.json")


def test_multiline_prompt_stays_one_argv_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Newlines and indentation survive because the prompt is never re-split.

    Delivering the prompt through argv (not a tmux paste) is what keeps a
    multi-line prompt from being interpreted line-by-line by the TUI.
    """
    captured = _capture_remote(monkeypatch)

    claude_native.run_claude_native(
        server="https://example.com/",
        session_id=None,
        prompt=_MULTILINE,
    )

    assert captured["claude_args"] == (_MULTILINE,)


def test_prompt_is_added_after_resume_args_are_stripped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stray ``--resume`` is still dropped, and the prompt stays first."""
    captured = _capture_remote(monkeypatch)

    claude_native.run_claude_native(
        server="https://example.com/",
        session_id=None,
        extra_args=("--resume", "abc", "--verbose"),
        prompt="hello",
    )

    assert captured["claude_args"] == ("hello", "--verbose")


def test_dash_leading_prompt_stays_a_positional(monkeypatch: pytest.MonkeyPatch) -> None:
    """A prompt that starts with ``-`` is not handed over as an option.

    Claude Code rejects ``claude "- fix the lint"`` as an unknown option, and the
    runner would keep replaying a stored prompt it mistakes for a flag.
    """
    captured = _capture_remote(monkeypatch)

    claude_native.run_claude_native(
        server="https://example.com/",
        session_id=None,
        extra_args=("--verbose",),
        prompt="- fix the lint\n- run the tests",
    )

    assert captured["claude_args"] == (" - fix the lint\n- run the tests", "--verbose")


def test_local_launch_path_also_receives_the_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture_local(monkeypatch)

    claude_native.run_claude_native(server=None, session_id=None, prompt="hello")

    assert captured["claude_args"] == ("hello",)


@pytest.mark.parametrize(
    ("session_id", "resume_picker"),
    [("conv_abc123", False), (None, True)],
    ids=["resume-id", "resume-picker"],
)
def test_prompt_is_refused_with_a_remote_resume(
    monkeypatch: pytest.MonkeyPatch, session_id: str | None, resume_picker: bool
) -> None:
    """A resume on the remote path refuses a prompt instead of persisting it.

    The daemon-launched runner can only read the prompt from the session's
    stored launch args, and every later relaunch of the pane would replay it
    into the resumed conversation.
    """
    captured = _capture_remote(monkeypatch)

    with pytest.raises(click.UsageError, match="cannot be combined with --resume"):
        claude_native.run_claude_native(
            server="https://example.com/",
            session_id=session_id,
            resume_picker=resume_picker,
            prompt="hello",
        )

    assert captured == {}


def test_local_resume_still_forwards_the_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    """The local path hands the prompt to one launch through argv, so a resume keeps it."""
    captured = _capture_local(monkeypatch)

    claude_native.run_claude_native(server=None, session_id="conv_abc123", prompt="hello")

    assert captured["claude_args"] == ("hello",)


@pytest.mark.parametrize("prompt", [None, "", "   "])
def test_no_prompt_adds_no_argument(monkeypatch: pytest.MonkeyPatch, prompt: str | None) -> None:
    """An empty prompt must not become an empty positional argv entry."""
    captured = _capture_remote(monkeypatch)

    claude_native.run_claude_native(
        server="https://example.com/",
        session_id=None,
        extra_args=("--verbose",),
        prompt=prompt,
    )

    assert captured["claude_args"] == ("--verbose",)
