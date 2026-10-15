"""Record why a native Codex provider's bearer helper failed, for the turn error.

Codex reports a failed ``auth.command`` as ``provider auth command `sh` exited
with status exit status: 1`` and discards the helper's stdout, where the
Databricks CLI prints its ``UNAUTHENTICATED`` diagnostic. The native launch
runs bearer helpers through :func:`main` (built by
:func:`wrap_provider_auth_command`), which forwards a minted token and records
a redacted, bounded account of a failed run in the bridge directory for
:func:`diagnose_provider_auth_failure` to turn into the failure card.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shlex
import signal
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

#: Failure code the turn error carries; described in ``launch_failure.py`` and
#: the web ``FAILURE_CODE_DESCRIPTIONS`` mirror.
PROVIDER_AUTH_FAILED_CODE = "codex_provider_auth_failed"
_MODULE = "omnigent.harnesses.codex_native.provider_auth"

OUTCOME_EXITED = "exited"
OUTCOME_EMPTY_TOKEN = "empty_token"
OUTCOME_TIMED_OUT = "timed_out"

_CODEX_PROVIDER_AUTH_ERROR = "provider auth command"
_DATABRICKS_TOKEN_COMMAND = ("databricks", "auth", "token")
_SHELL_SEPARATORS = frozenset({"|", "||", "&&", ";", "&"})
# Each captured stream is kept to this many bytes in the record and the card.
_OUTPUT_LIMIT_BYTES = 2048
# The only stdout kept verbatim: what ``jq -e`` prints for a missing field.
_SAFE_STDOUT = frozenset({"", "null", "true", "false"})
# Diagnostic keys kept from a JSON error object on stdout; everything else may be a token.
_ERROR_JSON_KEYS = ("error_code", "error", "error_description", "message", "details", "code")
# An unlabeled opaque word with a digit (hex, base64) is treated as a credential.
_OPAQUE_WORD = re.compile(
    r"(?<![\w./:@%-])(?=[A-Za-z0-9_+=-]*\d)[A-Za-z0-9_+=-]{20,}(?![\w./:@%-])"
)
_CODEX_MESSAGE_LIMIT_BYTES = 512
# Finish (and record) before Codex's own timeout kills this process.
_TIMEOUT_MARGIN_MS = 1000
_EXIT_TIMED_OUT = 124
_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)
_LOGIN_REQUIRED_FRAGMENTS = (
    "unauthenticated",
    "databricks auth login",
    "refresh token is invalid",
    "invalid_grant",
    "not logged in",
    "login required",
    "please log in",
    "please login",
    "re-authenticate",
    "token has expired",
    "token expired",
    "expired token",
)


@dataclass(frozen=True)
class ProviderAuthHelper:
    """
    Launch-time identity of the bearer helper, for naming the failure.

    :param provider: Provider display name, e.g. ``"DatabricksOSS"``.
    :param databricks_profile: ``~/.databrickscfg`` profile the helper mints
        with when it runs the Databricks CLI, e.g. ``"oss"``.
    :param databricks_host: Workspace host the helper mints for when it runs
        the Databricks CLI by host, e.g. ``"https://example.cloud.databricks.com"``.
    """

    provider: str
    databricks_profile: str | None = None
    databricks_host: str | None = None

    @property
    def uses_databricks_cli(self) -> bool:
        """:returns: ``True`` when the helper mints through the Databricks CLI."""
        return self.databricks_profile is not None or self.databricks_host is not None

    def describe(self) -> str:
        """:returns: e.g. ``"provider 'DatabricksOSS' (Databricks profile 'oss')"``."""
        if self.databricks_profile is not None:
            return f"provider {self.provider!r} (Databricks profile {self.databricks_profile!r})"
        if self.databricks_host is not None:
            return f"provider {self.provider!r} (Databricks workspace {self.databricks_host})"
        return f"provider {self.provider!r}"

    def cli_selector_flag(self) -> str:
        """:returns: The CLI selector, e.g. ``"--profile oss"`` or ``"--host https://..."``."""
        if self.databricks_profile is not None:
            return f"--profile {shlex.quote(self.databricks_profile)}"
        return f"--host {shlex.quote(self.databricks_host or '')}"


@dataclass(frozen=True)
class ProviderAuthFailure:
    """
    One failed helper run, as recorded in the bridge directory.

    :param helper: The helper that failed.
    :param outcome: :data:`OUTCOME_EXITED`, :data:`OUTCOME_EMPTY_TOKEN` or
        :data:`OUTCOME_TIMED_OUT`.
    :param exit_status: The helper's exit status, or ``None`` when it timed out.
    :param stdout: Redacted, bounded stdout, e.g. ``"null"``.
    :param stderr: Redacted, bounded stderr.
    """

    helper: ProviderAuthHelper
    outcome: str
    exit_status: int | None
    stdout: str
    stderr: str

    def to_record(self) -> dict[str, object]:
        """:returns: The JSON-serializable record."""
        return {
            "provider": self.helper.provider,
            "databricks_profile": self.helper.databricks_profile,
            "databricks_host": self.helper.databricks_host,
            "outcome": self.outcome,
            "exit_status": self.exit_status,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }

    @classmethod
    def from_record(cls, record: object) -> ProviderAuthFailure | None:
        """:returns: The failure a record describes, or ``None`` when malformed."""
        if not isinstance(record, dict):
            return None
        provider = record.get("provider")
        outcome = record.get("outcome")
        if not isinstance(provider, str) or outcome not in {
            OUTCOME_EXITED,
            OUTCOME_EMPTY_TOKEN,
            OUTCOME_TIMED_OUT,
        }:
            return None
        exit_status = record.get("exit_status")
        return cls(
            helper=ProviderAuthHelper(
                provider=provider,
                databricks_profile=_optional_str(record.get("databricks_profile")),
                databricks_host=_optional_str(record.get("databricks_host")),
            ),
            outcome=outcome,
            exit_status=exit_status if isinstance(exit_status, int) else None,
            stdout=_optional_str(record.get("stdout")) or "",
            stderr=_optional_str(record.get("stderr")) or "",
        )

    def reported_error(self) -> tuple[str | None, str | None]:
        """
        Return the structured error the helper printed, if any.

        The Databricks CLI reports a failed mint as ``{"error_code":
        "UNAUTHENTICATED", "message": "..."}``; whichever stream carries such an
        object wins.

        :returns: ``(error_code, message)``; each ``None`` when absent.
        """
        for text in (self.stdout, self.stderr):
            parsed = _json_object(text)
            if parsed is None:
                continue
            code = _optional_str(parsed.get("error_code")) or _optional_str(parsed.get("error"))
            message = _optional_str(parsed.get("message"))
            if code or message:
                return code, message
        return None, None


@dataclass(frozen=True)
class ProviderAuthDiagnosis:
    """
    What the failure card says about a failed bearer helper.

    :param title: Headline, e.g. ``"Databricks sign-in required for Codex"``.
    :param cause: Which provider/profile failed and what the helper reported.
    :param remediation: The provider-appropriate next step.
    :param login_required: Whether the evidence shows the credential needs a
        new sign-in rather than some other repair.
    """

    title: str
    cause: str
    remediation: str
    login_required: bool

    def message(self, codex_message: str) -> str:
        """:returns: The full text for clients that render only code/message."""
        reported = _bounded(_sanitized(codex_message), _CODEX_MESSAGE_LIMIT_BYTES, keep="head")
        return f"{self.cause}\n\nTry this: {self.remediation}\n\nCodex reported: {reported}"


def is_provider_auth_error(message: str) -> bool:
    """
    Report whether a Codex turn error is its bearer helper failing.

    :param message: Codex's error text, e.g. ``"provider auth command `sh`
        exited with status exit status: 1"``.
    :returns: ``True`` for the ``provider auth command`` failures Codex raises
        when the helper exits non-zero, prints no token, or times out.
    """
    return _CODEX_PROVIDER_AUTH_ERROR in message.lower()


def is_static_key_command(auth_command: str) -> bool:
    """
    Report whether *auth_command* only prints a stored key.

    :param auth_command: The provider's auth command, e.g. ``"printf %s sk-x"``.
    :returns: ``True`` for the ``printf %s <key>`` form a static credential is
        wrapped in; it cannot fail, so it is not worth a recorder. A key that
        the shell still has to expand (``printf %s "$TOKEN"``) can.
    """
    try:
        words = shlex.split(auth_command)
    except ValueError:
        return False
    return (
        len(words) == 3
        and words[:2] == ["printf", "%s"]
        and not _has_shell_expansion(auth_command)
    )


def _has_shell_expansion(command: str) -> bool:
    """Report whether *command* has a ``$`` or backtick the shell would expand."""
    in_single = in_double = escaped = False
    for character in command:
        if escaped:
            escaped = False
        elif character == "\\" and not in_single:
            escaped = True
        elif character == "'" and not in_double:
            in_single = not in_single
        elif character == '"' and not in_single:
            in_double = not in_double
        elif character in "$`" and not in_single:
            return True
    return False


def databricks_cli_selector(auth_command: str) -> tuple[str | None, str | None]:
    """
    Return the profile or host a ``databricks auth token`` helper mints with.

    :param auth_command: The provider's auth command, e.g.
        ``"databricks auth token --profile oss --output json | jq -er .access_token"``.
    :returns: ``(profile, host)``; both ``None`` when the command does not run
        ``databricks auth token`` or selects neither.
    """
    try:
        words = shlex.split(auth_command)
    except ValueError:
        return None, None
    start = next(
        (
            index + len(_DATABRICKS_TOKEN_COMMAND)
            for index in range(len(words) - len(_DATABRICKS_TOKEN_COMMAND) + 1)
            if tuple(words[index : index + len(_DATABRICKS_TOKEN_COMMAND)])
            == _DATABRICKS_TOKEN_COMMAND
        ),
        None,
    )
    if start is None:
        return None, None
    profile = host = None
    arguments = words[start:]
    for position, word in enumerate(arguments):
        if word in _SHELL_SEPARATORS:
            break
        following = arguments[position + 1] if position + 1 < len(arguments) else None
        if word in {"--profile", "-p"}:
            profile = following
        elif word.startswith("--profile="):
            profile = word.partition("=")[2]
        elif word == "--host":
            host = following
        elif word.startswith("--host="):
            host = word.partition("=")[2]
    return profile or None, host or None


def wrap_provider_auth_command(
    auth_command: str, helper: ProviderAuthHelper, *, timeout_ms: int
) -> str:
    """
    Return *auth_command* run through this module's failure recorder.

    :param auth_command: The provider's bearer helper, run with ``sh -c``.
    :param helper: Who the helper mints for, named in the failure card.
    :param timeout_ms: Codex's ``timeout_ms`` for the helper; the recorder
        finishes slightly earlier so a timeout is recorded too.
    :returns: A shell command printing the token the helper prints.
    """
    argv = [
        sys.executable or "python3",
        # Isolated mode: the helper inherits the session workspace as cwd,
        # which must not shadow the installed omnigent package.
        "-I",
        "-m",
        _MODULE,
        "--provider",
        helper.provider,
        "--timeout-ms",
        str(timeout_ms),
    ]
    if helper.databricks_profile is not None:
        argv.extend(["--databricks-profile", helper.databricks_profile])
    if helper.databricks_host is not None:
        argv.extend(["--databricks-host", helper.databricks_host])
    argv.extend(["--", auth_command])
    return shlex.join(argv)


def diagnose_provider_auth_failure(
    codex_message: str, failure: ProviderAuthFailure | None
) -> ProviderAuthDiagnosis:
    """
    Explain a failed bearer helper for the turn's failure card.

    :param codex_message: Codex's own error text; the only evidence when no
        run was recorded (an unwrapped helper, or a record that was cleared).
    :param failure: The recorded failed run, or ``None``.
    :returns: Title, cause and the provider-appropriate next step.
    """
    if failure is None:
        return _diagnose_from_codex_message(codex_message)
    helper = failure.helper
    error_code, error_message = failure.reported_error()
    evidence = " ".join(
        part for part in (failure.stdout, failure.stderr, error_code or "", codex_message) if part
    ).lower()
    login_required = (error_code or "").upper() == "UNAUTHENTICATED" or any(
        fragment in evidence for fragment in _LOGIN_REQUIRED_FRAGMENTS
    )
    subject = f"The auth command for {helper.describe()}"
    if failure.outcome == OUTCOME_TIMED_OUT:
        cause = f"{subject} did not finish in time, so Codex had no token for the request."
    elif error_code or error_message:
        reporter = "the Databricks CLI" if helper.uses_databricks_cli else "the helper"
        reported = ": ".join(part for part in (error_code, error_message) if part)
        cause = f"{subject} failed; {reporter} reported {reported}"
    elif failure.outcome == OUTCOME_EMPTY_TOKEN:
        cause = f"{subject} printed no token"
        if failure.stderr:
            cause += f"; it reported: {failure.stderr}"
    else:
        cause = f"{subject} exited with status {failure.exit_status}"
        if failure.stdout.strip() == "null":
            cause += (
                " and printed `null` instead of a token, so the CLI's own error was lost "
                "in the command's output filter"
            )
        elif failure.stdout:
            cause += f" and printed: {failure.stdout}"
        if failure.stderr:
            cause += f"; it reported: {failure.stderr}"
    cause = cause.rstrip(".") + "."
    if helper.uses_databricks_cli:
        remediation = _databricks_remediation(helper, failure.outcome, login_required)
        title = (
            "Databricks sign-in required for Codex"
            if login_required
            else "Codex could not get a Databricks token"
        )
    else:
        remediation = _generic_remediation(helper.provider, login_required)
        title = (
            "Provider sign-in required for Codex"
            if login_required
            else "Codex could not get a provider token"
        )
    return ProviderAuthDiagnosis(
        title=title, cause=cause, remediation=remediation, login_required=login_required
    )


def _diagnose_from_codex_message(codex_message: str) -> ProviderAuthDiagnosis:
    reported = _bounded(_sanitized(codex_message), _CODEX_MESSAGE_LIMIT_BYTES, keep="head")
    login_required = any(fragment in reported.lower() for fragment in _LOGIN_REQUIRED_FRAGMENTS)
    return ProviderAuthDiagnosis(
        title="Codex could not get a provider token",
        cause=f"The configured provider's auth command failed on the host: {reported}",
        remediation=_generic_remediation(None, login_required),
        login_required=login_required,
    )


def _databricks_remediation(helper: ProviderAuthHelper, outcome: str, login_required: bool) -> str:
    selector = helper.cli_selector_flag()
    login = f"`databricks auth login {selector}`"
    if login_required:
        return (
            f"Sign in to Databricks again on the host that runs this agent: {login}. "
            "Then send your message again."
        )
    probe = f"`databricks auth token {selector} --output json`"
    if outcome == OUTCOME_TIMED_OUT:
        return (
            f"On the host that runs this agent, check that {probe} completes; a Databricks "
            f"CLI waiting for a browser sign-in never does, so run {login} there. "
            "Then send your message again."
        )
    return (
        f"On the host that runs this agent, run {probe} to see the full error. "
        f"If it asks you to sign in, run {login}. Then send your message again."
    )


def _generic_remediation(provider: str | None, login_required: bool) -> str:
    from omnigent.cli_invocation import cli_invocation

    target = (
        f"the auth command for provider {provider!r}"
        if provider
        else "the provider's auth command"
    )
    action = (
        "sign in to the provider again there"
        if login_required
        else "run it in a shell there to see its full error"
    )
    return (
        f"On the host that runs this agent, check {target}: {action}, or reconfigure the "
        f"provider with `{cli_invocation()} setup`. Then send your message again."
    )


def _json_object(text: str) -> dict[str, object] | None:
    candidates = [text.strip()]
    embedded = _JSON_OBJECT.search(text)
    if embedded is not None:
        candidates.append(embedded.group(0))
    for candidate in candidates:
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _sanitized(text: str) -> str:
    from omnigent.harnesses.diagnostics import sanitize_diagnostic_text

    return sanitize_diagnostic_text(text).strip()


def _bounded(text: str, limit: int, *, keep: str) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    omitted = len(encoded) - limit
    if keep == "head":
        return encoded[:limit].decode("utf-8", errors="ignore") + f" … [{omitted} more bytes]"
    return f"[{omitted} earlier bytes] … " + encoded[-limit:].decode("utf-8", errors="ignore")


def _captured_stdout(stream: bytes) -> str:
    """Keep stdout only when it cannot carry a token: a ``null``-style value or a JSON error."""
    text = stream.decode("utf-8", errors="replace").strip()
    if text in _SAFE_STDOUT:
        return text
    parsed = _json_object(text)
    if parsed is not None:
        kept = {
            key: parsed[key] for key in _ERROR_JSON_KEYS if isinstance(parsed.get(key), (str, int))
        }
        if kept:
            return _bounded(
                _redacted(json.dumps(kept, sort_keys=True)), _OUTPUT_LIMIT_BYTES, keep="head"
            )
    return f"[output omitted: {len(stream)} bytes]"


def _captured_stderr(stream: bytes) -> str:
    return _bounded(
        _redacted(stream.decode("utf-8", errors="replace")), _OUTPUT_LIMIT_BYTES, keep="tail"
    )


def _redacted(text: str) -> str:
    return _OPAQUE_WORD.sub("[REDACTED]", _sanitized(text))


def _bridge_dir_from_env(env: Mapping[str, str]) -> Path | None:
    """The bridge dir behind the ``CODEX_HOME`` Omnigent gives its app-server."""
    from omnigent.harnesses.codex_native.bridge import (
        CODEX_HOME_DIR_NAME,
        bridge_dir_for_codex_home,
    )

    codex_home = env.get("CODEX_HOME")
    if not codex_home:
        return None
    home = Path(codex_home)
    if home.name != CODEX_HOME_DIR_NAME or not home.parent.is_dir():
        return None
    return bridge_dir_for_codex_home(home)


def _end_process_group(proc: subprocess.Popen[bytes]) -> None:
    with contextlib.suppress(OSError):
        os.killpg(proc.pid, signal.SIGKILL)


def main(argv: list[str] | None = None) -> int:
    """
    Run the wrapped helper, forwarding its token and recording a failure.

    :param argv: Arguments after the module name; ``None`` reads ``sys.argv``.
    :returns: The helper's exit status (``124`` when it timed out).
    """
    parser = argparse.ArgumentParser(prog="provider_auth", add_help=False)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--timeout-ms", type=int, required=True)
    parser.add_argument("--databricks-profile")
    parser.add_argument("--databricks-host")
    parser.add_argument("command")
    args = parser.parse_args(argv)
    helper = ProviderAuthHelper(
        provider=args.provider,
        databricks_profile=args.databricks_profile or None,
        databricks_host=args.databricks_host or None,
    )
    from omnigent.harnesses.codex_native.bridge import (
        clear_provider_auth_failure,
        record_provider_auth_failure,
    )

    bridge_dir = _bridge_dir_from_env(os.environ)
    timeout_s = max(args.timeout_ms - _TIMEOUT_MARGIN_MS, _TIMEOUT_MARGIN_MS) / 1000
    proc = subprocess.Popen(
        ["sh", "-c", args.command],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _end_process_group(proc)
        # A descendant that left the process group can keep the pipes open.
        try:
            stdout, stderr = proc.communicate(timeout=_TIMEOUT_MARGIN_MS / 2000)
        except subprocess.TimeoutExpired:
            stdout, stderr = b"", b""
        if bridge_dir is not None:
            record_provider_auth_failure(
                bridge_dir,
                ProviderAuthFailure(
                    helper=helper,
                    outcome=OUTCOME_TIMED_OUT,
                    exit_status=None,
                    stdout=_captured_stdout(stdout),
                    stderr=_captured_stderr(stderr),
                ),
            )
        sys.stderr.buffer.write(stderr)
        return _EXIT_TIMED_OUT
    status = proc.returncode
    if status == 0 and stdout.strip():
        if bridge_dir is not None:
            clear_provider_auth_failure(bridge_dir)
    elif bridge_dir is not None:
        record_provider_auth_failure(
            bridge_dir,
            ProviderAuthFailure(
                helper=helper,
                outcome=OUTCOME_EXITED if status != 0 else OUTCOME_EMPTY_TOKEN,
                exit_status=status,
                stdout=_captured_stdout(stdout),
                stderr=_captured_stderr(stderr),
            ),
        )
    sys.stdout.buffer.write(stdout)
    sys.stdout.flush()
    sys.stderr.buffer.write(stderr)
    return status


if __name__ == "__main__":
    sys.exit(main())
