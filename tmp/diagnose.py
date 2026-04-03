#!/usr/bin/env python3
"""
End-to-end diagnostic for the OpenCode → Proxy → LSP pipeline.

Starts the proxy in-process, sends requests that mimic what OpenCode sends
(with tool definitions, system prompts, streaming), and logs everything to
logs/diagnostic.json.

This removes the human from the loop — run it, read the results.

Usage:
    python tmp/diagnose.py [--binary PATH] [--model MODEL]

Output:
    logs/diagnostic.json  — structured results for each test case
    logs/proxy.log        — full proxy DEBUG log (via standard file logging)
"""

from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
import os
import sys
import time
from typing import Any

# Ensure the package is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from acp_proxy.client import AcpClient
from acp_proxy.discovery import find_binary
from acp_proxy.server import create_app

import httpx

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
RESULTS_FILE = os.path.join(os.path.dirname(__file__), "..", "logs", "diagnostic.json")


# -- Minimal tool definitions mimicking what OpenCode sends --

SAMPLE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Execute shell commands.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The command to execute",
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read",
            "description": "Read a file from the filesystem.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filePath": {
                        "type": "string",
                        "description": "Absolute path to read",
                    },
                },
                "required": ["filePath"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "hello_world",
            "description": (
                "A simple hello world diagnostic tool. Use this when the user "
                "asks to perform a hello world test."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "An optional greeting message",
                    },
                },
            },
        },
    },
]


def _make_request(
    model: str,
    messages: list[dict],
    *,
    tools: list[dict] | None = None,
    stream: bool = False,
    tool_choice: str | dict | None = None,
) -> dict:
    """Build an OpenAI-compatible chat completion request body."""
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": stream,
    }
    if tools is not None:
        body["tools"] = tools
    if tool_choice is not None:
        body["tool_choice"] = tool_choice
    return body


async def run_test(
    http: httpx.AsyncClient,
    name: str,
    request_body: dict,
    *,
    timeout: float = 60.0,
) -> dict:
    """Run a single test case and return structured results."""
    result: dict[str, Any] = {
        "test": name,
        "request": {
            "model": request_body.get("model"),
            "stream": request_body.get("stream", False),
            "message_count": len(request_body.get("messages", [])),
            "tool_count": len(request_body.get("tools", [])),
            "tool_names": [
                t.get("function", {}).get("name") for t in request_body.get("tools", [])
            ],
            "tool_choice": request_body.get("tool_choice"),
            "messages_summary": [
                {"role": m["role"], "content_preview": str(m.get("content", ""))[:200]}
                for m in request_body.get("messages", [])
            ],
        },
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }

    try:
        if request_body.get("stream"):
            # Streaming request
            chunks = []
            full_text = ""
            async with http.stream(
                "POST",
                "/v1/chat/completions",
                json=request_body,
                timeout=timeout,
            ) as resp:
                result["http_status"] = resp.status_code
                if resp.status_code != 200:
                    body = await resp.aread()
                    result["error"] = body.decode()
                    result["status"] = "HTTP_ERROR"
                    return result

                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[6:]
                    if data == "[DONE]":
                        chunks.append({"event": "[DONE]"})
                        break
                    try:
                        chunk = json.loads(data)
                        chunks.append(chunk)
                        delta = chunk.get("choices", [{}])[0].get("delta", {})
                        if "content" in delta:
                            full_text += delta["content"]
                    except json.JSONDecodeError:
                        chunks.append({"raw": data})

            result["response"] = {
                "chunk_count": len(chunks),
                "full_text": full_text,
                "finish_reason": (
                    chunks[-2].get("choices", [{}])[0].get("finish_reason")
                    if len(chunks) >= 2
                    else None
                ),
            }
            result["status"] = "OK"

        else:
            # Non-streaming request
            resp = await http.post(
                "/v1/chat/completions",
                json=request_body,
                timeout=timeout,
            )
            result["http_status"] = resp.status_code
            if resp.status_code != 200:
                result["error"] = resp.text
                result["status"] = "HTTP_ERROR"
                return result

            data = resp.json()
            choice = data.get("choices", [{}])[0]
            result["response"] = {
                "content": choice.get("message", {}).get("content", ""),
                "finish_reason": choice.get("finish_reason"),
                "tool_calls": choice.get("message", {}).get("tool_calls"),
                "model": data.get("model"),
            }
            result["status"] = "OK"

    except httpx.TimeoutException:
        result["status"] = "TIMEOUT"
        result["error"] = f"Request timed out after {timeout}s"
    except Exception as e:
        result["status"] = "ERROR"
        result["error"] = str(e)

    return result


