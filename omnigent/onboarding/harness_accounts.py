"""Non-secret account metadata for locally installed harness CLIs."""

from __future__ import annotations

import json
import os
import selectors
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, TypeAlias, cast

from omnigent.harness_aliases import canonicalize_harness
from omnigent.harness_availability import HarnessAvailability

HarnessAccount: TypeAlias = dict[str, Any]
HarnessAccounts: TypeAlias = dict[str, HarnessAccount]

_ACCOUNT_KINDS = frozenset(
    {"chatgpt", "api_key", "subscription", "providers", "gateway", "unknown"}
)
_PROBE_TIMEOUT_S = 5.0


def normalize_harness_accounts(value: object) -> HarnessAccounts | None:
    """Return a wire-safe account map while dropping malformed entries."""
    if not isinstance(value, dict):
        return None
    normalized: HarnessAccounts = {}
    for harness, raw_account in value.items():
        if not isinstance(harness, str) or not isinstance(raw_account, dict):
            continue
        kind = raw_account.get("kind")
        if kind not in _ACCOUNT_KINDS:
            continue
        account: HarnessAccount = {"kind": kind}
        for key in ("email", "org", "plan"):
            field = raw_account.get(key)
            if isinstance(field, str) and field:
                account[key] = field
        if isinstance(raw_account.get("expired"), bool):
            account["expired"] = raw_account["expired"]
        raw_providers = raw_account.get("providers")
        if isinstance(raw_providers, list):
            providers: list[dict[str, str]] = []
            for raw_provider in raw_providers:
                if not isinstance(raw_provider, dict):
                    continue
                provider_id = raw_provider.get("id")
                label = raw_provider.get("label")
                auth_type = raw_provider.get("auth_type")
                if (
                    not isinstance(provider_id, str)
                    or not provider_id
                    or not isinstance(label, str)
                    or not label
                    or not isinstance(auth_type, str)
                    or not auth_type
                ):
                    continue
                providers.append({"id": provider_id, "label": label, "auth_type": auth_type})
            account["providers"] = providers
        normalized[harness] = account
    return normalized


