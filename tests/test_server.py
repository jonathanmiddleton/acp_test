"""Tests for the OpenAI-compatible HTTP server layer."""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from acp_proxy.client import AcpClient, ModelInfo
from acp_proxy.server import create_app


class FakeAcpClient:
    """A fake AcpClient for testing the HTTP server without a real subprocess."""

    def __init__(
        self,
        models: list[ModelInfo] | None = None,
        default_model: str = "gpt-4.1",
        prompt_response: str = "Hello from Copilot!",
        stop_reason: str = "end_turn",
    ) -> None:
        self._models = models or [
            ModelInfo(model_id="gpt-4.1", name="GPT-4.1"),
            ModelInfo(model_id="gpt-4o", name="GPT-4o"),
        ]
        self._default_model = default_model
        self._prompt_response = prompt_response
        self._stop_reason = stop_reason
        self._sessions: dict[str, str] = {}
        self._session_counter = 0
        self.last_prompt_messages: list[dict] | None = None

    @property
    def models(self) -> list[ModelInfo]:
        return list(self._models)

    @property
    def default_model(self) -> str | None:
        return self._default_model

    @property
    def agent_info(self) -> dict[str, str | None]:
        return {"name": "FakeAgent", "version": "0.0.1"}

    async def create_session(self, cwd: str, model_id: str | None = None) -> str:
        self._session_counter += 1
        sid = f"fake-session-{self._session_counter}"
        self._sessions[sid] = model_id or self._default_model
        return sid

    async def prompt(
        self, session_id: str, messages: list[dict[str, Any]]
    ) -> AsyncIterator[dict[str, Any]]:
        self.last_prompt_messages = messages

        # Simulate streaming chunks
        words = self._prompt_response.split(" ")
        for i, word in enumerate(words):
            text = word if i == 0 else " " + word
            yield {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": text},
            }
            await asyncio.sleep(0.01)

        yield {"done": True, "stopReason": self._stop_reason}


@pytest.fixture
def fake_client():
    return FakeAcpClient()


@pytest.fixture
def app(fake_client):
    return create_app(fake_client, "/tmp/test")


@pytest_asyncio.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_list_models(client, fake_client):
    """GET /v1/models returns the models from the ACP client."""
    resp = await client.get("/v1/models")
    assert resp.status_code == 200
    data = resp.json()
    assert data["object"] == "list"
    ids = [m["id"] for m in data["data"]]
    assert "gpt-4.1" in ids
    assert "gpt-4o" in ids


@pytest.mark.asyncio
async def test_chat_completion_non_streaming(client, fake_client):
    """POST /v1/chat/completions (non-streaming) returns a complete response."""
    resp = await client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4.1",
            "messages": [{"role": "user", "content": "Hello"}],
            "stream": False,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["object"] == "chat.completion"
    assert data["model"] == "gpt-4.1"
    assert len(data["choices"]) == 1
    assert data["choices"][0]["message"]["role"] == "assistant"
    assert data["choices"][0]["message"]["content"] == "Hello from Copilot!"
    assert data["choices"][0]["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_chat_completion_streaming(client, fake_client):
    """POST /v1/chat/completions (streaming) returns SSE chunks."""
    resp = await client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4.1",
            "messages": [{"role": "user", "content": "Hello"}],
            "stream": True,
        },
    )
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]

    # Parse SSE events
    body = resp.text
    events = []
    for line in body.split("\n"):
        if line.startswith("data: ") and line != "data: [DONE]":
            events.append(json.loads(line[6:]))

    # Should have content chunks + final chunk
    assert len(events) >= 2
    # First chunk should have content
    assert events[0]["object"] == "chat.completion.chunk"
    assert events[0]["choices"][0]["delta"].get("content") is not None
    # Last chunk should have finish_reason
    assert events[-1]["choices"][0]["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_chat_completion_unknown_model_returns_error(client, fake_client):
    """An unknown model ID returns a 404 error, not a silent fallback."""
    resp = await client.post(
        "/v1/chat/completions",
        json={
            "model": "nonexistent-model",
            "messages": [{"role": "user", "content": "test"}],
            "stream": False,
        },
    )
    assert resp.status_code == 404
    data = resp.json()
    assert data["error"]["code"] == "model_not_found"
    assert "nonexistent-model" in data["error"]["message"]


@pytest.mark.asyncio
async def test_chat_completion_passes_messages(client, fake_client):
    """Messages from the request are forwarded to the ACP client."""
    await client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4.1",
            "messages": [
                {"role": "system", "content": "You are helpful."},
                {"role": "user", "content": "What is 2+2?"},
            ],
            "stream": False,
        },
    )
    assert fake_client.last_prompt_messages is not None
    assert len(fake_client.last_prompt_messages) == 2
    assert fake_client.last_prompt_messages[0]["role"] == "system"
    assert fake_client.last_prompt_messages[1]["role"] == "user"


@pytest.mark.asyncio
async def test_health_endpoint(client, fake_client):
    """GET /health returns status and agent info."""
    resp = await client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["agent"]["name"] == "FakeAgent"


@pytest.mark.asyncio
async def test_extra_fields_accepted(client, fake_client):
    """Extra fields in the request (like stream_options) are accepted."""
    resp = await client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4.1",
            "messages": [{"role": "user", "content": "test"}],
            "stream": False,
            "stream_options": {"include_usage": True},
            "temperature": 0.7,
            "top_p": 0.9,
        },
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_session_reuse_per_conversation(client, fake_client):
    """Sessions are identified by (model, hash_of_first_user_message).

    Same model + same first user message = same session (multi-turn).
    Same model + different first user message = different session.
    Different model + same first user message = different session.
    """
    # Turn 1 of conversation A
    await client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4.1",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": False,
        },
    )
    assert fake_client._session_counter == 1

    # Turn 2 of conversation A — same first user message, reuses session
    await client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4.1",
            "messages": [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi there"},
                {"role": "user", "content": "follow up"},
            ],
            "stream": False,
        },
    )
    assert fake_client._session_counter == 1

    # Conversation B — different first user message, new session
    await client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4.1",
            "messages": [{"role": "user", "content": "different topic"}],
            "stream": False,
        },
    )
    assert fake_client._session_counter == 2

    # Conversation C — same first msg as A but different model, new session
    await client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": False,
        },
    )
    assert fake_client._session_counter == 3
