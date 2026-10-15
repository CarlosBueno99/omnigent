"""A host whose sign-in the server no longer accepts launches runners that are refused.

The host keeps its earlier-authenticated tunnel, so it still receives launches;
each new runner's tunnel upgrade is refused with HTTP 401 and the runner exits.
The session failure must lead with the sign-in instruction and the host must
attribute the death as a credential/setup failure at launch, not a runner crash.

A loopback front door stands in for the remote deployment's credential proxy:
it pipes requests carrying an accepted bearer to the real server and answers
HTTP 401 (Databricks edge shape) for every other bearer.

Usage::

    python -m pytest tests/e2e/test_host_runner_refused_credentials_launch.py -v
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import threading
import time
import uuid
import warnings
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest

from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from tests._helpers.host import Host, isolated_host
from tests._helpers.live_server import isolated_local_server
from tests._helpers.session import bundle_files, post_session_bundle
from tests._helpers.zerobus import ZeroBusCapture

_STALE_BEARER = "stale-access-token-" + "0" * 24

# Launch ack + three ~0.5 s upgrade retries + exit report, with CI slack.
_SESSION_FAILURE_TIMEOUT_S = 120.0

_AGENT_YAML = """\
name: refused-credential-host-agent
description: Minimal agent for the refused-credential host launch journey.
executor:
  harness: openai-agents
  model: gpt-5.4
prompt: |
  You are a terse smoke-test assistant.