class _StdioJsonRpc:
    """Small bounded JSON-RPC client for one Codex app-server probe."""

    def __init__(self, timeout_s: float = _PROBE_TIMEOUT_S) -> None:
        codex = shutil.which("codex")
        if codex is None:
            raise FileNotFoundError("codex is not installed")
        self._deadline = time.monotonic() + timeout_s
        self._next_id = 1
        self._proc = subprocess.Popen(
            [codex, "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )

    def close(self) -> None:
        """Terminate the short-lived probe process."""
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=1)

    def request(self, method: str, params: dict[str, object]) -> dict[str, Any]:
        """Send a request and return its result object."""
        request_id = self._next_id
        self._next_id += 1
        self._write({"id": request_id, "method": method, "params": params})
        while True:
            message = self._read()
            if message.get("id") != request_id:
                continue
            if message.get("error"):
                raise RuntimeError(f"codex app-server rejected {method}")
            result = message.get("result")
            return cast(dict[str, Any], result) if isinstance(result, dict) else {}

    def notify(self, method: str) -> None:
        """Send a notification."""
        self._write({"method": method})

    def _write(self, message: dict[str, object]) -> None:
        if self._proc.stdin is None:
            raise RuntimeError("codex app-server stdin is unavailable")
        self._proc.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        self._proc.stdin.flush()

    def _read(self) -> dict[str, Any]:
        if self._proc.stdout is None:
            raise RuntimeError("codex app-server stdout is unavailable")
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("codex account probe timed out")
        selector = selectors.DefaultSelector()
        try:
            selector.register(self._proc.stdout, selectors.EVENT_READ)
            if not selector.select(remaining):
                raise TimeoutError("codex account probe timed out")
            line = self._proc.stdout.readline()
        finally:
            selector.close()
        if not line:
            raise RuntimeError("codex app-server exited during account probe")
        message = json.loads(line)
        if not isinstance(message, dict):
            raise RuntimeError("codex app-server returned a non-object message")
        return cast(dict[str, Any], message)


def _codex_account() -> HarnessAccount | None:
    if shutil.which("codex") is None:
        return None
    rpc: _StdioJsonRpc | None = None
    try:
        rpc = _StdioJsonRpc()
        rpc.request(
            "initialize",
            {
                "clientInfo": {"name": "omnigent", "version": "0.1"},
                "capabilities": {"experimentalApi": True},
            },
        )
        rpc.notify("initialized")
        result = rpc.request("account/read", {"refreshToken": True})
        raw_account = result.get("account")
        if not isinstance(raw_account, dict):
            return {"kind": "unknown", "expired": True}
        account_type = raw_account.get("type")
        if account_type == "chatgpt":
            account: HarnessAccount = {"kind": "chatgpt", "expired": False}
            email = raw_account.get("email")
            plan = raw_account.get("planType")
            if isinstance(email, str) and email:
                account["email"] = email
            if isinstance(plan, str) and plan:
                account["plan"] = plan
            return account
        if account_type in {"apiKey", "api_key"}:
            return {"kind": "api_key", "expired": False}
        return {"kind": "unknown", "expired": False}
    except Exception:
        return {"kind": "unknown", "expired": True}
    finally:
        if rpc is not None:
            rpc.close()


def _run_json_command(argv: list[str]) -> tuple[int, dict[str, Any] | None]:
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 1, None
    try:
        payload = json.loads(completed.stdout)
    except (json.JSONDecodeError, TypeError):
        return completed.returncode, None
    return completed.returncode, cast(dict[str, Any], payload) if isinstance(
        payload, dict
    ) else None


def _claude_account() -> HarnessAccount | None:
    claude = shutil.which("claude")
    if claude is None:
        return None
    return_code, payload = _run_json_command([claude, "auth", "status", "--json"])
    if payload is None or return_code != 0 or payload.get("loggedIn") is not True:
        return {"kind": "unknown", "expired": True}
    nested = payload.get("account")
    source = nested if isinstance(nested, dict) else payload
    auth_method = payload.get("authMethod")
    account: HarnessAccount = {
        "kind": "api_key" if auth_method == "api_key" else "subscription",
        "expired": False,
    }
    for target, candidates in {
        "email": ("email",),
        "org": ("organization", "org"),
        "plan": ("subscriptionType", "plan"),
    }.items():
        value = next((source.get(key) for key in candidates if source.get(key)), None)
        if isinstance(value, str):
            account[target] = value
    return account


def _pi_account() -> HarnessAccount | None:
    pi = shutil.which("pi")
    if pi is None:
        return None
    providers: list[dict[str, str]] = []
    labels = {
        "anthropic": "Anthropic",
        "openai": "OpenAI",
        "openai-codex": "OpenAI Codex",
    }
    for provider_id, label in labels.items():
        return_code, payload = _run_json_command(
            [pi, "auth", "check", "--provider", provider_id, "--json"]
        )
        if return_code != 0 or payload is None or payload.get("status") != "ready":
            continue
        auth_type = payload.get("authType")
        if isinstance(auth_type, str) and auth_type:
            providers.append({"id": provider_id, "label": label, "auth_type": auth_type})
    return {"kind": "providers", "providers": providers, "expired": not providers}


def _opencode_account() -> HarnessAccount | None:
    if shutil.which("opencode") is None:
        return None
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    auth_path = data_home / "opencode" / "auth.json"
    try:
        payload = json.loads(auth_path.read_text())
    except (OSError, json.JSONDecodeError):
        return {"kind": "providers", "providers": [], "expired": True}
    if not isinstance(payload, dict):
        return {"kind": "providers", "providers": [], "expired": True}
    providers: list[dict[str, str]] = []
    expired = False
    now_ms = int(time.time() * 1000)
    for provider_id, raw_record in payload.items():
        if not isinstance(provider_id, str) or not isinstance(raw_record, dict):
            continue
        auth_type = raw_record.get("type")
        if not isinstance(auth_type, str) or not auth_type:
            continue
        providers.append({"id": provider_id, "label": provider_id, "auth_type": auth_type})
        expires = raw_record.get("expires")
        if auth_type == "oauth" and isinstance(expires, (int, float)) and expires <= now_ms:
            expired = True
    return {
        "kind": "providers",
        "providers": providers,
        "expired": expired or not providers,
    }


def harness_account_map() -> HarnessAccounts:
    """Probe installed harnesses and return non-secret account metadata."""
    accounts: HarnessAccounts = {}
    for harness, probe in (
        ("codex", _codex_account),
        ("claude", _claude_account),
        ("pi", _pi_account),
        ("opencode", _opencode_account),
    ):
        account = probe()
        if account is not None:
            accounts[harness] = account
    return accounts


def apply_accounts_to_readiness(
    configured: dict[str, HarnessAvailability], accounts: HarnessAccounts | None
) -> dict[str, HarnessAvailability]:
    """Mark installed harness spellings needs-auth when their account expired."""
    result = dict(configured)
    if not accounts:
        return result
    expired = {harness for harness, account in accounts.items() if account.get("expired") is True}
    if not expired:
        return result
    for spelling, availability in result.items():
        canonical = canonicalize_harness(spelling) or spelling
        family = (
            "claude"
            if canonical in {"claude", "claude-sdk", "claude-native"}
            else "codex"
            if canonical in {"codex", "codex-native"}
            else "opencode"
            if canonical in {"opencode", "opencode-native"}
            else "pi"
            if canonical in {"pi", "pi-native"}
            else None
        )
        if family in expired and availability not in {False, "version-too-low"}:
            result[spelling] = "needs-auth"
    return result
