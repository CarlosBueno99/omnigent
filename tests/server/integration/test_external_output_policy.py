"""Completed native messages must pass policy before durable or live publication."""

import json
from typing import Any

import httpx
import pytest

from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from tests.server.helpers import create_test_agent
from tests.server.integration.test_sessions_endpoints import _capture_published
from tests.server.integration.test_sessions_policy_evaluate import _patch_default_policies


def _review_output(event: dict[str, Any]) -> dict[str, str]:
    if event.get("type") == "response" and "review-marker" in str(event.get("data")):
        return {"result": "DENY", "reason": "Output requires review"}
    return {"result": "ALLOW"}


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
        json={
            "type": "external_assistant_message",
            "data": {
                "agent": "claude-native-ui",
                "text": "unreviewed output",
                "response_id": "resp-test",
            },
        },
    )
    assert response.status_code == 409, response.text
    assert not published


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

    def event(text: str) -> dict[str, Any]:
        if kind == "external_assistant_message":
            return {
                "type": kind,
                "data": {
                    "agent": "claude-native-ui",
                    "text": text,
                    "response_id": "resp-test",
                },
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

    payload: Any = (
        [event("review-marker"), event("ordinary answer")]
        if kind == "batch"
        else event("review-marker")
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
