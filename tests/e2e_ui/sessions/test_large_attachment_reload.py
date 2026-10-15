"""Browser e2e: a session holding a multi-MB attachment must still reopen.

Reported journey: attach a large PDF in the chat composer, send, let the reply
finish, then reload the page. On a deployment whose conversation store
batch-decrypts each items page through a gRPC CMK sidecar with a 1 MiB inbound
frame limit, the reload answered HTTP 500 and the conversation could never be
opened again.

This build decodes item payloads in-process, so the spawned server runs a
stand-in decode seam modelling that boundary: the decrypt frame is the page's
stored payload plus the base64 size of every attachment the page references
by ``file_id`` (the report states attachment bytes are stored inline in the
encrypted item), and a frame over 1 MiB raises the sidecar's
``RESOURCE_EXHAUSTED`` RpcError. Under that model the message carrying the
attachment can never be decoded on its own, so the expected outcome is a
readable conversation — the reply plus a clear placeholder for that message —
rather than a 500. Everything else — upload, message, turn, reload,
``list_items``, the error handler and the SPA — is the real product.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
from playwright.sync_api import Browser, Page, expect

from tests._helpers.server_runner import ServerRunner, server_runner
from tests._helpers.session import bind_session_runner, post_session_bundle
from tests.e2e_ui.conftest import _build_hello_world_bundle, configure_mock_llm

# A 2.66 MB PDF: its base64 form alone exceeds the 1 MiB frame cap.
LARGE_PDF_BYTES = 2_660_000
MODEL_KEY = "gpt-4o-mini"
REPLY = "I received your PDF."
_COMPOSER_LABEL = "Message the agent"
_USER = '[data-testid="message-bubble"][data-role="user"]'
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_WORKING = '[data-testid="working-indicator"]'
_PLACEHOLDER_TITLE = "Part of this conversation couldn't be loaded"
_PLACEHOLDER_DETAIL = "Its stored content could not be read"

# Runs inside the spawned server before the CLI starts (``python -c`` bootstrap).
SERVER_BOOTSTRAP = """
import json, os
import grpc
from omnigent.stores.conversation_store import sqlalchemy_store as _conv
from omnigent.stores.file_store import sqlalchemy_store as _files

_FRAME_LIMIT = 1024 * 1024
_LOG = os.environ.get("DECODE_FRAME_LOG")
_file_stores = []
_orig_file_init = _files.SqlAlchemyFileStore.__init__


def _file_init(self, *args, **kwargs):
    _orig_file_init(self, *args, **kwargs)
    _file_stores.append(self)


_files.SqlAlchemyFileStore.__init__ = _file_init


class _SidecarRpcError(grpc.RpcError):
    def __init__(self, frame):
        self._details = f"Frame size {frame} exceeds maximum: {_FRAME_LIMIT}"
        super().__init__(f"RPC terminated with RESOURCE_EXHAUSTED. {self._details}")

    def code(self):
        return grpc.StatusCode.RESOURCE_EXHAUSTED

    def details(self):
        return self._details


def _attachment_bytes(stored):
    total = 0
    for raw in stored:
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        content = data.get("content") if isinstance(data, dict) else None
        for block in content or []:
            file_id = block.get("file_id") if isinstance(block, dict) else None
            if not file_id:
                continue
            for store in _file_stores:
                meta = store.get(file_id)
                if meta is not None:
                    total += (meta.bytes + 2) // 3 * 4
                    break
    return total


def _decode_item_data_batch(self, stored):
    payload = sum(len(s) if isinstance(s, bytes) else len(s.encode()) for s in stored)
    attachments = _attachment_bytes(stored)
    frame = payload + attachments
    outcome = "ok" if frame <= _FRAME_LIMIT else "RESOURCE_EXHAUSTED"
    if _LOG:
        with open(_LOG, "a") as fh:
            fh.write(json.dumps({"items": len(stored), "payload": payload,
                                 "attachments": attachments, "frame": frame,
                                 "outcome": outcome}) + "\\n")
    if frame > _FRAME_LIMIT:
        raise _SidecarRpcError(frame)
    return stored


_conv.SqlAlchemyConversationStore._decode_item_data_batch = _decode_item_data_batch

from omnigent.cli import main

main()
"""


def write_pdf(path: Path, target_bytes: int) -> int:
    """Write a valid one-page PDF padded with an incompressible stream to about *target_bytes*."""
    header = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
    text = b"BT /F1 24 Tf 72 720 Td (large attachment fixture) Tj ET"
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(text) + text + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    fixed = sum(len(o) + 24 for o in objs) + len(header) + 200
    pad = os.urandom(max(0, target_bytes - fixed))
    objs.append(b"<< /Length %d >>\nstream\n" % len(pad) + pad + b"\nendstream")
    out = bytearray(header)
    offsets = []
    for index, obj in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % index + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % off for off in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    path.write_bytes(out)
    return len(out)


@contextmanager
def stand_in_cmk_stack(root: Path, mock_url: str, decode_log: Path) -> Iterator[ServerRunner]:
    """Spawn server + runner where the server's decode seam models the 1 MiB CMK frame cap."""
    provider_env = {"OPENAI_BASE_URL": f"{mock_url}/v1", "OPENAI_API_KEY": "mock-key"}
    with server_runner(
        root,
        server_bootstrap=SERVER_BOOTSTRAP,
        server_env={**provider_env, "DECODE_FRAME_LOG": str(decode_log)},
    ) as stack:
        stack.start_runner(env=provider_env)
        yield stack


