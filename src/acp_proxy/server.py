"""
OpenAI-compatible HTTP server.

Exposes /v1/chat/completions and /v1/models endpoints, translating
requests to ACP calls via the AcpClient.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from .client import AcpClient

logger = logging.getLogger(__name__)


# --- Request/Response models (OpenAI-compatible subset) ---


class ChatMessage(BaseModel):
    role: str
    content: str | list[dict[str, Any]] | None = None


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None
    # Accept and ignore these — OpenCode sends them
    stream_options: dict[str, Any] | None = None
    top_p: float | None = None
    frequency_penalty: float | None = None
    presence_penalty: float | None = None
    stop: list[str] | str | None = None
    n: int | None = None

    model_config = {"extra": "allow"}


class ChatCompletionChoice(BaseModel):
    index: int = 0
    message: ChatMessage
    finish_reason: str | None = "stop"


class UsageInfo(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: list[ChatCompletionChoice]
    usage: UsageInfo = Field(default_factory=UsageInfo)


# --- Server ---


def create_app(acp_client: AcpClient, cwd: str) -> FastAPI:
    """Create the FastAPI app wired to the given ACP client."""

    app = FastAPI(title="ACP-to-OpenAI Proxy")

    # Track session per model to reuse sessions where possible.
    # Key: model_id, Value: session_id
    _model_sessions: dict[str, str] = {}

    async def _get_session(model_id: str) -> str:
        """Get or create a session for the given model."""
        if model_id not in _model_sessions:
            session_id = await acp_client.create_session(cwd, model_id=model_id)
            _model_sessions[model_id] = session_id
        return _model_sessions[model_id]

    @app.get("/v1/models")
    async def list_models() -> JSONResponse:
        """List available models (OpenAI-compatible)."""
        models = acp_client.models
        data = []
        for m in models:
            data.append(
                {
                    "id": m.model_id,
                    "object": "model",
                    "created": 0,
                    "owned_by": "copilot",
                }
            )
        return JSONResponse({"object": "list", "data": data})

    @app.post("/v1/chat/completions")
    async def chat_completions(request: ChatCompletionRequest) -> Any:
        """Handle chat completion requests."""
        model_id = request.model
        messages = [m.model_dump() for m in request.messages]

        available_ids = {m.model_id for m in acp_client.models}
        if model_id not in available_ids:
            logger.debug(
                "Model %s not in available set %s",
                model_id,
                available_ids,
            )
            return JSONResponse(
                status_code=404,
                content={
                    "error": {
                        "message": (
                            f"Model '{model_id}' is not available. "
                            f"Available models: {sorted(available_ids)}"
                        ),
                        "type": "invalid_request_error",
                        "code": "model_not_found",
                    }
                },
            )

        session_id = await _get_session(model_id)
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        created = int(time.time())

        if request.stream:
            return StreamingResponse(
                _stream_response(
                    acp_client, session_id, messages, completion_id, created, model_id
                ),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )
        else:
            return await _non_streaming_response(
                acp_client, session_id, messages, completion_id, created, model_id
            )

    @app.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse({"status": "ok", "agent": acp_client.agent_info})

    return app


async def _non_streaming_response(
    client: AcpClient,
    session_id: str,
    messages: list[dict[str, Any]],
    completion_id: str,
    created: int,
    model_id: str,
) -> JSONResponse:
    """Collect the full response and return it."""
    full_text = ""
    stop_reason = "stop"

    async for update in client.prompt(session_id, messages):
        if update.get("done"):
            sr = update.get("stopReason", "end_turn")
            stop_reason = _map_stop_reason(sr)
            break
        kind = update.get("sessionUpdate", "")
        if kind == "agent_message_chunk":
            content = update.get("content", {})
            if content.get("type") == "text":
                full_text += content.get("text", "")

    response = ChatCompletionResponse(
        id=completion_id,
        created=created,
        model=model_id,
        choices=[
            ChatCompletionChoice(
                message=ChatMessage(role="assistant", content=full_text),
                finish_reason=stop_reason,
            )
        ],
        usage=UsageInfo(
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
        ),
    )
    return JSONResponse(response.model_dump())


async def _stream_response(
    client: AcpClient,
    session_id: str,
    messages: list[dict[str, Any]],
    completion_id: str,
    created: int,
    model_id: str,
) -> AsyncIterator[str]:
    """Stream SSE events in OpenAI format."""

    async for update in client.prompt(session_id, messages):
        if update.get("done"):
            sr = update.get("stopReason", "end_turn")
            finish_reason = _map_stop_reason(sr)
            # Send final chunk with finish_reason
            chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model_id,
                "choices": [
                    {
                        "index": 0,
                        "delta": {},
                        "finish_reason": finish_reason,
                    }
                ],
            }
            yield f"data: {json.dumps(chunk)}\n\n"
            yield "data: [DONE]\n\n"
            break

        kind = update.get("sessionUpdate", "")
        if kind == "agent_message_chunk":
            content = update.get("content", {})
            if content.get("type") == "text":
                text = content.get("text", "")
                if text:
                    chunk = {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model_id,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"role": "assistant", "content": text},
                                "finish_reason": None,
                            }
                        ],
                    }
                    yield f"data: {json.dumps(chunk)}\n\n"


def _map_stop_reason(acp_reason: str) -> str:
    """Map ACP stop reasons to OpenAI finish reasons."""
    mapping = {
        "end_turn": "stop",
        "max_tokens": "length",
        "cancelled": "stop",
        "refusal": "stop",
        "max_turn_requests": "stop",
    }
    return mapping.get(acp_reason, "stop")
