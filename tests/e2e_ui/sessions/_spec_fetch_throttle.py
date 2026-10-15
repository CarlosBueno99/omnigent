"""A rate-limiting API front end stand-in for the runner's agent-spec fetch.

Production puts the server behind an API front end that answers bursts with
HTTP 429 (``RESOURCE_EXHAUSTED``). Locally nothing in front of the server does
that, so ``install()`` wraps the product app in a pure-ASGI edge that throttles
``GET /v1/sessions/{id}/agent/contents`` while armed, and passes everything else
(including the runner tunnel) through untouched. The edge is armed by writing the
flag file with the current epoch time; it then throttles every matching fetch for
a bounded window and lifts automatically, so a runner that retried the transient
429 would recover. Run it from the server process bootstrap::

    python -c "from tests.e2e_ui.sessions._spec_fetch_throttle import install; \\
               install(); from omnigent.cli import main; main()" server ...
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

FLAG_ENV = "OMNIGENT_E2E_SPEC_FETCH_THROTTLE_FLAG"
COUNTER_ENV = "OMNIGENT_E2E_SPEC_FETCH_THROTTLE_COUNTER"
WINDOW_ENV = "OMNIGENT_E2E_SPEC_FETCH_THROTTLE_WINDOW"

_SPEC_FETCH = re.compile(r"^/v1/sessions/[^/]+/agent/contents$")


class SpecFetchThrottle:
    """Answer every spec fetch with 429 for *window* seconds after each arming."""

    def __init__(self, app: Any, *, flag: Path, counter: Path, window: float) -> None:
        self._app = app
        self._flag = flag
        self._counter = counter
        self._window = window
        self._served: list[str] = []

    def _armed(self) -> bool:
        try:
            armed_at = float(self._flag.read_text().strip())
        except (OSError, ValueError):
            return False
        return time.time() < armed_at + self._window

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if (
            scope["type"] == "http"
            and scope["method"] == "GET"
            and _SPEC_FETCH.match(scope["path"])
            and self._armed()
        ):
            self._served.append(scope["path"])
            self._counter.write_text(json.dumps(self._served))
            body = json.dumps(
                {"error_code": "RESOURCE_EXHAUSTED", "message": "Too many requests"}
            ).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 429,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                        (b"retry-after", b"1"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        await self._app(scope, receive, send)


def install() -> None:
    """Wrap ``create_app`` so the server process serves behind the throttle."""
    import omnigent.server.app as server_app

    flag = Path(os.environ[FLAG_ENV])
    counter = Path(os.environ[COUNTER_ENV])
    window = float(os.environ.get(WINDOW_ENV, "15"))
    original = server_app.create_app

    def create_app(*args: Any, **kwargs: Any) -> Any:
        return SpecFetchThrottle(
            original(*args, **kwargs), flag=flag, counter=counter, window=window
        )

    server_app.create_app = create_app


def served_429s(counter: Path) -> list[str]:
    """Return the spec-fetch paths the edge has throttled so far."""
    if not counter.exists():
        return []
    return list(json.loads(counter.read_text()))
