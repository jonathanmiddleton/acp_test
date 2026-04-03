"""
Integration test: runs the full proxy against the real copilot-language-server.

This test is skipped if the binary cannot be found (e.g., in CI).
Run it explicitly with: pytest tests/test_integration.py -v
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys

import pytest
import httpx

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from acp_proxy.client import AcpClient


def _find_binary() -> str | None:
    """Locate the copilot-language-server binary."""
    try:
        out = subprocess.check_output(
            ["ps", "-eo", "command"], text=True, stderr=subprocess.DEVNULL
        )
        for line in out.splitlines():
            if "copilot-language-server" in line and "grep" not in line:
                parts = line.split(" --")
                return parts[0].strip()
    except Exception:
        pass

    import glob

    home = os.path.expanduser("~")
    patterns = [
        os.path.join(
            home,
            "Library/Application Support/JetBrains/*/plugins/"
            "github-copilot-intellij/copilot-agent/native/darwin-arm64/"
            "copilot-language-server",
        ),
    ]
    for pattern in patterns:
        matches = glob.glob(pattern)
        if matches:
            matches.sort(key=os.path.getmtime, reverse=True)
            return matches[0]
    return None


BINARY = _find_binary()
SKIP_REASON = "copilot-language-server not found"


@pytest.mark.asyncio
@pytest.mark.skipif(BINARY is None, reason=SKIP_REASON)
async def test_acp_client_initialize_and_discover_models():
    """Start the ACP client and verify initialization + model discovery."""
    client = AcpClient(BINARY)
    try:
        await client.start()
        session_id = await client.create_session(os.getcwd())

        assert session_id is not None
        assert len(client.models) > 0
        assert client.default_model is not None
        assert client.agent_info["name"] is not None

        model_ids = [m.model_id for m in client.models]
        # Should have at least one model
        assert len(model_ids) >= 1
    finally:
        await client.stop()


@pytest.mark.asyncio
@pytest.mark.skipif(BINARY is None, reason=SKIP_REASON)
async def test_acp_client_prompt_and_stream():
    """Send a prompt and verify streaming response."""
    client = AcpClient(BINARY)
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
@pytest.mark.skipif(BINARY is None, reason=SKIP_REASON)
async def test_full_proxy_http_roundtrip():
    """Start the full proxy and make an HTTP request against it."""
    from acp_proxy.server import create_app

    client = AcpClient(BINARY)
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
@pytest.mark.skipif(BINARY is None, reason=SKIP_REASON)
async def test_model_switching():
    """Verify that session/set_model actually changes the active model."""
    client = AcpClient(BINARY)
    try:
        await client.start()
        session_id = await client.create_session(os.getcwd())

        # Pick a model different from the default
        default = client.default_model
        available = [m.model_id for m in client.models]
        other = next((m for m in available if m != default), None)
        if other is None:
            pytest.skip("Only one model available, cannot test switching")

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