def create_bound_session(client: httpx.Client, stack: ServerRunner) -> str:
    create = post_session_bundle(
        client.post, f"{stack.base_url}/v1/sessions", _build_hello_world_bundle(), timeout=30.0
    )
    create.raise_for_status()
    session_id = str(create.json()["session_id"])
    bind_session_runner(client.patch, stack.base_url, session_id, stack.runner_id, timeout=10.0)
    return session_id


def send_message(page: Page, text: str) -> None:
    page.get_by_label(_COMPOSER_LABEL).fill(text)
    send = page.get_by_role("button", name="Send", exact=True)
    expect(send).to_be_enabled(timeout=10_000)
    send.click()
    expect(page.locator(_USER).first).to_be_visible(timeout=30_000)
    expect(page.locator(_ASSISTANT).first).to_be_visible(timeout=60_000)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=60_000)


def attach_send_reload(
    page: Page, base_url: str, session_id: str, pdf: Path, mark: Callable[[str], None]
) -> tuple[int, str]:
    """Drive the reported journey; return the status/body of the items request after reload."""

    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_label(_COMPOSER_LABEL)).to_be_visible(timeout=30_000)
    mark("composer_visible")
    page.locator('input[type="file"][accept*="application/pdf"]').first.set_input_files(str(pdf))
    expect(page.get_by_role("button", name=f"Remove {pdf.name}")).to_be_visible(timeout=15_000)
    mark("attachment_chip_visible")
    page.wait_for_timeout(1_500)
    send_message(page, "Please read this PDF.")
    mark("reply_finished")
    page.wait_for_timeout(1_500)
    mark("reload")
    with page.expect_response(
        lambda response: f"/v1/sessions/{session_id}/items" in response.url, timeout=30_000
    ) as reloaded:
        page.reload()
    response = reloaded.value
    mark("items_response")
    return response.status, response.text()


def test_session_with_multi_mb_attachment_reopens_with_placeholder(
    browser: Browser, mock_llm_server_url: str, tmp_path: Path
) -> None:
    """Attach a 2.66 MB PDF, send, reload: the conversation loads again (no 500),
    showing the reply and a placeholder for the message the store cannot decode."""
    pdf = tmp_path / "quarterly-report.pdf"
    write_pdf(pdf, LARGE_PDF_BYTES)
    decode_log = tmp_path / "decode-frames.jsonl"
    with (
        httpx.Client(trust_env=False) as client,
        stand_in_cmk_stack(tmp_path / "stack", mock_llm_server_url, decode_log) as stack,
    ):
        configure_mock_llm(mock_llm_server_url, [{"text": REPLY}] * 3, key=MODEL_KEY)
        session_id = create_bound_session(client, stack)
        context = browser.new_context()
        started = time.monotonic()
        timeline: dict[str, float] = {}

        def mark(step: str) -> None:
            timeline[step] = round(time.monotonic() - started, 2)

        try:
            page = context.new_page()
            status, body = attach_send_reload(page, stack.base_url, session_id, pdf, mark)
            frames = decode_log.read_text() if decode_log.exists() else ""
            print(f"items_after_reload status={status} body={body[:200]!r}")
            assert status == 200, (
                f"GET /items after reload answered {status}: {body[:300]}\n"
                f"decode frames:\n{frames}"
            )
            kinds = [
                (item["type"], item.get("code") or item.get("role"))
                for item in json.loads(body)["data"]
            ]
            print(f"items_after_reload kinds={kinds}")
            assert kinds.count(("error", "item_data_unreadable")) == 1, kinds
            assert ("message", "assistant") in kinds, kinds

            expect(page.get_by_role("heading", name="Conversation not found")).to_have_count(0)
            expect(page.get_by_label(_COMPOSER_LABEL)).to_be_visible(timeout=30_000)
            expect(page.get_by_text(REPLY).first).to_be_visible()
            placeholder = page.get_by_role("button", name=_PLACEHOLDER_TITLE)
            expect(placeholder).to_be_visible()
            placeholder.click()
            expect(page.get_by_text(_PLACEHOLDER_DETAIL).first).to_be_visible()
            mark("placeholder_expanded")
            page.wait_for_timeout(3_000)
            page.screenshot(path=str(tmp_path / "after-reload-large.png"))
            mark("end")
            print(f"timeline={timeline}")
        finally:
            context.close()
            print(f"session={session_id} decode_frame_log={decode_log}")
