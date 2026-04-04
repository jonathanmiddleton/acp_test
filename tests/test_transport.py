"""Tests for the NDJSON transport layer."""

from __future__ import annotations

import asyncio
import json

import pytest

from acp_proxy.transport import AcpTransport, AcpError


@pytest.fixture
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


class FakeProcess:
    """Simulates an asyncio subprocess for transport testing."""

    def __init__(self) -> None:
        self.stdin = FakeStdin()
        self.stdout = FakeStdout()
        self.stderr = FakeStdout()
        self._returncode: int | None = None

    def terminate(self) -> None:
        self._returncode = 0
        self.stdout.close()
        self.stderr.close()

    def kill(self) -> None:
        self._returncode = -9
        self.stdout.close()
        self.stderr.close()

    async def wait(self) -> int:
        return self._returncode or 0


class FakeStdin:
    def __init__(self) -> None:
        self.written: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.written.append(data)

    async def drain(self) -> None:
        pass


class FakeStdout:
    def __init__(self) -> None:
        self._lines: asyncio.Queue[bytes] = asyncio.Queue()
        self._closed = False

    def feed(self, line: str) -> None:
        self._lines.put_nowait((line + "\n").encode())

    def close(self) -> None:
        self._closed = True
        self._lines.put_nowait(b"")

    async def readline(self) -> bytes:
        return await self._lines.get()


def make_transport_with_fake(fake: FakeProcess) -> AcpTransport:
    """Create a transport wired to a FakeProcess (bypass subprocess spawn)."""
    transport = AcpTransport()
    transport._process = fake  # type: ignore[assignment]
    transport._reader_task = asyncio.create_task(transport._read_loop())
    asyncio.create_task(transport._drain_stderr())
    return transport


@pytest.mark.asyncio
async def test_send_request_receives_response():
    """Sending a request and receiving a matching response resolves the future."""
    fake = FakeProcess()
    transport = make_transport_with_fake(fake)

    # Start a request in background
    task = asyncio.create_task(transport.send_request("test/method", {"key": "value"}))

    # Let the send happen
    await asyncio.sleep(0.05)

    # Verify the request was written
    assert len(fake.stdin.written) == 1
    sent = json.loads(fake.stdin.written[0].decode())
    assert sent["method"] == "test/method"
    assert sent["params"] == {"key": "value"}
    req_id = sent["id"]

    # Simulate response from server
    response = json.dumps({"jsonrpc": "2.0", "id": req_id, "result": {"ok": True}})
    fake.stdout.feed(response)

    result = await asyncio.wait_for(task, timeout=2.0)
    assert result == {"ok": True}

    fake.stdout.close()
    fake.stderr.close()


@pytest.mark.asyncio
async def test_send_request_error_raises():
    """An error response raises AcpError."""
    fake = FakeProcess()
    transport = make_transport_with_fake(fake)

    task = asyncio.create_task(transport.send_request("bad/method"))
    await asyncio.sleep(0.05)

    sent = json.loads(fake.stdin.written[0].decode())
    error_resp = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": sent["id"],
            "error": {"code": -32600, "message": "Invalid request"},
        }
    )
    fake.stdout.feed(error_resp)

    with pytest.raises(AcpError, match="Invalid request"):
        await asyncio.wait_for(task, timeout=2.0)

    fake.stdout.close()
    fake.stderr.close()


@pytest.mark.asyncio
async def test_notification_dispatch():
    """Incoming notifications are dispatched to the registered handler."""
    fake = FakeProcess()
    transport = make_transport_with_fake(fake)

    received: list[dict] = []
    transport.on_notification(lambda msg: received.append(msg))

    fake.stdout.feed(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": "s1",
                    "update": {"sessionUpdate": "agent_message_chunk"},
                },
            }
        )
    )

    await asyncio.sleep(0.1)
    assert len(received) == 1
    assert received[0]["method"] == "session/update"

    fake.stdout.close()
    fake.stderr.close()


@pytest.mark.asyncio
async def test_incoming_request_dispatch():
    """Incoming requests from the agent are dispatched and responded to."""
    fake = FakeProcess()
    transport = make_transport_with_fake(fake)

    def handle_request(msg):
        if msg["method"] == "session/request_permission":
            return {"outcome": {"outcome": "cancelled"}}
        return None

    transport.on_request(handle_request)

    fake.stdout.feed(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 99,
                "method": "session/request_permission",
                "params": {"sessionId": "s1", "options": []},
            }
        )
    )

    await asyncio.sleep(0.15)

    # Should have written a response back
    assert len(fake.stdin.written) >= 1
    resp = json.loads(fake.stdin.written[-1].decode())
    assert resp["id"] == 99
    assert resp["result"]["outcome"]["outcome"] == "cancelled"

    fake.stdout.close()
    fake.stderr.close()


