"""E2E: stopping a claude-native session parked on a permission prompt must not
hold the next message.

A real ``claude`` CLI parks on a ``Bash`` permission prompt while its
``PermissionRequest`` hook (inside the session's tmux) keeps an approval-wait
marker fresh. The web Stop control runs ``tmux kill-session``, killing the CLI
and that hook mid-wait. The next composer message relaunches ``claude --resume``
and must be typed into the pane promptly, not buffered behind the dead hook's
marker. Only a real ``tmux kill-session`` orphans that marker, so no synthetic
hook POST can stand in for this journey.

The journey posts the exact ``message`` / ``stop_session`` event bodies the web
composer and Stop control send (the behavior under test is the runner's
buffering decision, not the SPA) and reads the relaunched pane over the
product's read-only terminal-attach WebSocket, rendered with ``pyte``.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import shutil
import time

import httpx
import pyte
import pytest
import websockets

from tests.e2e_ui.conftest import set_fallback_mock_llm

_CLAUDE_MOCK_MODEL = "claude-sonnet-4-20250514"
_GATED_COMMAND = "sudo -n true"
# Wider than any real pane: a read-only attach ignores the resize and a narrow
# screen would wrap lines.
_PANE_COLS, _PANE_ROWS = 220, 55
# claude-native auto-launch + first-run pre-accept + WS attach.
_BOOT_TIMEOUT_S = 150.0
# Comfortably under APPROVAL_WAIT_MARKER_TTL_S (420s): a healthy delivery types
# the message within a few seconds, so a message still missing here is held.
_DELIVERY_TIMEOUT_S = 75.0


def _post_event(
    base_url: str, session_id: str, body: dict, timeout: float = 30.0
) -> httpx.Response:
    return httpx.post(f"{base_url}/v1/sessions/{session_id}/events", json=body, timeout=timeout)


def _send_user_message(base_url: str, session_id: str, text: str) -> httpx.Response:
    """Send a composer message exactly as the web composer does."""
    return _post_event(
        base_url,
        session_id,
        {
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": text}]},
        },
    )


def _snapshot(base_url: str, session_id: str) -> dict:
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    resp.raise_for_status()
    return resp.json()


def _terminal_id(base_url: str, session_id: str) -> str | None:
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}/resources", timeout=10.0)
    resp.raise_for_status()
    for resource in resp.json().get("data", []):
        if resource.get("type") == "terminal":
            return str(resource["id"])
    return None


def _render_pane(base_url: str, session_id: str, terminal_id: str, settle_s: float = 3.0) -> str:
    ws_url = (
        base_url.replace("http://", "ws://", 1).replace("https://", "wss://", 1)
        + f"/v1/sessions/{session_id}/resources/terminals/{terminal_id}/attach?read_only=true"
    )
    screen = pyte.Screen(_PANE_COLS, _PANE_ROWS)
    stream = pyte.ByteStream(screen)

    async def _collect() -> None:
        async with websockets.connect(ws_url, max_size=None, open_timeout=20) as ws:
            await ws.send(json.dumps({"type": "resize", "cols": _PANE_COLS, "rows": _PANE_ROWS}))
            deadline = time.monotonic() + settle_s
            while (remaining := deadline - time.monotonic()) > 0:
                try:
                    frame = await asyncio.wait_for(ws.recv(), timeout=remaining)
                except (TimeoutError, asyncio.TimeoutError):
                    break
                if isinstance(frame, (bytes, bytearray)):
                    stream.feed(bytes(frame))

    asyncio.run(_collect())
    return "\n".join(screen.display)


def _wait_pane_contains(
    base_url: str, session_id: str, needle: str, timeout_s: float
) -> tuple[bool, str]:
    deadline = time.monotonic() + timeout_s
    screen = ""
    while time.monotonic() < deadline:
        terminal_id = _terminal_id(base_url, session_id)
        if terminal_id is not None:
            try:
                screen = _render_pane(base_url, session_id, terminal_id)
            except (httpx.HTTPError, websockets.WebSocketException, OSError):
                screen = ""
            if needle.lower() in screen.lower():
                return True, screen
        time.sleep(1.0)
    return False, screen


def _pending_elicitations(base_url: str, session_id: str) -> list[dict]:
    return _snapshot(base_url, session_id).get("pending_elicitations") or []


def _wait_until(predicate, *, timeout_s: float, interval_s: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return False


@pytest.mark.skipif(
    shutil.which("claude") is None or shutil.which("tmux") is None,
    reason="a real claude CLI inside tmux owns the approval-wait marker under test",
)
@pytest.mark.timeout(360)
def test_stop_during_permission_prompt_does_not_hold_next_message(
    native_claude_mock_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    base_url, session_id = native_claude_mock_session
    nonce = secrets.token_hex(4)
    followup_marker = f"followup-{nonce}"

    # Stray/background/resume requests must never drain or hang the mock.
    set_fallback_mock_llm(mock_llm_server_url, _CLAUDE_MOCK_MODEL, "ok")
    # First message -> a gated Bash tool call (routed by the message nonce).
    httpx.post(
        f"{mock_llm_server_url}/mock/configure",
        json={
            "match": nonce,
            "required_tools": ["Bash"],
            "responses": [
                {
                    "tool_calls": [
                        {
                            "name": "Bash",
                            "arguments": json.dumps(
                                {
                                    "command": _GATED_COMMAND,
                                    "description": "check passwordless sudo",
                                }
                            ),
                        }
                    ]
                }
            ],
        },
        timeout=5.0,
    ).raise_for_status()

    booted, _ = _wait_pane_contains(base_url, session_id, "❯", timeout_s=_BOOT_TIMEOUT_S)
    assert booted, "Claude CLI never reached a ready composer"

    # setup: park on the Bash permission prompt via the real hook subprocess.
    sent = _send_user_message(
        base_url, session_id, f"[{nonce}] run the shell command `{_GATED_COMMAND}`"
    )
    assert sent.status_code in (200, 202), sent.text
    parked = _wait_until(lambda: bool(_pending_elicitations(base_url, session_id)), timeout_s=90.0)
    assert parked, "the gated Bash call never parked a permission elicitation (hook did not fire)"

    # trigger: the web Stop control kills the pane and the parked hook mid-wait.
    stopped = _post_event(base_url, session_id, {"type": "stop_session", "data": {}})
    assert stopped.status_code in (200, 202), stopped.text

    # The orphaned elicitation drains and the session returns to idle; wait so the
    # only thing that could hold the next message is the stale on-disk marker.
    _wait_until(
        lambda: (
            _snapshot(base_url, session_id).get("status") == "idle"
            and not _pending_elicitations(base_url, session_id)
        ),
        timeout_s=60.0,
    )

    # trigger: the next composer message must reach the relaunched session.
    followup = _send_user_message(base_url, session_id, f"did it work? [{followup_marker}]")
    assert followup.status_code in (200, 202), followup.text

    delivered, screen = _wait_pane_contains(
        base_url, session_id, followup_marker, timeout_s=_DELIVERY_TIMEOUT_S
    )
    try:
        requests = httpx.get(f"{mock_llm_server_url}/mock/requests", timeout=10.0).json()
        reached_model = followup_marker in json.dumps(requests)
    except (httpx.HTTPError, ValueError):
        reached_model = False

    assert delivered or reached_model, (
        "the follow-up message was silently held after Stop killed the parked "
        f"permission hook: it never reached the relaunched Claude within "
        f"{_DELIVERY_TIMEOUT_S:.0f}s (TTL ~420s). "
        f"reached_model={reached_model}; last pane:\n{screen}"
    )
