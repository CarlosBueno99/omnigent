"""Consuming a delivered one-shot launch arg must not erase a newer stored list."""

from __future__ import annotations

import httpx
import pytest

from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio

_PROMPT = "review the last commit"


async def _create_native_session(client: httpx.AsyncClient, launch_args: list[str]) -> str:
    """Create a session carrying stored launch args, as ``omni claude -p`` does."""
    agent = await create_test_agent(client)
    response = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "initial_items": [], "terminal_launch_args": launch_args},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def test_consume_removes_only_the_matching_leading_arg(client: httpx.AsyncClient) -> None:
    """The prompt goes, the flags stay, and a second consume is a no-op."""
    session_id = await _create_native_session(client, [_PROMPT, "--verbose"])
    consume_path = f"/v1/sessions/{session_id}/terminal-launch-args/consume"

    response = await client.post(consume_path, json={"expected_leading_arg": _PROMPT})

    assert response.status_code == 200, response.text
    assert response.json() == {"consumed": True}
    snapshot = await client.get(f"/v1/sessions/{session_id}")
    assert snapshot.json()["terminal_launch_args"] == ["--verbose"]

    again = await client.post(consume_path, json={"expected_leading_arg": _PROMPT})

    assert again.json() == {"consumed": False}
    snapshot = await client.get(f"/v1/sessions/{session_id}")
    assert snapshot.json()["terminal_launch_args"] == ["--verbose"]


async def test_consume_preserves_a_newer_stored_prompt(client: httpx.AsyncClient) -> None:
    """A prompt another client stored meanwhile stays pending."""
    session_id = await _create_native_session(client, [_PROMPT])
    replaced = await client.patch(
        f"/v1/sessions/{session_id}", json={"terminal_launch_args": ["new request"]}
    )
    assert replaced.status_code == 200, replaced.text

    response = await client.post(
        f"/v1/sessions/{session_id}/terminal-launch-args/consume",
        json={"expected_leading_arg": _PROMPT},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"consumed": False}
    snapshot = await client.get(f"/v1/sessions/{session_id}")
    assert snapshot.json()["terminal_launch_args"] == ["new request"]


async def test_consume_unknown_session_is_not_found(client: httpx.AsyncClient) -> None:
    """An unknown session is a 404, not a silent no-op."""
    response = await client.post(
        f"/v1/sessions/{'f' * 32}/terminal-launch-args/consume",
        json={"expected_leading_arg": _PROMPT},
    )

    assert response.status_code == 404, response.text
