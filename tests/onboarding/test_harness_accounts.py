"""Account probes expose identity metadata without credential material."""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest

from omnigent.onboarding import harness_accounts, harness_login


def _executable(path: Path, body: str) -> None:
    path.write_text(f"#!{sys.executable}\n{body}")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_codex_probe_reads_chatgpt_identity_from_app_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _executable(
        bin_dir / "codex",
        """import json, sys
for line in sys.stdin:
    message = json.loads(line)
    if message.get('method') == 'initialize':
        print(json.dumps({'id': message['id'], 'result': {}}), flush=True)
    elif message.get('method') == 'account/read':
        print(json.dumps({'id': message['id'], 'result': {'account': {
            'type': 'chatgpt', 'email': 'person@example.test', 'planType': 'pro'
        }}}), flush=True)
""",
    )
    monkeypatch.setenv("PATH", str(bin_dir))

    assert harness_accounts.harness_account_map() == {
        "codex": {
            "kind": "chatgpt",
            "email": "person@example.test",
            "plan": "pro",
            "expired": False,
        }
    }


@pytest.mark.asyncio
async def test_codex_login_and_logout_use_app_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _executable(
        bin_dir / "codex",
        """import json, sys
for line in sys.stdin:
    message = json.loads(line)
    method = message.get('method')
    if method == 'initialize':
        print(json.dumps({'id': message['id'], 'result': {}}), flush=True)
    elif method == 'account/login/start':
        print(json.dumps({'id': message['id'], 'result': {
            'type': 'chatgptDeviceCode', 'loginId': 'login-1',
            'userCode': 'ABCD-EFGH',
            'verificationUrl': 'https://example.test/device'
        }}), flush=True)
        print(json.dumps({'method': 'account/login/completed', 'params': {
            'loginId': 'login-1', 'success': True
        }}), flush=True)
    elif method == 'account/logout':
        print(json.dumps({'id': message['id'], 'result': {}}), flush=True)
""",
    )
    monkeypatch.setenv("PATH", str(bin_dir))

    session = harness_login.CodexLoginSession()
    try:
        login = await session.start()
        assert login == harness_login.CodexDeviceLogin(
            login_id="login-1",
            verification_url="https://example.test/device",
            user_code="ABCD-EFGH",
        )
        assert await session.wait_completed(login.login_id) == (True, None)
    finally:
        await session.close()

    await harness_login.codex_logout()


@pytest.mark.asyncio
async def test_claude_logout_uses_supported_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _executable(
        bin_dir / "claude",
        """import sys
raise SystemExit(0 if sys.argv[1:] == ['auth', 'logout'] else 2)
""",
    )
    monkeypatch.setenv("PATH", str(bin_dir))

    await harness_login.claude_logout()


def test_claude_and_pi_probes_use_supported_json_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _executable(
        bin_dir / "claude",
        """import json
print(json.dumps({'loggedIn': True, 'authMethod': 'claudeai',
 'email': 'claude@example.test', 'organization': 'Example',
 'subscriptionType': 'max'}))
""",
    )
    _executable(
        bin_dir / "pi",
        """import json, sys
provider = sys.argv[sys.argv.index('--provider') + 1]
if provider == 'anthropic':
    print(json.dumps({'status': 'ready', 'provider': provider, 'authType': 'oauth'}))
else:
    print(json.dumps({'status': 'not_ready', 'provider': provider,
                      'reason': 'credentials_not_configured'}))
    raise SystemExit(1)
""",
    )
    monkeypatch.setenv("PATH", str(bin_dir))

    accounts = harness_accounts.harness_account_map()
    assert accounts["claude"] == {
        "kind": "subscription",
        "email": "claude@example.test",
        "org": "Example",
        "plan": "max",
        "expired": False,
    }
    assert accounts["pi"] == {
        "kind": "providers",
        "providers": [{"id": "anthropic", "label": "Anthropic", "auth_type": "oauth"}],
        "expired": False,
    }


def test_opencode_probe_returns_only_non_secret_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _executable(bin_dir / "opencode", "raise SystemExit(0)\n")
    data_home = tmp_path / "data"
    auth_path = data_home / "opencode" / "auth.json"
    auth_path.parent.mkdir(parents=True)
    auth_path.write_text(
        json.dumps(
            {
                "anthropic": {"type": "api", "key": "must-not-leak"},
                "github": {
                    "type": "oauth",
                    "access": "must-not-leak",
                    "refresh": "must-not-leak",
                    "expires": 1,
                },
            }
        )
    )
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))

    account = harness_accounts.harness_account_map()["opencode"]
    assert account == {
        "kind": "providers",
        "providers": [
            {"id": "anthropic", "label": "anthropic", "auth_type": "api"},
            {"id": "github", "label": "github", "auth_type": "oauth"},
        ],
        "expired": True,
    }
    assert "must-not-leak" not in json.dumps(account)


def test_expired_accounts_flip_ready_spellings_to_needs_auth() -> None:
    configured = {
        "codex": True,
        "codex-native": True,
        "claude-native": "version-too-low",
        "qwen": True,
    }

    result = harness_accounts.apply_accounts_to_readiness(
        configured,
        {
            "codex": {"kind": "unknown", "expired": True},
            "claude": {"kind": "unknown", "expired": True},
        },
    )

    assert result == {
        "codex": "needs-auth",
        "codex-native": "needs-auth",
        "claude-native": "version-too-low",
        "qwen": True,
    }


def test_normalizer_drops_secret_and_malformed_fields() -> None:
    normalized = harness_accounts.normalize_harness_accounts(
        {
            "codex": {
                "kind": "chatgpt",
                "email": "person@example.test",
                "token": "secret",
            },
            "bad": {"kind": "future"},
        }
    )

    assert normalized == {"codex": {"kind": "chatgpt", "email": "person@example.test"}}