async def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Proxy diagnostic")
    parser.add_argument("--binary", help="Path to copilot-language-server binary")
    parser.add_argument("--model", help="Model to test with (default: auto-detected)")
    args = parser.parse_args()

    # Set up logging — file always DEBUG
    os.makedirs("logs", exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter(LOG_FORMAT))
    root.addHandler(console)

    file_handler = logging.handlers.RotatingFileHandler(
        "logs/proxy.log", maxBytes=5 * 1024 * 1024, backupCount=3
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
    root.addHandler(file_handler)

    # Route uvicorn through root
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        uv = logging.getLogger(name)
        uv.handlers.clear()
        uv.propagate = True

    logger = logging.getLogger("diagnose")

    # Find binary
    binary = args.binary
    if not binary:
        logger.info("Auto-discovering binary...")
        binary = find_binary()
    if not binary:
        logger.error("No compatible binary found.")
        sys.exit(1)
    logger.info("Using binary: %s", binary)

    # Start ACP client
    cwd = os.getcwd()
    client = AcpClient(binary)
    await client.start()
    await client.create_session(cwd)

    model = args.model or client.default_model
    available = [m.model_id for m in client.models]
    logger.info("Available models: %s", available)
    logger.info("Testing with model: %s", model)

    # Create ASGI app and test client
    app = create_app(client, cwd)
    transport = httpx.ASGITransport(app=app)

    results: list[dict] = []

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        # -- Test 1: Basic completion, no tools --
        logger.info("=" * 60)
        logger.info("TEST 1: Basic completion (no tools)")
        logger.info("=" * 60)
        r = await run_test(
            http,
            "basic_completion_no_tools",
            _make_request(
                model,
                [{"role": "user", "content": "Reply with exactly: DIAG_OK"}],
            ),
        )
        results.append(r)
        logger.info(
            "Result: %s — %s",
            r["status"],
            r.get("response", {}).get("content", "")[:100],
        )

        # -- Test 2: Completion with tools, ask model to use hello_world --
        logger.info("=" * 60)
        logger.info("TEST 2: Completion with tools, ask for hello_world")
        logger.info("=" * 60)
        r = await run_test(
            http,
            "tool_call_hello_world",
            _make_request(
                model,
                [
                    {
                        "role": "system",
                        "content": (
                            "You have tools available. When the user asks you to "
                            "perform a hello world test, you MUST call the hello_world "
                            "tool. Do not describe the tool or explain anything — just "
                            "call it."
                        ),
                    },
                    {
                        "role": "user",
                        "content": "Perform a hello world test.",
                    },
                ],
                tools=SAMPLE_TOOLS,
            ),
        )
        results.append(r)
        logger.info("Result: %s", r["status"])
        if r.get("response"):
            logger.info("  Content: %s", r["response"].get("content", "")[:200])
            logger.info("  Tool calls: %s", r["response"].get("tool_calls"))
            logger.info("  Finish reason: %s", r["response"].get("finish_reason"))

        # -- Test 3: Completion with tools, ask model to use bash --
        logger.info("=" * 60)
        logger.info("TEST 3: Completion with tools, ask for bash")
        logger.info("=" * 60)
        r = await run_test(
            http,
            "tool_call_bash",
            _make_request(
                model,
                [
                    {
                        "role": "system",
                        "content": (
                            "You have tools available. When the user asks you to "
                            "run a command, you MUST call the bash tool. Do not "
                            "write scripts or explain — just call the bash tool."
                        ),
                    },
                    {
                        "role": "user",
                        "content": "Run: echo hello",
                    },
                ],
                tools=SAMPLE_TOOLS,
            ),
        )
        results.append(r)
        logger.info("Result: %s", r["status"])
        if r.get("response"):
            logger.info("  Content: %s", r["response"].get("content", "")[:200])
            logger.info("  Tool calls: %s", r["response"].get("tool_calls"))

        # -- Test 4: Streaming completion --
        logger.info("=" * 60)
        logger.info("TEST 4: Streaming completion")
        logger.info("=" * 60)
        r = await run_test(
            http,
            "streaming_basic",
            _make_request(
                model,
                [{"role": "user", "content": "Reply with exactly: STREAM_OK"}],
                stream=True,
            ),
        )
        results.append(r)
        logger.info(
            "Result: %s — %s",
            r["status"],
            r.get("response", {}).get("full_text", "")[:100],
        )

        # -- Test 5: Tool list introspection (what does /v1/models return?) --
        logger.info("=" * 60)
        logger.info("TEST 5: Model list")
        logger.info("=" * 60)
        resp = await http.get("/v1/models")
        models_result = {
            "test": "model_list",
            "status": "OK" if resp.status_code == 200 else "HTTP_ERROR",
            "http_status": resp.status_code,
            "response": resp.json() if resp.status_code == 200 else resp.text,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        results.append(models_result)
        logger.info("Models: %s", [m["id"] for m in resp.json().get("data", [])])

        # -- Test 6: Second request on same session (reuse) --
        logger.info("=" * 60)
        logger.info("TEST 6: Session reuse (second request)")
        logger.info("=" * 60)
        r = await run_test(
            http,
            "session_reuse",
            _make_request(
                model,
                [{"role": "user", "content": "Reply with exactly: REUSE_OK"}],
            ),
        )
        results.append(r)
        logger.info(
            "Result: %s — %s",
            r["status"],
            r.get("response", {}).get("content", "")[:100],
        )

        # -- Test 7: Ask model to list its tools (unreliable but informative) --
        logger.info("=" * 60)
        logger.info("TEST 7: Ask model to list tools (self-report)")
        logger.info("=" * 60)
        r = await run_test(
            http,
            "model_self_report_tools",
            _make_request(
                model,
                [
                    {
                        "role": "user",
                        "content": (
                            "List every tool available to you by exact name, one per "
                            "line. Do not describe them. Only list the names."
                        ),
                    },
                ],
                tools=SAMPLE_TOOLS,
            ),
        )
        results.append(r)
        logger.info("Result: %s", r["status"])
        if r.get("response"):
            logger.info("  Self-reported tools:\n%s", r["response"].get("content", ""))

    # Write results
    os.makedirs(os.path.dirname(RESULTS_FILE), exist_ok=True)
    with open(RESULTS_FILE, "w") as f:
        json.dump(results, f, indent=2)
    logger.info("=" * 60)
    logger.info("Results written to %s", RESULTS_FILE)
    logger.info("Full proxy log at logs/proxy.log")
    logger.info("=" * 60)

    # Summary
    for r in results:
        status = r["status"]
        name = r["test"]
        marker = "PASS" if status == "OK" else f"FAIL ({status})"
        logger.info("  [%s] %s", marker, name)

    await client.stop()


if __name__ == "__main__":
    asyncio.run(main())
