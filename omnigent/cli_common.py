"""Shared constants and guards for the CLI, importable without ``omnigent.cli``.

The native coding-agent subcommands live in :mod:`omnigent.cli_native`, which
``omnigent.cli`` imports at module load to register them on the ``cli`` group.
Click evaluates command decorators at import time, so any module-level name a
decorator references (``flag_value=``, help-string interpolation) must resolve
before the command object is built. Keeping those names here — in a leaf module
that imports nothing from ``omnigent.cli`` — lets both ``cli`` and ``cli_native``
import them without an import cycle.
"""

from __future__ import annotations

import click

from omnigent._platform import IS_WINDOWS
from omnigent.cli_invocation import cli_invocation

# Click ``flag_value`` for bare ``--resume`` (no arg). Must exist before any
# command's decorator evaluates.
RESUME_PICKER_SENTINEL = "__resume_picker__"

# Env var that force-enables native Claude startup timing marks. Referenced in a
# command's ``--profile-startup`` help string, so it is decorator-time state.
CLAUDE_STARTUP_PROFILE_ENV_VAR = "OMNIGENT_CLAUDE_STARTUP_PROFILE"

# A remote resume-time prompt could only travel as persisted launch config that
# every later relaunch would replay, so the combination is refused up front.
CLAUDE_PROMPT_WITH_RESUME_ERROR = (
    "-p/--prompt gives a new session its first message, so it cannot be combined "
    "with --resume (or --session, or --continue on `omnigent run`). Resume without -p "
    "and type the message in Claude."
)


def claude_prompt_conflicts_with_resume(prompt: str | None, *, resuming: bool) -> bool:
    """Return whether a Claude ``-p`` prompt is combined with resuming a session.

    :param prompt: The ``-p`` text, or ``None``. Blank text counts as no prompt.
    :param resuming: ``True`` when the invocation targets an existing session.
    :returns: ``True`` when the combination must be refused with
        :data:`CLAUDE_PROMPT_WITH_RESUME_ERROR`.
    """
    return bool(resuming and prompt and prompt.strip())


def reject_native_on_windows(harness: str) -> None:
    """Fail a native (tmux/PTY) harness command with an actionable message.

    The ``omnigent claude`` / ``codex`` / ``cursor`` native wrappers drive a
    private tmux server and PTY, which don't exist on Windows. Point users at
    the SDK harnesses / web UI instead of letting them hit a tmux crash.

    :param harness: The native command name, e.g. ``"claude"``.
    :raises click.ClickException: Always, when running on Windows.
    """
    if IS_WINDOWS:
        raise click.ClickException(
            f"`{cli_invocation()} {harness}` (native tmux/PTY terminal) is not supported on "
            f"Windows. Use an SDK-based harness via `{cli_invocation()} run <agent.yaml>` "
            "or the web UI."
        )