@pytest.mark.asyncio
async def test_request_ids_increment():
    """Each request gets a unique incrementing ID."""
    fake = FakeProcess()
    transport = make_transport_with_fake(fake)

    # Fire off two requests without resolving them
    t1 = asyncio.create_task(transport.send_request("m1"))
    t2 = asyncio.create_task(transport.send_request("m2"))
    await asyncio.sleep(0.05)

    ids = [json.loads(w.decode())["id"] for w in fake.stdin.written]
    assert ids[0] < ids[1]

    # Clean up
    t1.cancel()
    t2.cancel()
    fake.stdout.close()
    fake.stderr.close()


@pytest.mark.asyncio
async def test_stop_rejects_pending():
    """Stopping the transport rejects all pending request futures."""
    fake = FakeProcess()
    transport = make_transport_with_fake(fake)

    task = asyncio.create_task(transport.send_request("slow/method"))
    await asyncio.sleep(0.05)

    await transport.stop()

    with pytest.raises(ConnectionError):
        await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_non_json_line_skipped():
    """Non-JSON output from the subprocess is logged and skipped, not fatal."""
    fake = FakeProcess()
    transport = make_transport_with_fake(fake)

    # Send a non-JSON line followed by a valid response
    task = asyncio.create_task(transport.send_request("test/method"))
    await asyncio.sleep(0.05)

    sent = json.loads(fake.stdin.written[0].decode())
    req_id = sent["id"]

    # Garbage line — should be skipped
    fake.stdout.feed("this is not json {{{")
    # Valid response — should still be processed
    fake.stdout.feed(
        json.dumps({"jsonrpc": "2.0", "id": req_id, "result": {"recovered": True}})
    )

    result = await asyncio.wait_for(task, timeout=2.0)
    assert result == {"recovered": True}

    fake.stdout.close()
    fake.stderr.close()


@pytest.mark.asyncio
async def test_unexpected_response_id_ignored():
    """A response with an unknown ID is logged and ignored."""
    fake = FakeProcess()
    transport = make_transport_with_fake(fake)

    # Send a response for an ID that was never requested
    fake.stdout.feed(
        json.dumps({"jsonrpc": "2.0", "id": 99999, "result": {"orphan": True}})
    )

    # Give the read loop time to process
    await asyncio.sleep(0.1)

    # Transport should still be functional
    task = asyncio.create_task(transport.send_request("test/method"))
    await asyncio.sleep(0.05)

    sent = json.loads(fake.stdin.written[0].decode())
    fake.stdout.feed(
        json.dumps({"jsonrpc": "2.0", "id": sent["id"], "result": {"ok": True}})
    )

    result = await asyncio.wait_for(task, timeout=2.0)
    assert result == {"ok": True}

    fake.stdout.close()
    fake.stderr.close()


@pytest.mark.asyncio
async def test_handler_exception_returns_error_response():
    """If a request handler raises, an error response is sent back."""
    fake = FakeProcess()
    transport = make_transport_with_fake(fake)

    def exploding_handler(msg):
        raise ValueError("handler blew up")

    transport.on_request(exploding_handler)

    fake.stdout.feed(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 42,
                "method": "some/request",
                "params": {},
            }
        )
    )

    await asyncio.sleep(0.15)

    # Should have sent an error response back
    assert len(fake.stdin.written) >= 1
    resp = json.loads(fake.stdin.written[-1].decode())
    assert resp["id"] == 42
    assert "error" in resp
    assert resp["error"]["code"] == -32603
    assert resp["error"]["message"] == "Internal error"

    fake.stdout.close()
    fake.stderr.close()


@pytest.mark.asyncio
async def test_send_notification_no_id():
    """send_notification sends a message without an 'id' field."""
    fake = FakeProcess()
    transport = make_transport_with_fake(fake)

    await transport.send_notification("test/notify", {"data": "value"})

    assert len(fake.stdin.written) == 1
    sent = json.loads(fake.stdin.written[0].decode())
    assert "id" not in sent
    assert sent["method"] == "test/notify"
    assert sent["params"] == {"data": "value"}

    fake.stdout.close()
    fake.stderr.close()
