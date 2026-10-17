r"""E2E: a parked claude-native permission prompt must survive a host network outage.

Journey: real Claude Code (mock model) asks to create a file -> the
``PermissionRequest`` hook parks the web approval card -> the connection the
hook polls over is interrupted while the card is parked -> connectivity
returns -> the user sends a web message.

Expected: once the server is reachable again the hook re-parks the same
elicitation, the approval card reappears, and the web message is delivered
when the prompt is answered. On the buggy build the card is cleared as
"Prompt expired" during the interruption and never republished, and the web
message never reaches Claude.

The reported host network outage is stood in for by a TCP relay between the
runner (and the hook subprocesses it launches) and the server:
``RUNNER_SERVER_URL`` points at the relay. Cutting it refuses new HTTP
connections through the relay (the hook's permission poll and its re-POSTs)
like a dead NIC. Note that this does NOT take the long-lived runner tunnel
down: it reconnects through the relay within the server's liveness lease and
the runner is never marked offline, so this stand-in exercises only the
hook-poll interruption, not a full host-network outage. The observed
non-delivery is of a web message held client-side in the queued composer; the
report's "202 buffered behind the pending prompt" accounting is not reproduced
here (no pending prompt remains once the card is cleared).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import socket
import subprocess
import textwrap
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from omnigent.harnesses.claude_native.bridge import _BRIDGE_ROOT
from tests._helpers.live_server import find_free_port
from tests._helpers.native_session import create_native_session
from tests._helpers.server_runner import ServerRunner, server_runner
from tests._helpers.session import bind_session_runner
from tests.e2e_ui.conftest import configure_mock_llm, reset_mock_llm, set_fallback_mock_llm
from tests.e2e_ui.messages.test_message_render_parity import _ASSISTANT, _ensure_chat_view, _send
from tests.e2e_ui.messages.test_native_claude_render_parity import (
    _open_terminal_view,
    _wait_terminal_connected,
)
from tests.helpers.ui_configuration import _CLAUDE_MOCK_MODEL

_log = logging.getLogger(__name__)

_APPROVAL_CARD = '[data-testid="approval-card"]'
# A new file: Write always prompts in Claude's default permission mode, whereas
# read-only shell commands such as echo are auto-approved there.
_PROBE_FILE = "omnigent-outage-probe.txt"

# Claude Code boot + first terminal attach.
_CLAUDE_READY_TIMEOUT_S = 180.0
# Mock model answers instantly; the card still needs Claude's tool-call turn
# and the hook subprocess to park.
_CARD_TIMEOUT_MS = 90_000
# Hook re-POSTs after a severed poll back off 1,2,4,8,16,30,30 s and give up on
# the 8th consecutive connect failure (~91 s), then Claude shows its own prompt.
_OUTAGE_S = 120.0
# Server re-park grace (30 s) plus slack: the parked card is cleared when no
# retry re-parks it.
_CARD_CLEAR_TIMEOUT_S = 90.0
_RUNNER_RECONNECT_TIMEOUT_S = 90.0
# Window after recovery for the hook to re-park and the card to come back.
_REPARK_WINDOW_S = 70.0
# Window for a post-recovery web message to reach Claude.
_DELIVERY_WINDOW_S = 40.0


def _queue_followup(page: Page, text: str) -> None:
    """Send a web message, locating the composer by its stable aria-label.

    After the prompt expires the composer enters its queued state and its
    placeholder changes, so a placeholder locator no longer matches; the Send
    button is also hidden while the agent is working, so submit with Enter.
    """
    composer = page.get_by_role("textbox", name="Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(text)
    composer.press("Enter")


class TcpRelay:
    """Loopback TCP relay whose link can be cut and restored mid-flight.

    ``cut()`` closes every live connection on both sides and stops listening,
    so established long-polls are severed and new connects are refused;
    ``restore()`` listens on the same port again.
    """

    def __init__(self) -> None:
        self.port = find_free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self._upstream: tuple[str, int] | None = None
        self._listener: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._live: set[socket.socket] = set()
        self.connections_accepted = 0
        self.connections_refused_since_cut = 0

    def start(self, upstream_host: str, upstream_port: int) -> None:
        self._upstream = (upstream_host, upstream_port)
        self.restore()

    def restore(self) -> None:
        assert self._upstream is not None
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", self.port))
        listener.listen(64)
        self._listener = listener
        self._accept_thread = threading.Thread(
            target=self._accept_loop, args=(listener,), name="tcp-relay-accept", daemon=True
        )
        self._accept_thread.start()

    def cut(self) -> None:
        listener, self._listener = self._listener, None
        if listener is not None:
            with contextlib.suppress(OSError):
                listener.close()
        with self._lock:
            live, self._live = list(self._live), set()
        for sock in live:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(OSError):
                sock.close()
        if self._accept_thread is not None:
            self._accept_thread.join(timeout=5.0)
            self._accept_thread = None

    def live_connections(self) -> int:
        with self._lock:
            return len(self._live) // 2

    def _accept_loop(self, listener: socket.socket) -> None:
        while True:
            try:
                client, _addr = listener.accept()
            except OSError:
                return
            assert self._upstream is not None
            try:
                upstream = socket.create_connection(self._upstream, timeout=10.0)
            except OSError:
                client.close()
                continue
            upstream.settimeout(None)
            with self._lock:
                self._live.update((client, upstream))
                self.connections_accepted += 1
            for src, dst in ((client, upstream), (upstream, client)):
                threading.Thread(
                    target=self._pump, args=(src, dst), name="tcp-relay-pump", daemon=True
                ).start()

    def _pump(self, src: socket.socket, dst: socket.socket) -> None:
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        finally:
            with contextlib.suppress(OSError):
                dst.shutdown(socket.SHUT_WR)
            with self._lock:
                self._live.discard(src)
            with contextlib.suppress(OSError):
                src.close()


def _mock_claude_provider_yaml(mock_llm_server_url: str) -> str:
    return textwrap.dedent(
        f"""\
        providers:
          mock-claude:
            kind: key
            default: [anthropic]
            anthropic:
              base_url: "{mock_llm_server_url}"
              api_key: "mock-key"
              models:
                default: {_CLAUDE_MOCK_MODEL}
        """
    )


@contextlib.contextmanager
def relayed_claude_stack(
    root: Path, mock_llm_server_url: str
) -> Iterator[tuple[ServerRunner, TcpRelay]]:
    """A server plus a runner whose only route to it is a cuttable TCP relay.

    The runner gets an isolated config home carrying the mock Anthropic
    provider (what the shared ``native_claude_mock_session`` fixture writes
    into the ambient config) and its own Claude config dir.
    """
    relay = TcpRelay()
    config_home = root / "config"
    config_home.mkdir(parents=True, exist_ok=True)
    (config_home / "config.yaml").write_text(_mock_claude_provider_yaml(mock_llm_server_url))
    claude_config_dir = root / "claude-config"
    claude_config_dir.mkdir(parents=True, exist_ok=True)
    with server_runner(root, health_timeout=180.0) as stack:
        relay.start("127.0.0.1", stack.port)
        try:
            stack.start_runner(
                env={
                    "RUNNER_SERVER_URL": relay.url,
                    "OMNIGENT_CONFIG_HOME": str(config_home),
                    "CLAUDE_CONFIG_DIR": str(claude_config_dir),
                    "ANTHROPIC_API_KEY": None,
                }
            )
            yield stack, relay
        finally:
            relay.cut()


def runner_online(base_url: str, runner_id: str) -> bool:
    try:
        resp = httpx.get(f"{base_url}/v1/runners/{runner_id}/status", timeout=5.0)
    except httpx.HTTPError:
        return False
    return resp.status_code == 200 and resp.json().get("online") is True


def pending_elicitations(base_url: str, session_id: str) -> list[dict]:
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    resp.raise_for_status()
    return resp.json().get("pending_elicitations") or []


def wait_for(
    predicate: Callable[[], bool], *, timeout_s: float, interval_s: float = 1.0, what: str
) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval_s)
    raise AssertionError(f"{what} not observed within {timeout_s:.0f}s")


def poll_until(
    predicate: Callable[[], bool], *, timeout_s: float, interval_s: float = 1.0
) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return False


def create_claude_session(stack: ServerRunner) -> str:
    # The wrapper's default launch boots Claude Code in auto mode, which would
    # not prompt for the probe command.
    created = create_native_session(
        httpx,
        stack.base_url,
        harness="claude",
        metadata={"terminal_launch_args": ["--permission-mode", "default"]},
    )
    session_id = str(created["session_id"])
    bind_session_runner(httpx.patch, stack.base_url, session_id, stack.runner_id, timeout=10.0)
    return session_id


class ClaudePane:
    """Read the runner-launched Claude TUI through its tmux socket.

    The runner shares this process's temp root, so the bridge directory it
    advertises in ``tmux.json`` is readable here; the pane text is saved as
    evidence next to the browser recording.
    """

    def __init__(self, started_after: float) -> None:
        self._started_after = started_after
        self._tmux: tuple[str, str] | None = None

    def locate(self) -> bool:
        for tmux_json in _BRIDGE_ROOT.glob("*/tmux.json"):
            try:
                if tmux_json.stat().st_mtime < self._started_after:
                    continue
                info = json.loads(tmux_json.read_text())
            except (OSError, ValueError):
                continue
            socket_path, target = info.get("socket_path"), info.get("tmux_target")
            if isinstance(socket_path, str) and isinstance(target, str):
                self._tmux = (socket_path, target)
                return True
        return False

    def capture(self) -> str:
        if self._tmux is None and not self.locate():
            return ""
        assert self._tmux is not None
        socket_path, target = self._tmux
        result = subprocess.run(
            ["tmux", "-S", socket_path, "capture-pane", "-p", "-t", target],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.stdout if result.returncode == 0 else ""


def approval_wait_markers() -> list[str]:
    root = _BRIDGE_ROOT / "approval-waits"
    now = time.time()
    return sorted(
        f"{marker.name} age={now - marker.stat().st_mtime:.0f}s"
        for marker in root.glob("*.wait")
        if marker.exists()
    )


def script_write_probe(mock_llm_server_url: str, nonce: str, workspace: Path) -> None:
    reset_mock_llm(mock_llm_server_url)
    for key in ("default", _CLAUDE_MOCK_MODEL):
        set_fallback_mock_llm(mock_llm_server_url, key, f"ast-done-{nonce}")
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": f"toolu_outage_{nonce}",
                        "name": "Write",
                        "arguments": json.dumps(
                            {
                                "file_path": str(workspace / _PROBE_FILE),
                                "content": f"omnigent-outage-probe {nonce}\n",
                            }
                        ),
                    }
                ]
            }
        ],
        key=_CLAUDE_MOCK_MODEL,
        required_tools=["Write"],
    )


def stack_log_lines(stack: ServerRunner, *needles: str) -> list[str]:
    """Lines from the stack's stdout captures and data-dir logs matching any needle."""
    paths = sorted(
        {
            *stack.root.glob("*.log"),
            *stack.runner_home.rglob("*.log"),
            *stack.server_home.rglob("*.log"),
        }
    )
    matched: list[str] = []
    for path in paths:
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        matched.extend(
            f"{path.name}: {line}" for line in text.splitlines() if any(n in line for n in needles)
        )
    return matched


