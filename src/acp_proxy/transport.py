"""
NDJSON transport layer for ACP communication.

Manages the copilot-language-server subprocess and provides async
send/receive of JSON-RPC messages over stdin/stdout.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)


class AcpTransport:
    """Async NDJSON transport over a subprocess's stdin/stdout.

    Handles:
    - Launching the copilot-language-server process
    - Sending JSON-RPC requests/notifications
    - Dispatching incoming messages to registered handlers
    - Request/response correlation via pending futures
    """

    def __init__(self) -> None:
        self._process: asyncio.subprocess.Process | None = None
        self._next_id: int = 1
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._notification_handler: Callable[[dict[str, Any]], None] | None = None
        self._request_handler: (
            Callable[[dict[str, Any]], asyncio.Future[dict[str, Any]] | None] | None
        ) = None
        self._reader_task: asyncio.Task[None] | None = None
        self._closed = False

    async def start(self, binary_path: str) -> None:
        """Launch the language server subprocess in ACP mode."""
        logger.info("Starting copilot-language-server: %s", binary_path)
        self._process = await asyncio.create_subprocess_exec(
            binary_path,
            "--acp",
            "--stdio",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._reader_task = asyncio.create_task(self._read_loop())
        # Drain stderr to avoid blocking
        asyncio.create_task(self._drain_stderr())

    async def stop(self) -> None:
        """Terminate the subprocess and clean up."""
        self._closed = True
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        if self._process:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self._process.kill()
                await self._process.wait()
        # Reject any pending futures
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError("Transport closed"))
        self._pending.clear()

    def on_notification(self, handler: Callable[[dict[str, Any]], None]) -> None:
        """Register a handler for incoming notifications (no 'id' field)."""
        self._notification_handler = handler

    def on_request(
        self,
        handler: Callable[[dict[str, Any]], asyncio.Future[dict[str, Any]] | None],
    ) -> None:
        """Register a handler for incoming requests (has 'id' and 'method').

        The handler receives the full JSON-RPC request and should return
        a Future that resolves to the response result, or None to reject.
        """
        self._request_handler = handler

    async def send_request(
        self, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Send a JSON-RPC request and wait for the response."""
        req_id = self._next_id
        self._next_id += 1

        msg: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
        }
        if params is not None:
            msg["params"] = params

        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_event_loop().create_future()
        )
        self._pending[req_id] = future

        await self._write(msg)
        return await future

    async def send_notification(
        self, method: str, params: dict[str, Any] | None = None
    ) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        msg: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
        }
        if params is not None:
            msg["params"] = params
        await self._write(msg)

    async def send_response(
        self, req_id: int | str, result: Any = None, error: dict[str, Any] | None = None
    ) -> None:
        """Send a JSON-RPC response to an incoming request."""
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id}
        if error is not None:
            msg["error"] = error
        else:
            msg["result"] = result
        await self._write(msg)

    async def _write(self, msg: dict[str, Any]) -> None:
        """Write a single NDJSON line to stdin."""
        assert self._process and self._process.stdin
        line = json.dumps(msg, separators=(",", ":")) + "\n"
        self._process.stdin.write(line.encode())
        await self._process.stdin.drain()
        logger.debug(">>> %s", line.rstrip())

    async def _read_loop(self) -> None:
        """Read NDJSON lines from stdout and dispatch."""
        assert self._process and self._process.stdout
        while not self._closed:
            line_bytes = await self._process.stdout.readline()
            if not line_bytes:
                logger.info("Subprocess stdout closed")
                break
            line = line_bytes.decode().strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("Non-JSON line from subprocess: %s", line[:200])
                continue

            logger.debug("<<< %s", line[:500])
            self._dispatch(msg)

    def _dispatch(self, msg: dict[str, Any]) -> None:
        """Route an incoming message to the appropriate handler."""
        if "id" in msg and "method" in msg:
            # Incoming request from agent (e.g., fs/read_text_file, session/request_permission)
            if self._request_handler:
                asyncio.create_task(self._handle_incoming_request(msg))
            else:
                logger.warning("No request handler for: %s", msg.get("method"))
        elif "id" in msg:
            # Response to one of our requests
            req_id = msg["id"]
            future = self._pending.pop(req_id, None)
            if future and not future.done():
                if "error" in msg:
                    future.set_exception(
                        AcpError(
                            msg["error"].get("message", "Unknown error"), msg["error"]
                        )
                    )
                else:
                    future.set_result(msg.get("result", {}))
            else:
                logger.warning("Unexpected response id=%s", req_id)
        else:
            # Notification
            if self._notification_handler:
                self._notification_handler(msg)

    async def _handle_incoming_request(self, msg: dict[str, Any]) -> None:
        """Handle an incoming request from the agent."""
        assert self._request_handler
        try:
            result = self._request_handler(msg)
            if asyncio.isfuture(result) or asyncio.iscoroutine(result):
                result = await result
            await self.send_response(msg["id"], result=result)
        except Exception:
            logger.exception("Error handling request %s", msg.get("method"))
            await self.send_response(
                msg["id"],
                error={"code": -32603, "message": "Internal error"},
            )

    async def _drain_stderr(self) -> None:
        """Read and log stderr to prevent buffer blocking."""
        assert self._process and self._process.stderr
        while not self._closed:
            line = await self._process.stderr.readline()
            if not line:
                break
            text = line.decode().strip()
            if text:
                logger.debug("[stderr] %s", text)


class AcpError(Exception):
    """Error returned by the ACP agent."""

    def __init__(self, message: str, error_obj: dict[str, Any] | None = None):
        super().__init__(message)
        self.error_obj = error_obj or {}
