"""Tool completion waits for durable PR metadata and keeps failed snapshots replayable."""

from __future__ import annotations

import asyncio
import json

import httpx
from filelock import FileLock

from omnigent.runner import session_prs
from omnigent.runner.session_pr_sync import restore_session_prs, sync_session_prs
from omnigent.runner.session_prs import PullRequestRef, SessionPrRegistry

URL = "https://github.com/example/repo/pull/42"


async def test_sync_retries_failure_skips_missing_and_publishes_final_removal(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    registry = SessionPrRegistry("session")
    requests: list[dict] = []
    await restore_session_prs("session", [{"url": "invalid"}])
    assert registry.snapshot() is None

    def receive(request: httpx.Request) -> httpx.Response:
        # The local file lock must be released while the server acknowledges the update.
        with FileLock(str(registry.path) + ".lock", timeout=0):
            requests.append(json.loads(request.content))
        assert request.url.path == "/v1/sessions/session/events"
        return httpx.Response(503 if len(requests) == 1 else 200)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(receive), base_url="http://server"
    ) as client:
        await sync_session_prs("session", client)
        assert requests == []
        registry.record([PullRequestRef.from_url(URL)], relationship="created", source="test")
        await sync_session_prs("session", client)
        assert len(requests) == 1 and registry.list()[0].url == URL
        await sync_session_prs("session", client)
        assert requests[0] == requests[1]
        assert requests[1]["type"] == "external_session_prs"
        assert requests[1]["data"]["prs"][0]["url"] == URL
        await sync_session_prs("session", client)
        assert len(requests) == 2
        registry.remove(URL)
        await sync_session_prs("session", client)
        assert requests[-1]["data"]["prs"] == []
        assert requests[-1]["data"]["updated_at_ns"] > requests[0]["data"]["updated_at_ns"]
        registry.path.write_text("invalid json")
        await sync_session_prs("session", client)
        assert len(requests) == 3


async def test_sync_serializes_concurrent_snapshots_until_server_ack(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    registry = SessionPrRegistry("session")
    registry.record([PullRequestRef.from_url(URL)], relationship="created", source="test")
    receiving = asyncio.Event()
    acknowledge = asyncio.Event()
    requests: list[dict] = []

    async def receive(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            receiving.set()
            await acknowledge.wait()
        return httpx.Response(200)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(receive), base_url="http://server"
    ) as client:
        first = asyncio.create_task(sync_session_prs("session", client))
        await receiving.wait()
        assert not first.done()
        monkeypatch.setattr(session_prs.time, "time_ns", lambda: 1)
        registry.remove(URL)
        second = asyncio.create_task(sync_session_prs("session", client))
        await asyncio.sleep(0)
        assert len(requests) == 1 and not second.done()
        acknowledge.set()
        await asyncio.gather(first, second)
        assert len(requests) == 2
        assert requests[0]["data"]["prs"][0]["url"] == URL
        assert requests[1]["data"]["prs"] == []
        assert requests[1]["data"]["updated_at_ns"] > requests[0]["data"]["updated_at_ns"]


def test_restore_preserves_server_rows_and_keeps_unversioned_local_state(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    source = SessionPrRegistry("source")
    source.record(
        [PullRequestRef.from_url(URL)], relationship="created", source="sdk", timestamp=123
    )
    source.update_titles({URL: "Created remotely"}, timestamp=456)
    entries = [entry.model_dump() for entry in source.list()]
    target = SessionPrRegistry("target")
    target.restore(None)
    assert target.snapshot() is None
    server_snapshot = source.snapshot()
    assert server_snapshot is not None
    remote_revision = server_snapshot[0] + 1_000_000_000
    monkeypatch.setattr(session_prs.time, "time_ns", lambda: 1)
    target.restore(entries, updated_at_ns=remote_revision)
    assert target.list() == source.list()
    restored = target.snapshot()
    assert restored is not None and restored[0] > remote_revision
    target.remove(URL)
    target.restore(entries)
    assert target.list() == []
    empty = SessionPrRegistry("empty")
    empty.restore([])
    empty.restore(entries)
    assert empty.snapshot() is not None
    assert empty.list() == []


def test_newer_server_snapshot_restores_returning_host_and_preserves_removals(tmp_path):
    first_host = SessionPrRegistry("session", root=tmp_path / "first")
    second_host = SessionPrRegistry("session", root=tmp_path / "second")
    original = PullRequestRef.from_url(URL)
    added = PullRequestRef.from_url(URL.replace("42", "43"))
    unsynced = PullRequestRef.from_url(URL.replace("42", "44"))
    first_host.record([original], relationship="created", source="sdk", observation_id="first")
    first_snapshot = first_host.snapshot()
    assert first_snapshot is not None
    second_host.restore(
        [entry.model_dump() for entry in first_snapshot[1]], updated_at_ns=first_snapshot[0]
    )
    second_host.record([added], relationship="created", source="sdk")
    second_snapshot = second_host.snapshot()
    assert second_snapshot is not None
    first_host.restore(
        [entry.model_dump() for entry in second_snapshot[1]], updated_at_ns=second_snapshot[0]
    )
    assert {entry.url for entry in first_host.list()} == {original.url, added.url}
    first_host.record([unsynced], relationship="created", source="sdk")
    first_host.restore([], updated_at_ns=second_snapshot[0])
    assert {entry.url for entry in first_host.list()} == {original.url, added.url, unsynced.url}
    local_snapshot = first_host.snapshot()
    assert local_snapshot is not None
    first_host.restore([], updated_at_ns=local_snapshot[0] + 1)
    first_host.record([original], relationship="created", source="sdk", observation_id="replay")
    assert first_host.list() == []
    empty_snapshot = first_host.snapshot()
    assert empty_snapshot is not None
    first_host.restore(
        [entry.model_dump() for entry in first_snapshot[1]],
        updated_at_ns=empty_snapshot[0] + 1,
    )
    assert first_host.list() == first_snapshot[1]