@pytest.mark.timeout(1200)
def test_parked_permission_survives_host_network_outage(
    request: pytest.FixtureRequest,
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """Card parked -> host network out and back -> card re-parks and messages deliver."""
    evidence_dir = Path(os.environ.get("OMNIGENT_OUTAGE_EVIDENCE_DIR", str(tmp_path / "evidence")))
    evidence_dir.mkdir(parents=True, exist_ok=True)
    notes: list[str] = []

    def note(line: str) -> None:
        stamped = f"{time.strftime('%H:%M:%S')} {line}"
        notes.append(stamped)
        _log.info(stamped)
        (evidence_dir / "journey-notes.log").write_text("\n".join(notes) + "\n")

    started_at = time.time()
    with relayed_claude_stack(tmp_path / "stack", mock_llm_server_url) as (stack, relay):
        session_id = create_claude_session(stack)
        note(f"session {session_id} bound to runner {stack.runner_id} via relay {relay.url}")
        pane = ClaudePane(started_at)
        nonce = uuid.uuid4().hex[:8]
        script_write_probe(mock_llm_server_url, nonce, stack.workspace)

        page: Page = request.getfixturevalue("page")
        page.goto(f"{stack.base_url}/c/{session_id}")
        _open_terminal_view(page)
        _wait_terminal_connected(page)
        wait_for(pane.locate, timeout_s=_CLAUDE_READY_TIMEOUT_S, what="Claude tmux pane")
        _ensure_chat_view(page)
        note("Claude Code attached; chat view shown")

        _send(page, f"Create the file {_PROBE_FILE} with a probe marker in it.")
        card = page.locator(_APPROVAL_CARD).last
        expect(card).to_be_visible(timeout=_CARD_TIMEOUT_MS)
        expect(card.get_by_role("button", name="Approve", exact=True)).to_be_visible(
            timeout=_CARD_TIMEOUT_MS
        )
        parked = pending_elicitations(stack.base_url, session_id)
        assert parked, "the hook parked no elicitation although the card is visible"
        elicitation_id = str(parked[0].get("elicitation_id"))
        note(f"approval card parked: {elicitation_id}; markers={approval_wait_markers()}")
        (evidence_dir / "pane-01-card-parked.txt").write_text(pane.capture())
        page.screenshot(path=str(evidence_dir / "01-card-parked.png"))

        # --- Host network outage: tunnel + hook poll severed, reconnects refused. ---
        relay.cut()
        outage_started = time.monotonic()
        note(f"relay cut ({relay.connections_accepted} connections had been relayed)")
        # The server keeps a runner marked online through an outage shorter than
        # its liveness lease, so the flag is informational only.
        runner_marked_offline = poll_until(
            lambda: not runner_online(stack.base_url, stack.runner_id), timeout_s=30.0
        )
        note(f"server marked the runner offline during outage: {runner_marked_offline}")
        card_cleared = poll_until(
            lambda: not pending_elicitations(stack.base_url, session_id),
            timeout_s=_CARD_CLEAR_TIMEOUT_S,
        )
        note(f"card cleared during outage: {card_cleared}")
        page.screenshot(path=str(evidence_dir / "02-card-during-outage.png"))
        while time.monotonic() - outage_started < _OUTAGE_S:
            time.sleep(5.0)
        (evidence_dir / "pane-02-end-of-outage.txt").write_text(pane.capture())
        note(f"end of outage; markers={approval_wait_markers()}")

        # --- Network returns. ---
        relay.restore()
        note("relay restored")
        wait_for(
            lambda: runner_online(stack.base_url, stack.runner_id),
            timeout_s=_RUNNER_RECONNECT_TIMEOUT_S,
            what="runner back online",
        )
        note("runner back online")
        card_reparked = poll_until(
            lambda: bool(pending_elicitations(stack.base_url, session_id)),
            timeout_s=_REPARK_WINDOW_S,
            interval_s=2.0,
        )
        note(f"card re-parked within {_REPARK_WINDOW_S:.0f}s of recovery: {card_reparked}")
        page.screenshot(path=str(evidence_dir / "03-after-recovery.png"))

        # --- The user sends a web message after recovery. ---
        follow_up = f"after-outage-{nonce}"
        _queue_followup(page, f"Are you still there? {follow_up}")
        note(f"web message sent: {follow_up}")
        if card_reparked:
            page.locator(_APPROVAL_CARD).last.get_by_role(
                "button", name="Approve", exact=True
            ).click()
            note("approval card approved after re-park")
        delivered = poll_until(
            lambda: (
                follow_up in pane.capture()
                or page.locator(_ASSISTANT, has_text=f"ast-done-{nonce}").count() > 0
            ),
            timeout_s=_DELIVERY_WINDOW_S,
            interval_s=2.0,
        )
        buffered = stack_log_lines(stack, "buffering message for pending Claude prompt")
        (evidence_dir / "pane-03-after-web-message.txt").write_text(pane.capture())
        (evidence_dir / "runner-buffering.log").write_text("\n".join(buffered) + "\n")
        (evidence_dir / "stack-elicitation-lines.log").write_text(
            "\n".join(
                stack_log_lines(
                    stack,
                    "elicitation",
                    "permission",
                    "Runner connected",
                    "Runner disconnected",
                    "websocket disconnected",
                    "liveness lease",
                )
            )
            + "\n"
        )
        page.screenshot(path=str(evidence_dir / "04-after-web-message.png"))
        note(
            f"delivered={delivered} buffered_log_lines={len(buffered)} "
            f"pending={pending_elicitations(stack.base_url, session_id)} "
            f"markers={approval_wait_markers()}"
        )
        if delivered or card_reparked:
            time.sleep(3.0)
        page.context.close()

        assert card_reparked, (
            "after the host network returned the parked permission prompt was never "
            f"re-published (card cleared during outage: {card_cleared}); the web message "
            f"was {'delivered' if delivered else 'held'} and the runner logged "
            f"{len(buffered)} 'buffering message for pending Claude prompt' line(s)"
        )
        assert delivered, "web message sent after recovery never reached Claude"
