"""Read a session terminal's screen through its read-only attach WebSocket.

The attach bridge seeds every new client with tmux's ``capture-pane`` replay
of the current screen, so one short-lived connection yields the pane text
without the runner's tmux socket, which lives in the runner's private TMPDIR.
"""

from __future__ import annotations

import time
from urllib.parse import urlsplit, urlunsplit

import pyte
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

# Wider and taller than any browser terminal, so replayed rows keep their
# pane coordinates instead of wrapping or scrolling off.
_SCREEN_COLS = 400
_SCREEN_ROWS = 200
_QUIET_S = 0.5


def terminal_attach_url(
    base_url: str, session_id: str, terminal_id: str, *, read_only: bool = True
) -> str:
    """Return the WebSocket URL of a session terminal's attach endpoint."""
    parts = urlsplit(base_url)
    scheme = "wss" if parts.scheme == "https" else "ws"
    path = (
        f"{parts.path.rstrip('/')}/v1/sessions/{session_id}"
        f"/resources/terminals/{terminal_id}/attach"
    )
    query = f"read_only={'true' if read_only else 'false'}"
    return urlunsplit((scheme, parts.netloc, path, query, ""))


def read_terminal_screen(
    base_url: str,
    session_id: str,
    terminal_id: str = "terminal_claude_main",
    *,
    settle_s: float = _QUIET_S,
    timeout_s: float = 8.0,
) -> str:
    """Return the terminal's visible text, or ``""`` when nothing was received.

    Opens a fresh read-only attach, feeds its frames into a VT emulator until
    the stream is quiet for *settle_s* (or *timeout_s* elapses), and returns
    the rows with trailing blanks removed.
    """
    screen = pyte.Screen(_SCREEN_COLS, _SCREEN_ROWS)
    stream = pyte.ByteStream(screen)
    deadline = time.monotonic() + timeout_s
    received = False
    url = terminal_attach_url(base_url, session_id, terminal_id)
    with connect(url, max_size=None, open_timeout=10) as ws:
        while time.monotonic() < deadline:
            try:
                frame = ws.recv(timeout=settle_s)
            except (TimeoutError, ConnectionClosed):
                break
            if isinstance(frame, bytes):
                stream.feed(frame)
                received = True
    if not received:
        return ""
    lines = [row.rstrip() for row in screen.display]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)
