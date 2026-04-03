"""
Integration tests: run the full proxy against the real copilot-language-server.

These tests assert that the environment has a compatible binary available.
If the binary is not found, the tests FAIL — not skip. A missing binary
means the environment is misconfigured, and skipping would mask that.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest
import httpx

from acp_proxy.client import AcpClient
from acp_proxy.discovery import find_binary


@pytest.fixture(scope="module")
def binary() -> str:
    """Resolve the compatible copilot-language-server binary.

    Fails the test session if no compatible binary is found. This is
    intentional — the environment must have the IntelliJ 2025.3 Copilot
    plugin installed with its bundled language server.
    """
    result = find_binary()
    assert result is not None, (
        "No compatible copilot-language-server binary found. "
        "The environment must have the IntelliJ IDEA 2025.3 Copilot plugin "
        "installed. Only the binary bundled with that plugin is supported."
    )
    assert os.path.isfile(result), f"Discovered binary path does not exist: {result}"
    assert os.access(result, os.X_OK), f"Discovered binary is not executable: {result}"
    return result


@pytest.mark.asyncio
async def test_acp_client_initialize_and_discover_models(binary: str):
    """Start the ACP client and verify initialization + model discovery."""
    client = AcpClient(binary)
    try:
        await client.start()
        session_id = await client.create_session(os.getcwd())

        assert session_id is not None
        assert len(client.models) > 0
        assert client.default_model is not None
        assert client.agent_info["name"] is not None

        model_ids = [m.model_id for m in client.models]
        assert len(model_ids) >= 1
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_acp_client_prompt_and_stream(binary: str):
    """Send a prompt and verify streaming response."""
    client = AcpClient(binary)
    try:
        await client.start()
        session_id = await client.create_session(os.getcwd())

        chunks: list[dict] = []
        async for update in client.prompt(
            session_id,
            [{"role": "user", "content": "Reply with exactly: PROXY_TEST_OK"}],
        ):
            chunks.append(update)

        # Should have at least one message chunk and a done sentinel
        assert len(chunks) >= 2
        done = chunks[-1]
        assert done.get("done") is True
        assert done.get("stopReason") == "end_turn"

        # Collect all text from message chunks
        text = ""
        for c in chunks:
            if c.get("sessionUpdate") == "agent_message_chunk":
                content = c.get("content", {})
                if content.get("type") == "text":
                    text += content.get("text", "")

        assert "PROXY_TEST_OK" in text
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_full_proxy_http_roundtrip(binary: str):
    """Start the full proxy and make an HTTP request against it."""
    from acp_proxy.server import create_app

    client = AcpClient(binary)
    try:
        await client.start()
        await client.create_session(os.getcwd())

        app = create_app(client, os.getcwd())

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as http:
            # Test /v1/models
            resp = await http.get("/v1/models")
            assert resp.status_code == 200
            models = resp.json()
            assert len(models["data"]) > 0

            # Test non-streaming completion
            resp = await http.post(
                "/v1/chat/completions",
                json={
                    "model": client.default_model,
                    "messages": [
                        {"role": "user", "content": "Reply with exactly: HTTP_OK"}
                    ],
                    "stream": False,
                },
                timeout=30.0,
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["choices"][0]["finish_reason"] == "stop"
            assert "HTTP_OK" in data["choices"][0]["message"]["content"]
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_model_switching(binary: str):
    """Verify that session/set_model actually changes the active model."""
    client = AcpClient(binary)
    try:
        await client.start()
        session_id = await client.create_session(os.getcwd())

        # Pick a model different from the default
        default = client.default_model
        available = [m.model_id for m in client.models]

        # If there's only one model, that's an environment problem — not
        # something to skip over. Model switching is a required capability.
        assert len(available) >= 2, (
            f"Only one model available ({available}). "
            "Model switching requires at least two models. "
            "Check that the Copilot subscription has access to multiple models."
        )

        other = next(m for m in available if m != default)

        # Switch model
        await client.set_model(session_id, other)

        # Verify by asking the model to identify itself
        text = ""
        async for update in client.prompt(
            session_id,
            [
                {
                    "role": "user",
                    "content": "What model are you? Reply with just your model name, nothing else.",
                }
            ],
        ):
            if update.get("sessionUpdate") == "agent_message_chunk":
                content = update.get("content", {})
                if content.get("type") == "text":
                    text += content.get("text", "")

        # The response should reference the new model, not the default
        # (Loose check — model self-identification varies)
        assert text.strip(), "Model returned empty response"
        assert default not in text or other in text, (
            f"Expected model {other} but got response mentioning {default}: {text}"
        )
    finally:
        await client.stop()
