"""A rate-limiting API front end stand-in for the runner's agent-spec fetch.

``install()`` wraps the product app in a pure-ASGI edge that answers
``GET /v1/sessions/{id}/agent/contents`` with HTTP 429 for a bounded window that
starts at the first such fetch after each arming (the flag file holds the arming
request, then the window's start time) and passes everything else, including the
runner tunnel, through untouched. Run it from the server bootstrap::

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
ARM_ON_NEXT_FETCH = "next-fetch"

_SPEC_FETCH = re.compile(r"^/v1/sessions/[^/]+/agent/contents$")


def _replace_text(path: Path, text: str) -> None:
    """Write *text* atomically so a concurrent reader never sees a partial file."""
    pending = path.with_suffix(".tmp")
    pending.write_text(text)
    os.replace(pending, path)


def arm(flag: Path) -> None:
    """Throttle spec fetches for the window, starting at the next one."""
    _replace_text(flag, ARM_ON_NEXT_FETCH)


class SpecFetchThrottle:
    """Answer spec fetches with 429 for *window* seconds from the first one after arming."""

    def __init__(self, app: Any, *, flag: Path, counter: Path, window: float) -> None:
        self._app = app
        self._flag = flag
        self._counter = counter
        self._window = window
        # A restarted server process continues the counter instead of resetting it.
        self._served: list[str] = served_429s(counter)

    def _armed(self) -> bool:
        try:
            flag = self._flag.read_text().strip()
        except OSError:
            return False
        if flag == ARM_ON_NEXT_FETCH:
            # The window starts at the first throttled fetch rather than when the
            # test armed it, so slow UI setup in between cannot shorten it.
            _replace_text(self._flag, str(time.time()))
            return True
        try:
            armed_at = float(flag)
        except ValueError:
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
            _replace_text(self._counter, json.dumps(self._served))
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