"""


class _CredentialFrontDoor:
    """Loopback reverse proxy that checks the bearer once per connection.

    Requests whose ``Authorization: Bearer`` is currently accepted are piped to
    the backend byte-for-byte (HTTP and WebSocket upgrades alike), so an
    already-upgraded tunnel survives a later revocation while every new
    connection is checked. Other requests get the Databricks edge's 401.
    """

    def __init__(self, backend_url: str, accepted_tokens: set[str]) -> None:
        parts = urlsplit(backend_url)
        self._backend_host = parts.hostname or "127.0.0.1"
        self._backend_port = parts.port or 80
        self._accepted = set(accepted_tokens)
        self._rejections: list[tuple[str, str, int]] = []
        self._rejections_lock = threading.Lock()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        try:
            self._server = asyncio.run_coroutine_threadsafe(
                asyncio.start_server(self._handle, "127.0.0.1", 0), self._loop
            ).result(timeout=10)
        except BaseException:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=5)
            raise
        self.port = self._server.sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"

    def rejections(self) -> list[tuple[str, str, int]]:
        """Return the ``(method, path, status)`` rejections recorded so far."""
        with self._rejections_lock:
            return list(self._rejections)

    def revoke(self, token: str) -> None:
        """Revoke ``token`` on the proxy loop and wait until the loop applied it."""

        async def _revoke() -> None:
            self._accepted.discard(token)

        asyncio.run_coroutine_threadsafe(_revoke(), self._loop).result(timeout=5)

    def close(self) -> None:
        async def _shutdown() -> None:
            # Piped tunnels stay open until the host exits, so cancel them
            # instead of awaiting wait_closed().
            self._server.close()
            tasks = [t for t in asyncio.all_tasks(self._loop) if t is not asyncio.current_task()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._loop.stop()

        asyncio.run_coroutine_threadsafe(_shutdown(), self._loop)
        self._thread.join(timeout=10)
        if self._thread.is_alive():
            warnings.warn("front door loop did not stop; leaking it", stacklevel=2)
            return
        self._loop.close()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await self._serve(reader, writer)
        except (OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
            pass  # a peer that dropped mid-request is not a test failure
        finally:
            writer.close()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=30)
        request_line, *header_lines = head.decode("latin-1").split("\r\n")
        method, path = [*request_line.split(" "), "", ""][:2]
        headers = {
            name.strip().lower(): value.strip()
            for name, _, value in (line.partition(":") for line in header_lines if line)
        }
        bearer = headers.get("authorization", "").removeprefix("Bearer ").strip()
        if path.startswith("/.well-known/"):
            await self._drain_body(reader, headers)
            await self._respond(writer, 404, {}, b"not found", content_type="text/plain")
            return
        if not bearer or bearer not in self._accepted:
            await self._drain_body(reader, headers)
            with self._rejections_lock:
                self._rejections.append((method, path, 401))
            body = json.dumps(
                {
                    "error_code": 401,
                    "message": (
                        "Credential was not sent or was of an unsupported type for this API."
                    ),
                }
            ).encode()
            await self._respond(
                writer, 401, {"WWW-Authenticate": 'Bearer realm="DatabricksRealm"'}, body
            )
            return
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(
                self._backend_host, self._backend_port
            )
        except OSError:
            await self._respond(writer, 502, {}, b"backend unavailable", content_type="text/plain")
            return
        upstream_writer.write(head)
        await upstream_writer.drain()
        await asyncio.gather(
            self._pipe(reader, upstream_writer),
            self._pipe(upstream_reader, writer),
            return_exceptions=True,
        )

    @staticmethod
    async def _drain_body(reader: asyncio.StreamReader, headers: dict[str, str]) -> None:
        """Read a rejected request's body so closing never resets the client's socket."""
        if headers.get("transfer-encoding", "").lower() == "chunked":
            while True:
                size_line = await asyncio.wait_for(reader.readline(), timeout=10)
                size = int(size_line.split(b";")[0].strip() or b"0", 16)
                await asyncio.wait_for(reader.readexactly(size + 2), timeout=10)
                if size == 0:
                    return
        body_len = int(headers.get("content-length", "0") or 0)
        if body_len:
            await asyncio.wait_for(reader.readexactly(body_len), timeout=10)

    @staticmethod
    async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while chunk := await reader.read(65536):
                writer.write(chunk)
                await writer.drain()
        except ConnectionError:
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    @staticmethod
    async def _respond(
        writer: asyncio.StreamWriter,
        status: int,
        headers: dict[str, str],
        body: bytes,
        *,
        content_type: str = "application/json",
    ) -> None:
        reason = {401: "Unauthorized", 404: "Not Found", 502: "Bad Gateway"}[status]
        lines = [f"HTTP/1.1 {status} {reason}", f"Content-Type: {content_type}"]
        lines += [f"{name}: {value}" for name, value in headers.items()]
        lines += [f"Content-Length: {len(body)}", "Connection: close", "", ""]
        writer.write("\r\n".join(lines).encode("latin-1") + body)
        with contextlib.suppress(ConnectionError):
            await writer.drain()
        writer.close()


def _stage_signed_in_host_home(home: Path, server_url: str, bearer: str) -> None:
    """Leave the files ``omnigent login <server>`` and the Databricks CLI leave behind.

    The pointer record names the fronting workspace; the workspace profile
    still resolves a bearer the front door will stop accepting.
    """
    (home / ".omnigent").mkdir(parents=True, exist_ok=True)
    (home / ".databrickscfg").write_text(f"[stale]\nhost = {server_url}\ntoken = {bearer}\n")
    (home / ".omnigent" / "auth_tokens.json").write_text(
        json.dumps(
            {
                server_url: {
                    "auth_type": "databricks",
                    "workspace_host": server_url,
                    "user_id": "user@example.com",
                }
            },
            indent=2,
        )
    )


def _debug_log_env(capture: ZeroBusCapture) -> dict[str, str]:
    return {
        "OMNIGENT_DEBUG_LOG_CLIENT_ID": "e2e-client",
        "OMNIGENT_DEBUG_LOG_CLIENT_SECRET": "e2e-secret",
        "OMNIGENT_DEBUG_LOG_WORKSPACE_URL": capture.base_url,
        "OMNIGENT_DEBUG_LOG_ENDPOINT": capture.insert_url,
    }


def _register_agent(client: httpx.Client) -> str:
    response = post_session_bundle(
        client.post,
        "/v1/sessions",
        bundle_files({"refused-credential-host-agent.yaml": _AGENT_YAML.encode()}),
        filename="refused-credential-host-agent.tar.gz",
        headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
        timeout=30.0,
    )
    response.raise_for_status()
    return str(response.json()["agent_id"])


def _host_is_online(client: httpx.Client, host_id: str) -> bool:
    response = client.get("/v1/hosts", timeout=10.0)
    response.raise_for_status()
    return any(
        row.get("host_id") == host_id and row.get("status") == "online"
        for row in response.json().get("hosts", [])
    )


def _wait_for_session_failure(
    client: httpx.Client, session_id: str, *, timeout: float
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = client.get(f"/v1/sessions/{session_id}", timeout=10.0)
        response.raise_for_status()
        last = response.json()
        if last.get("status") == "failed" and last.get("last_task_error"):
            return last
        time.sleep(0.5)
    raise AssertionError(f"session {session_id} did not fail within {timeout}s: {last}")


def _runner_log_texts(home: Path) -> dict[str, str]:
    return {
        path.name: path.read_text(errors="replace")
        for path in sorted((home / ".omnigent" / "logs" / "runner").glob("*.log"))
    }


@contextlib.contextmanager
def signed_in_host_behind_front_door(
    root: Path,
    backend_url: str,
    *,
    host_id: str | None = None,
    capture: ZeroBusCapture | None = None,
) -> Iterator[tuple[_CredentialFrontDoor, Host, ZeroBusCapture]]:
    """A connected host that signed in through the front door with a bearer.

    Callers revoke the bearer on the returned front door to stage the
    credential lapse while the host's own tunnel stays connected.
    """
    with contextlib.ExitStack() as stack:
        if capture is None:
            capture = stack.enter_context(contextlib.closing(ZeroBusCapture()))
        client = stack.enter_context(httpx.Client(base_url=backend_url, trust_env=False))
        front_door = _CredentialFrontDoor(backend_url, {_STALE_BEARER})
        stack.callback(front_door.close)
        home = root / "home"
        _stage_signed_in_host_home(home, front_door.url, _STALE_BEARER)
        host = stack.enter_context(
            isolated_host(
                root,
                front_door.url,
                env=_debug_log_env(capture),
                host_id=host_id or uuid.uuid4().hex,
                name=f"refused-credential-host-{uuid.uuid4().hex[:8]}",
            )
        )
        host.wait_online(client, timeout=60, poll_interval=0.25)
        yield front_door, host, capture


def _first_line(text: str) -> str:
    return next((line for line in text.splitlines() if line.strip()), "")


def _attribution_rows(rows: list[dict[str, Any]], source: str, runner_id: str) -> list[dict]:
    return [
        row
        for row in rows
        if row.get("source") == source
        and isinstance(row.get("attributes"), dict)
        and row["attributes"].get("error_category") is not None
        and row["attributes"].get("runner_id") == runner_id
    ]


@pytest.fixture(scope="module")
def refused_launch(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    """Run the journey once; both symptom checks read the same outcome."""
    tmp_path = tmp_path_factory.mktemp("refused-launch")
    with (
        contextlib.closing(ZeroBusCapture()) as capture,
        isolated_local_server(tmp_path / "server", env=_debug_log_env(capture)) as backend_url,
        httpx.Client(base_url=backend_url, trust_env=False) as client,
        signed_in_host_behind_front_door(
            tmp_path / "host",
            backend_url,
            host_id=os.environ.get("OMNIGENT_E2E_HOST_ID"),
            capture=capture,
        ) as (front_door, host, _),
    ):
        front_door.revoke(_STALE_BEARER)
        assert _host_is_online(client, host.host_id), "host tunnel should survive revocation"

        agent_id = _register_agent(client)
        workspace = tmp_path / "project"
        workspace.mkdir()
        created = time.monotonic()
        create = client.post(
            "/v1/sessions",
            json={"agent_id": agent_id, "host_id": host.host_id, "workspace": str(workspace)},
            timeout=90.0,
        )
        create.raise_for_status()
        session_id = create.json()["id"]
        session = _wait_for_session_failure(client, session_id, timeout=_SESSION_FAILURE_TIMEOUT_S)
        seconds_to_failed = round(time.monotonic() - created, 2)
        runner_id = session.get("runner_id")
        assert runner_id, f"failed session carries no runner_id: {session}"
        runner_id = str(runner_id)
        # Wait for both host rows and the server row for this runner to flush.
        deadline = time.monotonic() + 30.0
        while True:
            rows = capture.rows()
            host_events = {r.get("event_name") for r in _attribution_rows(rows, "host", runner_id)}
            if {"runner_died", "runner_never_connected"} <= host_events and _attribution_rows(
                rows, "server", runner_id
            ):
                break
            assert time.monotonic() < deadline, (
                f"debug-log rows for runner {runner_id} did not arrive within 30s: "
                f"host events={sorted(e for e in host_events if e)}"
            )
            time.sleep(0.5)
        outcome = {
            "session": session,
            "seconds_to_failed": seconds_to_failed,
            "front_door_rejections": front_door.rejections(),
            "runner_logs": _runner_log_texts(host.home),
            "host_rows": _attribution_rows(rows, "host", runner_id),
            "server_rows": _attribution_rows(rows, "server", runner_id),
        }
        print("REFUSED_LAUNCH_OUTCOME " + json.dumps(outcome, default=str))
        yield outcome


@pytest.mark.timeout(420)
def test_refused_runner_credentials_name_sign_in(refused_launch: dict[str, Any]) -> None:
    """A credential refusal at runner launch must read as "sign in again"."""
    session = refused_launch["session"]
    error = session["last_task_error"]
    message = str(error.get("message", ""))
    tunnel_rejections = [
        path for _m, path, _s in refused_launch["front_door_rejections"] if "/tunnel" in path
    ]
    assert tunnel_rejections, (
        "precondition broken: the front door never refused a runner tunnel upgrade\n"
        f"rejections={refused_launch['front_door_rejections']}\n"
        f"runner logs={refused_launch['runner_logs']}"
    )
    headline = _first_line(message)
    assert "login" in headline, (
        "session failure does not lead with the sign-in instruction:\n"
        f"code={error.get('code')!r}\n--- message ---\n{message}"
    )
    assert "CancelledError" not in message, (
        f"an unrelated shutdown traceback precedes the cause:\n{message}"
    )


@pytest.mark.timeout(420)
def test_refused_runner_credentials_attributed_as_launch_setup(
    refused_launch: dict[str, Any],
) -> None:
    """The host must attribute the refused launch as a setup failure, not a crash."""
    host_rows = refused_launch["host_rows"]
    runner_id = refused_launch["session"].get("runner_id")
    assert host_rows, f"no host attribution row for runner {runner_id!r}"
    attribution = {
        (row["attributes"].get("error_category"), row["attributes"].get("error_phase"))
        for row in host_rows
    }
    observed = [
        (
            r.get("event_name"),
            r["attributes"].get("error_category"),
            r["attributes"].get("error_phase"),
        )
        for r in host_rows
    ]
    server_observed = [
        (
            r.get("event_name"),
            r["attributes"].get("error_category"),
            r["attributes"].get("error_phase"),
        )
        for r in refused_launch["server_rows"]
    ]
    assert not [row for row in observed if row[1] == "runner"], (
        "host attributed the refused launch to the runner: "
        f"host rows={observed}; server rows={server_observed}"
    )
    assert ("config", "runner_launch") in attribution, (
        "host did not attribute the refused launch as a launch setup failure: "
        f"host rows={observed}; server rows={server_observed}"
    )
    assert server_observed and all(
        (category, phase) == ("config", "runner_launch") for _n, category, phase in server_observed
    ), f"server did not mirror the launch setup attribution: server rows={server_observed}"
