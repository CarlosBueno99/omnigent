"""PTY e2e: ``omnigent run --resume`` replays a tool call whose arguments look
like Rich markup instead of crashing.

Journey (local-daemon mode, mock LLM):

1. ``omnigent run <agent>`` and ask the agent to run ``echo '[/dim]'``. The
   mock answers with a ``sys_os_shell`` call for that command, then a reply.
2. ``/quit``; the exit hint carries the conversation id.
3. ``omnigent run --resume <id>`` must show the replayed tool-call line with
   the brackets intact and reach the prompt.

Tool arguments and exception text are user data: both the formatter's tool-call
line and the resume-failure notice must render them literally.

Usage::

    python -m pytest tests/e2e/test_repl_resume_markup_args_e2e.py -v
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from omnigent.testing.process_reaper import reap_leaked_omnigent_processes
from tests.e2e.conftest import configure_mock_llm, reset_mock_llm

pexpect = pytest.importorskip("pexpect")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_AGENT_YAML = (
    _REPO_ROOT / "tests" / "_fixtures" / "agents" / "markup-args-shell" / "markup-args-shell.yaml"
)
_MODEL_KEY = "mock-markup-args-shell"
_SHELL_COMMAND = "echo '[/dim]'"
_ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_READY_PATTERNS = [r"state: sleeping", r"·\s*ready"]
_BOOT_TIMEOUT_S = 180.0
_RESUME_TIMEOUT_S = 120.0
# Inherited runner/credential variables would re-point the spawned CLI at this
# process's own Omnigent session instead of the isolated local daemon.
_STALE_ENV_VARS = (
    "DATABRICKS_TOKEN",
    "ANTHROPIC_API_KEY",
    "CODEX",
    "CLAUDE_CODE",
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_EXECPATH",
    "TMUX",
    "RUNNER_SERVER_URL",
    "OMNIGENT_RUNNER_WORKSPACE",
    "OMNIGENT_RUNNER_ID",
    "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN",
    "OMNIGENT_DATABASE_URI",
    "OMNIGENT_SESSION_ID",
)


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _omnigent_cli() -> str:
    venv_cli = Path(sys.executable).parent / "omnigent"
    if venv_cli.exists():
        return str(venv_cli)
    found = shutil.which("omnigent")
    if found is None:
        pytest.skip("omnigent console script not found")
    return found


@pytest.fixture
def repl_env(mock_llm_server_url: str, tmp_path: Path) -> Iterator[dict[str, str]]:
    """Isolated HOME/config/data for a local-daemon ``omnigent run`` pointed at the mock LLM."""
    home = tmp_path / "home"
    config_home = home / ".omnigent"
    config_home.mkdir(parents=True)
    (config_home / "config.yaml").write_text(
        f"auto_open_conversation: false\ntui:\n  theme: light\ndefault_agent: {_AGENT_YAML}\n",
        encoding="utf-8",
    )
    env = {k: v for k, v in os.environ.items() if k not in _STALE_ENV_VARS}
    env.update(
        {
            "HOME": str(home),
            "OMNIGENT_CONFIG_HOME": str(config_home),
            "OMNIGENT_DATA_DIR": str(config_home),
            "OMNIGENT_SKIP_ONBOARD": "1",
            "OMNIGENT_NO_UPDATE_CHECK": "1",
            "OPENAI_API_KEY": "mock-key",
            "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
            "TERM": "xterm-256color",
            "PROMPT_TOOLKIT_NO_CPR": "1",
            "LINES": "40",
            "COLUMNS": "160",
            "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        }
    )
    try:
        yield env
    finally:
        _, survivors = reap_leaked_omnigent_processes(config_home)
        assert not survivors, f"REPL test processes survived cleanup: {survivors}"


def _spawn(args: list[str], env: dict[str, str], transcript: io.StringIO) -> Any:
    child = pexpect.spawn(
        _omnigent_cli(),
        args,
        env=env,
        cwd=str(_REPO_ROOT),
        encoding="utf-8",
        codec_errors="replace",
        dimensions=(40, 160),
        timeout=_BOOT_TIMEOUT_S,
    )
    child.logfile_read = transcript
    return child


def _create_conversation_with_markup_tool_call(env: dict[str, str]) -> str:
    """Drive one turn whose tool call contains ``[/dim]``; return the conversation id."""
    transcript = io.StringIO()
    child = _spawn(["run", str(_AGENT_YAML)], env, transcript)
    try:
        child.expect(_READY_PATTERNS, timeout=_BOOT_TIMEOUT_S)
        time.sleep(1.0)
        child.send(f"run {_SHELL_COMMAND}\r")
        child.expect(r"state: running", timeout=60)
        child.expect(r"state: sleeping", timeout=120)
        time.sleep(1.0)
        child.send("/quit\r")
        child.expect(pexpect.EOF, timeout=60)
    finally:
        if child.isalive():
            child.terminate(force=True)
    plain = re.sub(r"\\\s*\n\s*", " ", _strip_ansi(transcript.getvalue()))
    match = re.search(r"--resume\s+([0-9a-f]{32}|conv_[0-9a-f]+)", plain)
    assert match, f"no resume hint with a conversation id in the exit output:\n{plain[-3000:]}"
    return match.group(1)


def test_resume_replays_tool_call_with_markup_like_args(
    repl_env: dict[str, str], mock_llm_server_url: str
) -> None:
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call-dim-1",
                        "name": "sys_os_shell",
                        "arguments": json.dumps({"command": _SHELL_COMMAND}),
                    }
                ]
            },
            {"text": "Done."},
        ],
        key=_MODEL_KEY,
    )
    conversation_id = _create_conversation_with_markup_tool_call(repl_env)

    transcript = io.StringIO()
    child = _spawn(["run", "--resume", conversation_id], repl_env, transcript)
    try:
        idx = child.expect([*_READY_PATTERNS, r"\[Y/n\]", pexpect.EOF], timeout=_RESUME_TIMEOUT_S)
        if idx == 2:
            # Decline the crash handler's bug-filing prompt; a bare Enter would accept it.
            child.send("n\r")
            with contextlib.suppress(Exception):
                child.expect(pexpect.EOF, timeout=30)
        plain = _strip_ansi(transcript.getvalue())
        assert idx in (0, 1), (
            f"`omnigent run --resume {conversation_id}` never reached the prompt. "
            f"Output tail:\n{plain[-3000:]}"
        )
        assert "MarkupError" not in plain, f"resume raised MarkupError:\n{plain[-3000:]}"
        assert "⏵ sys_os_shell" in plain and "[/dim]" in plain, (
            f"replayed history lacks the tool-call line with literal arguments:\n{plain[-3000:]}"
        )
        child.send("/quit\r")
        with contextlib.suppress(Exception):
            child.expect(pexpect.EOF, timeout=60)
    finally:
        if child.isalive():
            child.terminate(force=True)
