"""Completed native messages must pass policy before durable or live publication."""

import json
from typing import Any

import httpx
import pytest

from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from tests.server.helpers import create_session_for_agent, create_test_agent
from tests.server.integration.test_sessions_endpoints import _capture_published
from tests.server.integration.test_sessions_policy_evaluate import _patch_default_policies

_DENY_REASON = "Response contains the tripwire token"
_SENTINEL = f"[Denied by policy: {_DENY_REASON}]"
_CEL_EXPRESSION = (
    'event.type == "response" && event.data.contains("TRIPWIRE") '
    f'? {{"result": "DENY", "reason": "{_DENY_REASON}"}} : {{"result": "ALLOW"}}'
)


def _review_output(event: dict[str, Any]) -> dict[str, str]:
    if event.get("type") == "response" and "review-marker" in str(event.get("data")):
        return {"result": "DENY", "reason": "Output requires review"}
    return {"result": "ALLOW"}


def _external_event(kind: str, text: str) -> dict[str, Any]:
    if kind == "external_assistant_message":
        return {
            "type": kind,
            "data": {"agent": "claude-native-ui", "text": text, "response_id": "resp-test"},
        }
    return {
        "type": "external_conversation_item",
        "data": {
            "item_type": "message",
            "response_id": "resp-test",
            "item_data": {
                "role": "assistant",
                "agent": "claude-native-ui",
                "content": [{"type": "output_text", "text": text}],
            },
        },
    }


async def _assistant_texts(client: httpx.AsyncClient, session_id: str) -> list[str]:
    resp = await client.get(f"/v1/sessions/{session_id}/items", params={"limit": 100})
    resp.raise_for_status()
    return [
        block.get("text", "")
        for item in resp.json()["data"]
        if item.get("type") == "message" and item.get("role") == "assistant"
        for block in item.get("content", [])
    ]


@pytest.mark.asyncio
async def test_external_output_requires_a_resolvable_agent(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    db_uri: str,
) -> None:
    agent = await create_test_agent(client)
    session_id = agent["_session_id"]
    assert SqlAlchemyAgentStore(db_uri).delete(agent["id"])
    published: list[tuple[str, dict[str, Any]]] = []
    _capture_published(monkeypatch, published)
    response = await client.post(
        f"/v1/sessions/{session_id}/events",
        json=_external_event("external_assistant_message", "unreviewed output"),
    )
    assert response.status_code == 409, response.text
    assert not published
    assert await _assistant_texts(client, session_id) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind", ["external_assistant_message", "external_conversation_item", "batch"]
)
async def test_external_output_is_checked_before_storage_and_streaming(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    agent = await create_test_agent(client)
    session_id = agent["_session_id"]
    _patch_default_policies(monkeypatch, f"{__name__}._review_output")
    published: list[tuple[str, dict[str, Any]]] = []
    _capture_published(monkeypatch, published)

    # Partial previews cannot be evaluated as a complete response.
    for chunk in ["review-", "marker"]:
        response = await client.post(
            f"/v1/sessions/{session_id}/events",
            json={
                "type": "external_output_text_delta",
                "data": {"delta": chunk},
            },
        )
        assert response.status_code == 202, response.text
    assert not published

    item_kind = "external_conversation_item" if kind == "batch" else kind
    payload: Any = (
        [
            _external_event(item_kind, "review-marker"),
            _external_event(item_kind, "ordinary answer"),
        ]
        if kind == "batch"
        else _external_event(item_kind, "review-marker")
    )
    response = await client.post(f"/v1/sessions/{session_id}/events", json=payload)
    assert response.status_code == 202, response.text
    snapshot = await client.get(f"/v1/sessions/{session_id}")
    assert snapshot.status_code == 200
    assert "review-marker" not in snapshot.text
    assert "Output requires review" in snapshot.text
    assert "review-marker" not in json.dumps(published)
    assert "Output requires review" in json.dumps(published)
    if kind == "batch":
        assert "ordinary answer" in snapshot.text
        assert "ordinary answer" in json.dumps(published)


@pytest.mark.asyncio
async def test_session_policy_from_the_store_gates_external_uploads(
    policy_client: httpx.AsyncClient,
) -> None:
    """A CEL deny policy attached through the session policies route, as the web UI does."""
    agent = await create_test_agent(policy_client, name="external-upload-gate")
    session_id = await create_session_for_agent(policy_client, agent["id"])
    resp = await policy_client.post(
        f"/v1/sessions/{session_id}/policies",
        json={
            "name": "deny-tripwire-response",
            "type": "python",
            "handler": "omnigent.policies.builtins.cel.cel_policy",
            "factory_params": {"expression": _CEL_EXPRESSION, "reason": _DENY_REASON},
        },
    )
    assert resp.status_code == 200, resp.text

    batch = [
        _external_event("external_conversation_item", "TRIPWIRE-batch part 0"),
        _external_event("external_conversation_item", "clean reply"),
    ]
    resp = await policy_client.post(f"/v1/sessions/{session_id}/events", json=batch)
    assert resp.status_code == 202, resp.text
    resp = await policy_client.post(
        f"/v1/sessions/{session_id}/events",
        json=_external_event("external_assistant_message", "TRIPWIRE-assistant-message"),
    )
    assert resp.status_code == 202, resp.text
    assert await _assistant_texts(policy_client, session_id) == [
        _SENTINEL,
        "clean reply",
        _SENTINEL,
    ]


@pytest.mark.asyncio
async def test_malformed_preview_is_rejected_when_policies_apply(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = await create_test_agent(client)
    session_id = agent["_session_id"]
    _patch_default_policies(monkeypatch, f"{__name__}._review_output")
    published: list[tuple[str, dict[str, Any]]] = []
    _capture_published(monkeypatch, published)
    response = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={"type": "external_output_text_delta", "data": {"delta": {"text": "bad"}}},
    )
    assert response.status_code == 400, response.text
    assert "external_output_text_delta requires string data.delta" in response.text
    assert not published


@pytest.mark.asyncio
async def test_batch_keeps_reviewed_items_before_a_malformed_one(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = await create_test_agent(client)
    session_id = agent["_session_id"]
    _patch_default_policies(monkeypatch, f"{__name__}._review_output")
    published: list[tuple[str, dict[str, Any]]] = []
    _capture_published(monkeypatch, published)
    malformed = {
        "type": "external_conversation_item",
        "data": {"item_type": "message", "item_data": {}},
    }
    response = await client.post(
        f"/v1/sessions/{session_id}/events",
        json=[_external_event("external_conversation_item", "ordinary answer"), malformed],
    )
    assert response.status_code == 400, response.text
    assert await _assistant_texts(client, session_id) == ["ordinary answer"]
    assert "ordinary answer" in json.dumps(published)
