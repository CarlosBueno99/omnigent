"""Local stand-in for Google's Gemini ``generateContent`` API used by Pi's ``google`` provider.

Pi's Google provider (``pi-ai`` ``google-generative-ai``) honours a provider
``baseUrl`` from ``models.json`` and POSTs ``/models/{model}:streamGenerateContent?alt=sse``.
This server streams scripted Gemini-shaped SSE replies, records every request
body, and applies Google's documented ``function_response.name`` validation:
a request whose ``functionResponse.name`` is empty is rejected with HTTP 400
``INVALID_ARGUMENT``.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

MODEL_ID = "gemini-4-argon"
PI_MODEL_SELECTION = f"google/{MODEL_ID}"


def function_call_reply(name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {"functionCall": {"name": name, "args": args}}


def text_reply(text: str) -> dict[str, Any]:
    return {"text": text}


def _sse_body(part: dict[str, Any]) -> bytes:
    chunk = {
        "candidates": [
            {"content": {"parts": [part], "role": "model"}, "finishReason": "STOP", "index": 0}
        ],
        "usageMetadata": {
            "promptTokenCount": 12,
            "candidatesTokenCount": 6,
            "totalTokenCount": 18,
        },
        "modelVersion": MODEL_ID,
        "responseId": "stand-in-response",
    }
    return f"data: {json.dumps(chunk)}\n\n".encode()


def empty_function_response_names(body: dict[str, Any]) -> list[str]:
    """Return Google-style field paths of every tool result sent with an empty name."""
    offenders: list[str] = []
    for content_index, content in enumerate(body.get("contents") or []):
        for part_index, part in enumerate(content.get("parts") or []):
            response = part.get("functionResponse")
            if response is not None and not response.get("name"):
                offenders.append(
                    f"GenerateContentRequest.contents[{content_index}].parts[{part_index}]"
                    ".function_response.name"
                )
    return offenders


@dataclass
class GeminiStandIn:
    """Scripted replies are consumed in order; a rejected request consumes none."""

    replies: list[dict[str, Any]]
    fallback_text: str = "STAND-IN-FALLBACK"
    requests: list[dict[str, Any]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def handle(self, handler: BaseHTTPRequestHandler) -> None:
        raw = handler.rfile.read(int(handler.headers.get("Content-Length", "0")))
        body = json.loads(raw or b"{}")
        with self.lock:
            self.requests.append({"path": handler.path, "body": body})
            offenders = empty_function_response_names(body)
            if offenders:
                message = "".join(f"* {path}: Name cannot be empty.\n" for path in offenders)
                payload = json.dumps(
                    {"error": {"code": 400, "message": message, "status": "INVALID_ARGUMENT"}}
                ).encode()
                handler.send_response(400)
                handler.send_header("Content-Type", "application/json")
                handler.send_header("Content-Length", str(len(payload)))
                handler.end_headers()
                handler.wfile.write(payload)
                return
            part = self.replies.pop(0) if self.replies else text_reply(self.fallback_text)
        payload = _sse_body(part)
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)


@contextmanager
def serve_gemini_stand_in(stand_in: GeminiStandIn) -> Iterator[str]:
    """Serve *stand_in* on a loopback port; yield its base URL (no API version suffix)."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            stand_in.handle(self)

        def log_message(self, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def write_pi_own_login(home: Path, base_url: str, *, api_key: str = "stand-in-google-key") -> Path:
    """Write Pi's own-login files so ``google/gemini-4-argon`` resolves to the stand-in.

    Mirrors a user who logged Pi into Google with an API key: ``auth.json`` holds
    the credential, ``models.json`` overlays the built-in ``google`` provider
    with the stand-in ``baseUrl`` and the model, and ``models-store.json`` lists
    the model so Omnigent's own-login picker offers it.
    """
    agent_dir = home / ".pi" / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "auth.json").write_text(
        json.dumps({"google": {"type": "api_key", "key": api_key}}, indent=2)
    )
    (agent_dir / "models.json").write_text(
        json.dumps(
            {
                "providers": {
                    "google": {
                        "baseUrl": base_url,
                        "apiKey": api_key,
                        "api": "google-generative-ai",
                        "models": [
                            {
                                "id": MODEL_ID,
                                "name": "Gemini 4 Argon (stand-in)",
                                "reasoning": False,
                                "input": ["text"],
                                "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                                "contextWindow": 1_000_000,
                                "maxTokens": 65_536,
                            }
                        ],
                    }
                }
            },
            indent=2,
        )
    )
    (agent_dir / "models-store.json").write_text(
        json.dumps({"google": {"models": [{"id": MODEL_ID, "name": "Gemini 4 Argon"}]}}, indent=2)
    )
    return agent_dir
