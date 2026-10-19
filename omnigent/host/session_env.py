"""Per-session environment forwarded from a launching CLI to its runner.

The host daemon is shared by every session on the machine, so its own
environment cannot carry values that belong to one terminal, such as a
terminal multiplexer's pane socket or a short-lived capability token. A
launching CLI names those variables in :data:`SESSION_ENV_PASSTHROUGH_ENV_VAR`;
their values travel to the local daemon through an owner-only file that the
daemon removes as soon as it launches that session's runner. They never go
through the server, the session database, or the host's own environment.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import stat
import tempfile
import time
from collections.abc import AsyncIterator, Mapping
from pathlib import Path

import click

from omnigent._platform import IS_WINDOWS

_logger = logging.getLogger(__name__)

# Comma-separated variable names the launching shell forwards to this
# session's runner, e.g. ``ARCATERM_CAPABILITY,MACTERM_SOCKET``.
SESSION_ENV_PASSTHROUGH_ENV_VAR = "OMNIGENT_SESSION_ENV_PASSTHROUGH"

_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Runner wiring that a forwarded value must never replace.
_RESERVED_EXACT = frozenset({"RUNNER_SERVER_URL"})
_RESERVED_PREFIX = "OMNIGENT"
# A launch reaches the daemon within seconds; an older file outlived its CLI.
_HANDOFF_MAX_AGE_S = 300.0
_LAUNCH_LOCK_POLL_S = 0.05


def _handoff_dir() -> Path:
    from omnigent.process_logging import data_dir

    return data_dir() / "session-env"


def _handoff_path(session_id: str) -> Path:
    return _handoff_dir() / f"{hashlib.sha256(session_id.encode()).hexdigest()}.json"


def is_forwardable_name(name: str) -> bool:
    """Return whether *name* may be forwarded into a runner environment.

    :param name: Candidate variable name, e.g. ``"ARCATERM_CAPABILITY"``.
    :returns: ``True`` for a valid name outside Omnigent's runner wiring.
    """
    return (
        _NAME_PATTERN.fullmatch(name) is not None
        and not name.upper().startswith(_RESERVED_PREFIX)
        and name not in _RESERVED_EXACT
    )


def requested_session_env(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Collect the variables the launching shell asked to forward.

    Names listed in :data:`SESSION_ENV_PASSTHROUGH_ENV_VAR` but unset in the
    shell are skipped, so one setting works inside and outside the terminal
    that defines them.

    :param source: Environment to read. Defaults to ``os.environ``.
    :returns: Mapping of each requested, present name to its value.
    :raises click.ClickException: If a listed name is malformed or reserved.
    """
    env = os.environ if source is None else source
    names = [
        name.strip()
        for name in env.get(SESSION_ENV_PASSTHROUGH_ENV_VAR, "").split(",")
        if name.strip()
    ]
    rejected = sorted({name for name in names if not is_forwardable_name(name)})
    if rejected:
        raise click.ClickException(
            f"{SESSION_ENV_PASSTHROUGH_ENV_VAR} lists names that cannot be forwarded "
            f"to a runner: {', '.join(rejected)}. Use variable names that do not "
            f"start with {_RESERVED_PREFIX} and are not RUNNER_SERVER_URL."
        )
    return {name: env[name] for name in names if name in env}


@contextlib.asynccontextmanager
async def session_env_handoff(session_id: str, values: Mapping[str, str]) -> AsyncIterator[None]:
    """Hold this CLI's launch of *session_id* and offer *values* to its daemon.

    Concurrent launches of one session take turns on a per-session lock, so
    each launch's file is the one the daemon consumes for it; this needs
    ``fcntl`` and is skipped on Windows. The file exists only while the body
    runs; the daemon consumes it when it launches the runner, and this
    removes it if the launch never happened.

    :param session_id: Session whose runner should receive *values*.
    :param values: Forwarded variables from :func:`requested_session_env`;
        empty still clears any file a crashed launch left behind.
    """
    directory = _handoff_dir()
    path = _handoff_path(session_id)
    lock_path = path.with_suffix(".lock")
    lock_fd: int | None = None
    usable = True
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_fd = await _acquire_launch_lock(lock_path)
    except OSError:
        if values:
            raise
        _logger.debug("Launching without the session env lock", exc_info=True)
        usable = False
    if not usable:
        yield
        return
    try:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)
        if values:
            _write_handoff(directory, path, values)
        yield
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)
        _release_launch_lock(lock_path, lock_fd)


def _write_handoff(directory: Path, path: Path, values: Mapping[str, str]) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=".session-env-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(dict(values), handle)
        os.replace(temp_name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp_name)


async def _acquire_launch_lock(path: Path) -> int | None:
    try:
        import fcntl
    except ImportError:
        return None
    while True:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            await asyncio.sleep(_LAUNCH_LOCK_POLL_S)
            continue
        except BaseException:
            os.close(fd)
            raise
        # The previous holder unlinks the lock file on release; retry on a new one.
        with contextlib.suppress(FileNotFoundError):
            if os.path.samestat(os.fstat(fd), os.stat(path)):
                return fd
        os.close(fd)


def _release_launch_lock(path: Path, fd: int | None) -> None:
    if fd is None:
        return
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)
    os.close(fd)


def consume_session_env_handoff(session_id: str) -> dict[str, str]:
    """Read and remove the forwarded variables waiting for *session_id*.

    :param session_id: Session the daemon is launching a runner for.
    :returns: Forwarded variables, or ``{}`` when none are waiting or the file
        is not a fresh, owner-only regular file.
    """
    path = _handoff_path(session_id)
    _remove_expired_handoffs(path.parent)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return {}
    except OSError as exc:
        _logger.warning("Ignoring unreadable session env handoff %s: %s", path, exc)
        with contextlib.suppress(OSError):
            os.unlink(path)
        return {}
    try:
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            info = os.fstat(handle.fileno())
            problem = _handoff_problem(info)
            payload = None if problem else json.load(handle)
    except (OSError, ValueError) as exc:
        problem, payload = f"unreadable ({type(exc).__name__})", None
    finally:
        with contextlib.suppress(OSError):
            os.unlink(path)
    if problem:
        _logger.warning("Ignoring session env handoff %s: %s", path, problem)
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        name: value
        for name, value in payload.items()
        if isinstance(name, str) and isinstance(value, str) and is_forwardable_name(name)
    }


def _handoff_problem(info: os.stat_result) -> str | None:
    if not stat.S_ISREG(info.st_mode):
        return "not a regular file"
    if not IS_WINDOWS:
        if info.st_uid != os.getuid():
            return "owned by another user"
        if info.st_mode & 0o077:
            return "readable by other users"
    if time.time() - info.st_mtime > _HANDOFF_MAX_AGE_S:
        return "stale"
    return None


def _remove_expired_handoffs(directory: Path) -> None:
    cutoff = time.time() - _HANDOFF_MAX_AGE_S
    try:
        entries = list(os.scandir(directory))
    except FileNotFoundError:
        return
    except OSError as exc:
        _logger.warning("Cannot scan session env handoffs in %s: %s", directory, exc)
        return
    for entry in entries:
        if entry.name.endswith(".lock"):
            continue
        with contextlib.suppress(OSError):
            if entry.stat(follow_symlinks=False).st_mtime < cutoff:
                os.unlink(entry.path)
