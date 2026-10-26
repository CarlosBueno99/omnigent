"""Durable queue controls share authorization and atomic delivery boundaries."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from tests.server.helpers import create_test_agent
from tests.server.integration.test_sessions_permissions import _create_session_as

pytestmark = pytest.mark.asyncio


def message(text: str) -> dict[str, Any]:
    return {
        "type": "message",
        "data": {
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
            "stable_id": uuid4().hex,
        },
    }


async def test_queue_controls_survive_reopen_and_cannot_change_claimed_inputs(
    client: httpx.AsyncClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = app.state.startup_inputs
    monkeypatch.setattr(service, "wake_session", AsyncMock())
    agent = await create_test_agent(client)
    created = await client.post("/v1/sessions", json={"agent_id": agent["id"]})
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]
    url = f"/v1/sessions/{session_id}/queue"
    payloads = [message(text) for text in ("first", "second", "third")]
    ids = [body["data"]["stable_id"] for body in payloads]
    for body, item_id in zip(payloads, ids, strict=True):
        response = await client.post(url, json=body)
        assert response.status_code == 202, response.text
        assert response.json()["item_id"] == item_id
    # A lost acknowledgement is safe to retry, even from another client.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as reopened:
        assert (await reopened.post(url, json=payloads[0])).status_code == 202
        first = (await reopened.get(url, params={"limit": 2})).json()
        assert [item["id"] for item in first["data"]] == ids[:2]
        second = (
            await reopened.get(url, params={"limit": 2, "after": first["next_cursor"]})
        ).json()
        assert [item["id"] for item in second["data"]] == ids[2:]
        assert second["next_cursor"] is None
        snapshot = (await reopened.get(f"/v1/sessions/{session_id}")).json()
        assert [item["id"] for item in snapshot["queued_inputs"]] == ids
        assert snapshot["pending_inputs"] == []
        assert not any(item["type"] == "message" for item in snapshot["items"])

    changed = await client.patch(
        f"{url}/{ids[2]}", json={"action": "reorder", "before_id": ids[0]}
    )
    assert changed.status_code == 200, changed.text
    assert [item["id"] for item in (await client.get(url)).json()["data"]] == [
        ids[2],
        ids[0],
        ids[1],
    ]
    removed = await client.delete(f"{url}/{ids[0]}")
    assert removed.status_code == 200, removed.text
    assert removed.json()["data"]["stable_id"] == ids[0]
    assert (await client.delete(f"{url}/{ids[0]}")).status_code == 404

    store = service.conversations
    queued = store.get_item(session_id, ids[2])
    options = queued.data.startup_options
    assert store.transition_pending_input(
        session_id,
        ids[2],
        expected="completed",
        status="completed",
        expected_options=options,
        options=options.model_copy(update={"requires_retry": True}),
    )
    assert (await client.get(url)).json()["data"][0]["status"] == "failed"
    assert (await client.patch(f"{url}/{ids[2]}", json={"action": "retry"})).status_code == 200
    retry = store.get_item(session_id, ids[2])
    assert not retry.data.startup_options.wait_for_idle
    assert not retry.data.startup_options.requires_retry
    assert store.transition_pending_input(
        session_id, ids[2], expected="completed", status="incomplete", queued_only=True
    )
    assert (await client.delete(f"{url}/{ids[2]}")).status_code == 409
    assert (await client.patch(f"{url}/{ids[2]}", json={"action": "steer"})).status_code == 409


async def test_queue_requires_session_access_and_edit_permission(
    auth_client: httpx.AsyncClient, auth_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(auth_app.state.startup_inputs, "wake_session", AsyncMock())
    owner = {"X-Forwarded-Email": "owner@example.com"}
    viewer = {"X-Forwarded-Email": "viewer@example.com"}
    created = await _create_session_as(auth_client, "unused", "owner@example.com")
    session_url = f"/v1/sessions/{created['id']}"
    url = session_url + "/queue"
    payload = message("private queued message")
    response = await auth_client.post(url, json=payload, headers=owner)
    assert response.status_code == 202, response.text
    item_url = url + "/" + response.json()["item_id"]
    assert (await auth_client.get(url, headers=viewer)).status_code == 404
    granted = await auth_client.put(
        session_url + "/permissions",
        json={"user_id": "viewer@example.com", "level": 1},
        headers=owner,
    )
    assert granted.status_code == 200, granted.text
    assert (await auth_client.get(url, headers=viewer)).status_code == 200
    assert (await auth_client.post(url, json=message("denied"), headers=viewer)).status_code == 403
    assert (await auth_client.delete(item_url, headers=viewer)).status_code == 403
    assert (
        await auth_client.patch(item_url, json={"action": "steer"}, headers=viewer)
    ).status_code == 403


async def test_queue_validates_body_content_type_and_pagination(
    client: httpx.AsyncClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app.state.startup_inputs, "wake_session", AsyncMock())
    agent = await create_test_agent(client)
    created = await client.post("/v1/sessions", json={"agent_id": agent["id"]})
    url = f"/v1/sessions/{created.json()['id']}/queue"
    assert (await client.post(url, json={"type": "interrupt"})).status_code == 400
    response = await client.post(
        url, content='{"type":"compact"}', headers={"content-type": "text/plain"}
    )
    assert response.status_code == 415
    assert (await client.get(url, params={"limit": 101})).status_code == 422
    assert (await client.get(url, params={"after": -1})).status_code == 422
