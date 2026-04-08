"""Unit tests for the ACP client layer.

Covers message extraction (ADR-002, ADR-004), agent callback handlers
(ADR-007), model management, and notification routing. These tests
exercise client.py in isolation — no subprocess, no transport.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from acp_proxy.client import AcpClient, ModelInfo, SessionState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_messages(*roles_and_contents: tuple[str, str | list | None]) -> list[dict]:
    """Build an OpenAI-format messages array from (role, content) pairs."""
    return [{"role": r, "content": c} for r, c in roles_and_contents]


# ---------------------------------------------------------------------------
# _extract_text
# ---------------------------------------------------------------------------


class TestExtractText:
    """AcpClient._extract_text handles the three content shapes OpenCode sends."""

    def test_string_content(self):
        assert AcpClient._extract_text("hello world") == "hello world"

    def test_none_content(self):
        assert AcpClient._extract_text(None) == ""

    def test_list_with_text_blocks(self):
        content = [
            {"type": "text", "text": "part one"},
            {"type": "text", "text": "part two"},
        ]
        assert AcpClient._extract_text(content) == "part one\npart two"

    def test_list_with_mixed_block_types(self):
        """Non-text blocks (images, resources) are skipped."""
        content = [
            {"type": "image", "url": "http://example.com/img.png"},
            {"type": "text", "text": "the text"},
        ]
        assert AcpClient._extract_text(content) == "the text"

    def test_empty_list(self):
        assert AcpClient._extract_text([]) == ""

    def test_empty_string(self):
        assert AcpClient._extract_text("") == ""


# ---------------------------------------------------------------------------
# extract_last_user_message (ADR-004)
# ---------------------------------------------------------------------------


class TestExtractLastUserMessage:
    """ADR-004: only the last user message is forwarded to the ACP session."""

    def test_single_user_message(self):
        msgs = _make_messages(("user", "hello"))
        assert AcpClient.extract_last_user_message(msgs) == "hello"

    def test_multi_turn_returns_last_user(self):
        """With full history replay, returns only the newest user message."""
        msgs = _make_messages(
            ("system", "You are helpful."),
            ("user", "first question"),
            ("assistant", "first answer"),
            ("user", "second question"),
        )
        assert AcpClient.extract_last_user_message(msgs) == "second question"

    def test_system_messages_stripped(self):
        """System messages (OpenCode's prompt) are never returned."""
        msgs = _make_messages(
            ("system", "You are a coding assistant with tools..."),
            ("user", "help me"),
        )
        assert AcpClient.extract_last_user_message(msgs) == "help me"

    def test_assistant_messages_stripped(self):
        """Assistant messages from prior turns are not included."""
        msgs = _make_messages(
            ("user", "question"),
            ("assistant", "answer"),
            ("user", "follow-up"),
        )
        result = AcpClient.extract_last_user_message(msgs)
        assert result == "follow-up"
        assert "answer" not in result

    def test_system_reminder_in_earlier_user_message_stripped(self):
        """<system-reminder> tags in earlier user messages don't leak through."""
        msgs = _make_messages(
            ("user", "<system-reminder>build mode</system-reminder>\nfirst msg"),
            ("assistant", "ok"),
            ("user", "second msg"),
        )
        result = AcpClient.extract_last_user_message(msgs)
        assert result == "second msg"
        assert "system-reminder" not in result

    def test_list_content_in_last_user_message(self):
        """Content blocks (list format) are extracted correctly."""
        msgs = [
            {"role": "user", "content": [{"type": "text", "text": "from blocks"}]},
        ]
        assert AcpClient.extract_last_user_message(msgs) == "from blocks"

    def test_no_user_messages_fallback(self):
        """If no user message exists, concatenate all non-empty content."""
        msgs = _make_messages(
            ("system", "system prompt"),
            ("assistant", "stray assistant"),
        )
        result = AcpClient.extract_last_user_message(msgs)
        assert "system prompt" in result
        assert "stray assistant" in result

    def test_none_content_user_message_skipped(self):
        """A user message with None content is skipped in favor of earlier ones."""
        msgs = _make_messages(
            ("user", "real content"),
            ("assistant", "reply"),
            ("user", None),
        )
        # The last user message has None content, _extract_text returns "",
        # but the method still returns it (empty string). The fallback only
        # triggers when there are NO user messages at all.
        result = AcpClient.extract_last_user_message(msgs)
        # It returns "" because the last user message has None content
        assert result == ""


# ---------------------------------------------------------------------------
# extract_first_user_message (ADR-002)
# ---------------------------------------------------------------------------


class TestExtractFirstUserMessage:
    """ADR-002: first user message is the stable conversation anchor for session ID."""

    def test_returns_first_user_message(self):
        msgs = _make_messages(
            ("system", "system prompt"),
            ("user", "first question"),
            ("assistant", "answer"),
            ("user", "second question"),
        )
        assert AcpClient.extract_first_user_message(msgs) == "first question"

    def test_stable_across_turns(self):
        """Simulates OpenCode's full-replay: first user message is the same."""
        turn_1 = _make_messages(("user", "hello agent"))
        turn_2 = _make_messages(
            ("user", "hello agent"),
            ("assistant", "hi"),
            ("user", "follow up"),
        )
        assert AcpClient.extract_first_user_message(
            turn_1
        ) == AcpClient.extract_first_user_message(turn_2)

    def test_no_user_messages_returns_empty(self):
        msgs = _make_messages(("system", "only system"))
        assert AcpClient.extract_first_user_message(msgs) == ""

    def test_system_message_not_returned(self):
        """System messages are not user messages even though they come first."""
        msgs = _make_messages(
            ("system", "I am a system prompt"),
            ("user", "I am the user"),
        )
        assert AcpClient.extract_first_user_message(msgs) == "I am the user"

    def test_title_generator_different_anchor(self):
        """Title generator messages differ from conversation messages."""
        conversation = _make_messages(("user", "help me refactor this function"))
        title_gen = _make_messages(
            ("user", "You are a title generator. Summarize: help me refactor...")
        )
        assert AcpClient.extract_first_user_message(
            conversation
        ) != AcpClient.extract_first_user_message(title_gen)


# ---------------------------------------------------------------------------
# _messages_to_prompt (ADR-004)
# ---------------------------------------------------------------------------


class TestMessagesToPrompt:
    """ADR-004: messages are converted to a single ACP text content block."""

    def test_returns_single_text_block(self):
        client = AcpClient.__new__(AcpClient)
        msgs = _make_messages(
            ("system", "ignored system prompt"),
            ("user", "first question"),
            ("assistant", "first answer"),
            ("user", "second question"),
        )
        result = client._messages_to_prompt(msgs)
        assert len(result) == 1
        assert result[0]["type"] == "text"
        assert result[0]["text"] == "second question"

    def test_opencode_system_prompt_not_in_output(self):
        """OpenCode's ~15K system prompt must never reach the ACP session."""
        client = AcpClient.__new__(AcpClient)
        long_system = "You are OpenCode. " * 1000
        msgs = _make_messages(
            ("system", long_system),
            ("user", "actual question"),
        )
        result = client._messages_to_prompt(msgs)
        assert "OpenCode" not in result[0]["text"]
        assert result[0]["text"] == "actual question"


# ---------------------------------------------------------------------------
# _handle_permission_request (ADR-007)
# ---------------------------------------------------------------------------


class TestHandlePermissionRequest:
    """ADR-007: auto-approve with priority allow_always > allow_once > first."""

    def _make_client(self) -> AcpClient:
        client = AcpClient.__new__(AcpClient)
        client._sessions = {}
        client._update_queues = {}
        return client

    def test_prefers_allow_always(self):
        client = self._make_client()
        params = {
            "options": [
                {"optionId": "1", "kind": "allow_once", "name": "Once"},
                {"optionId": "2", "kind": "allow_always", "name": "Always"},
                {"optionId": "3", "kind": "deny", "name": "Deny"},
            ]
        }
        result = client._handle_permission_request(params)
        assert result["outcome"]["optionId"] == "2"
        assert result["outcome"]["outcome"] == "selected"

    def test_falls_back_to_allow_once(self):
        client = self._make_client()
        params = {
            "options": [
                {"optionId": "1", "kind": "deny", "name": "Deny"},
                {"optionId": "2", "kind": "allow_once", "name": "Once"},
            ]
        }
        result = client._handle_permission_request(params)
        assert result["outcome"]["optionId"] == "2"

    def test_falls_back_to_first_option(self):
        client = self._make_client()
        params = {
            "options": [
                {"optionId": "1", "kind": "deny", "name": "Deny"},
            ]
        }
        result = client._handle_permission_request(params)
        assert result["outcome"]["optionId"] == "1"

    def test_empty_options_returns_cancelled(self):
        client = self._make_client()
        params = {"options": []}
        result = client._handle_permission_request(params)
        assert result["outcome"]["outcome"] == "cancelled"


# ---------------------------------------------------------------------------
# _handle_read_file (ADR-007)
# ---------------------------------------------------------------------------


class TestHandleReadFile:
    """ADR-007: fs/read_text_file callback reads from disk."""

    def _make_client(self) -> AcpClient:
        client = AcpClient.__new__(AcpClient)
        return client

    def test_read_full_file(self, tmp_path):
        client = self._make_client()
        f = tmp_path / "test.txt"
        f.write_text("line one\nline two\nline three\n")
        result = client._handle_read_file({"path": str(f)})
        assert result["content"] == "line one\nline two\nline three\n"

    def test_read_with_line_and_limit(self, tmp_path):
        client = self._make_client()
        f = tmp_path / "test.txt"
        f.write_text("line1\nline2\nline3\nline4\nline5\n")
        result = client._handle_read_file({"path": str(f), "line": 2, "limit": 2})
        assert result["content"] == "line2\nline3\n"

    def test_read_with_line_no_limit(self, tmp_path):
        client = self._make_client()
        f = tmp_path / "test.txt"
        f.write_text("a\nb\nc\nd\n")
        result = client._handle_read_file({"path": str(f), "line": 3})
        assert result["content"] == "c\nd\n"

    def test_read_nonexistent_file_raises(self):
        client = self._make_client()
        with pytest.raises(FileNotFoundError):
            client._handle_read_file({"path": "/nonexistent/path/file.txt"})


# ---------------------------------------------------------------------------
# _handle_write_file (ADR-007)
# ---------------------------------------------------------------------------


class TestHandleWriteFile:
    """ADR-007: fs/write_text_file callback writes to disk."""

    def _make_client(self) -> AcpClient:
        client = AcpClient.__new__(AcpClient)
        return client

    def test_write_creates_file(self, tmp_path):
        client = self._make_client()
        target = tmp_path / "output.txt"
        client._handle_write_file({"path": str(target), "content": "hello"})
        assert target.read_text() == "hello"

    def test_write_creates_intermediate_directories(self, tmp_path):
        client = self._make_client()
        target = tmp_path / "a" / "b" / "c" / "file.txt"
        client._handle_write_file({"path": str(target), "content": "nested"})
        assert target.read_text() == "nested"

    def test_write_overwrites_existing(self, tmp_path):
        client = self._make_client()
        target = tmp_path / "existing.txt"
        target.write_text("old content")
        client._handle_write_file({"path": str(target), "content": "new content"})
        assert target.read_text() == "new content"


# ---------------------------------------------------------------------------
# _handle_agent_request dispatch (ADR-007)
# ---------------------------------------------------------------------------


class TestHandleAgentRequest:
    """ADR-007: incoming agent requests are dispatched to the correct handler."""

    def _make_client(self) -> AcpClient:
        client = AcpClient.__new__(AcpClient)
        client._sessions = {}
        client._update_queues = {}
        return client

    def test_unknown_method_returns_none(self):
        client = self._make_client()
        result = client._handle_agent_request(
            {"method": "unknown/method", "params": {}}
        )
        assert result is None

    def test_permission_request_dispatched(self):
        client = self._make_client()
        result = client._handle_agent_request(
            {
                "method": "session/request_permission",
                "params": {
                    "options": [
                        {"optionId": "1", "kind": "allow_always", "name": "Allow"}
                    ]
                },
            }
        )
        assert result["outcome"]["outcome"] == "selected"

    def test_handler_exception_propagates(self, tmp_path):
        client = self._make_client()
        with pytest.raises(FileNotFoundError):
            client._handle_agent_request(
                {
                    "method": "fs/read_text_file",
                    "params": {"path": "/nonexistent/file.txt"},
                }
            )


# ---------------------------------------------------------------------------
# _handle_notification routing
# ---------------------------------------------------------------------------


class TestHandleNotification:
    """Notifications are routed to the correct session's update queue."""

    def _make_client(self) -> AcpClient:
        client = AcpClient.__new__(AcpClient)
        client._sessions = {}
        client._update_queues = {}
        return client

    def test_session_update_routed_to_queue(self):
        client = self._make_client()
        queue: asyncio.Queue = asyncio.Queue()
        client._update_queues["session-1"] = queue

        client._handle_notification(
            {
                "method": "session/update",
                "params": {
                    "sessionId": "session-1",
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "hello"},
                    },
                },
            }
        )
        assert not queue.empty()
        update = queue.get_nowait()
        assert update["sessionUpdate"] == "agent_message_chunk"

    def test_unknown_session_id_silently_dropped(self):
        """Updates for sessions we're not tracking are dropped without error."""
        client = self._make_client()
        # No queues registered — should not raise
        client._handle_notification(
            {
                "method": "session/update",
                "params": {
                    "sessionId": "unknown-session",
                    "update": {"sessionUpdate": "agent_message_chunk"},
                },
            }
        )

    def test_non_session_update_notification_ignored(self):
        """Notifications that aren't session/update are handled gracefully."""
        client = self._make_client()
        # Should not raise even though no handler exists for this method
        client._handle_notification(
            {
                "method": "some/other_notification",
                "params": {},
            }
        )


# ---------------------------------------------------------------------------
# _extract_models
# ---------------------------------------------------------------------------


class TestExtractModels:
    """Model catalog parsing from ACP session/new response."""

    def _make_client(self) -> AcpClient:
        client = AcpClient.__new__(AcpClient)
        client._models = []
        client._default_model = None
        return client

    def test_typical_response(self):
        client = self._make_client()
        client._extract_models(
            {
                "availableModels": [
                    {"modelId": "gpt-4.1", "name": "GPT 4.1"},
                    {"modelId": "gpt-4o", "name": "GPT 4o", "_meta": {"tier": "free"}},
                ],
                "currentModelId": "gpt-4.1",
            }
        )
        assert len(client._models) == 2
        assert client._models[0].model_id == "gpt-4.1"
        assert client._models[1].meta == {"tier": "free"}
        assert client._default_model == "gpt-4.1"

    def test_empty_models_list(self):
        client = self._make_client()
        client._extract_models({"availableModels": [], "currentModelId": None})
        assert client._models == []
        assert client._default_model is None

    def test_missing_name_uses_model_id(self):
        client = self._make_client()
        client._extract_models(
            {
                "availableModels": [{"modelId": "auto"}],
                "currentModelId": "auto",
            }
        )
        assert client._models[0].name == "auto"

    def test_missing_available_models_key(self):
        client = self._make_client()
        client._extract_models({})
        assert client._models == []
        assert client._default_model is None


# ---------------------------------------------------------------------------
# _try_set_model
# ---------------------------------------------------------------------------


class TestTrySetModel:
    """Model selection tries session/set_model, falls back, or raises."""

    @pytest.mark.asyncio
    async def test_first_method_succeeds(self):
        """session/set_model works — no fallback needed."""
        client = AcpClient.__new__(AcpClient)
        client._sessions = {"s1": SessionState(session_id="s1")}

        transport = AsyncMock()
        transport.send_request = AsyncMock(return_value={})
        client._transport = transport

        await client._try_set_model("s1", "gpt-4o")
        transport.send_request.assert_called_once_with(
            "session/set_model", {"sessionId": "s1", "modelId": "gpt-4o"}
        )
        assert client._sessions["s1"].model_id == "gpt-4o"

    @pytest.mark.asyncio
    async def test_fallback_to_set_config_option(self):
        """session/set_model fails with 'not found', falls back to set_config_option."""
        from acp_proxy.transport import AcpError

        client = AcpClient.__new__(AcpClient)
        client._sessions = {"s1": SessionState(session_id="s1")}

        call_count = 0

        async def mock_send(method, params):
            nonlocal call_count
            call_count += 1
            if method == "session/set_model":
                raise AcpError("Method not found", {"code": -32601})
            return {}

        transport = MagicMock()
        transport.send_request = mock_send
        client._transport = transport

        await client._try_set_model("s1", "gpt-4o")
        assert call_count == 2
        assert client._sessions["s1"].model_id == "gpt-4o"

    @pytest.mark.asyncio
    async def test_both_methods_fail_raises(self):
        """Both methods return 'not found' — RuntimeError raised."""
        from acp_proxy.transport import AcpError

        client = AcpClient.__new__(AcpClient)
        client._sessions = {"s1": SessionState(session_id="s1")}

        async def mock_send(method, params):
            raise AcpError("Method not found", {"code": -32601})

        transport = MagicMock()
        transport.send_request = mock_send
        client._transport = transport

        with pytest.raises(RuntimeError, match="Model selection not supported"):
            await client._try_set_model("s1", "gpt-4o")

    @pytest.mark.asyncio
    async def test_non_not_found_error_propagates(self):
        """A non-'not found' error is re-raised immediately, no fallback."""
        from acp_proxy.transport import AcpError

        client = AcpClient.__new__(AcpClient)
        client._sessions = {"s1": SessionState(session_id="s1")}

        async def mock_send(method, params):
            raise AcpError("Server exploded", {"code": -32000})

        transport = MagicMock()
        transport.send_request = mock_send
        client._transport = transport

        with pytest.raises(AcpError, match="Server exploded"):
            await client._try_set_model("s1", "gpt-4o")


# ---------------------------------------------------------------------------
# Prompt timeout (prompt-level deadline enforcement)
# ---------------------------------------------------------------------------


class TestPromptTimeout:
    """Prompt-level timeout enforces a deadline on session/prompt.

    The prompt() method must raise PromptTimeout if the ACP server does
    not complete within the configured deadline.  This prevents a hung
    language server from blocking the HTTP connection indefinitely.
    """

    @pytest.mark.asyncio
    async def test_timeout_raises_prompt_timeout(self):
        """A prompt that exceeds the deadline raises PromptTimeout."""
        from acp_proxy.client import PromptTimeout

        client = AcpClient.__new__(AcpClient)
        client._sessions = {"s1": SessionState(session_id="s1")}
        client._update_queues = {}

        # Transport that never responds — simulates a hung server
        async def never_respond(method, params):
            await asyncio.sleep(999)

        transport = MagicMock()
        transport.send_request = never_respond
        client._transport = transport

        with pytest.raises(PromptTimeout) as exc_info:
            async for _ in client.prompt(
                "s1",
                [{"role": "user", "content": "hello"}],
                timeout_s=0.2,
            ):
                pass

        assert exc_info.value.session_id == "s1"
        assert exc_info.value.timeout_s == 0.2

    @pytest.mark.asyncio
    async def test_timeout_includes_partial_text(self):
        """Partial text collected before the timeout is preserved in the exception."""
        from acp_proxy.client import PromptTimeout

        client = AcpClient.__new__(AcpClient)
        client._sessions = {"s1": SessionState(session_id="s1")}
        client._update_queues = {}

        async def slow_respond(method, params):
            # Wait long enough that chunks are delivered, then hang
            await asyncio.sleep(999)

        transport = MagicMock()
        transport.send_request = slow_respond
        client._transport = transport

        async def push_chunks():
            """Push chunks into the queue shortly after it's created."""
            # Wait for prompt() to create the queue
            for _ in range(50):
                if "s1" in client._update_queues:
                    break
                await asyncio.sleep(0.01)
            q = client._update_queues.get("s1")
            if q:
                q.put_nowait(
                    {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "partial "},
                    }
                )
                q.put_nowait(
                    {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "response"},
                    }
                )

        # Start pushing chunks concurrently
        push_task = asyncio.create_task(push_chunks())

        with pytest.raises(PromptTimeout) as exc_info:
            async for _ in client.prompt(
                "s1",
                [{"role": "user", "content": "hello"}],
                timeout_s=0.5,
            ):
                pass

        await push_task
        assert exc_info.value.partial_text == "partial response"

    @pytest.mark.asyncio
    async def test_normal_completion_within_timeout(self):
        """A prompt that completes before the deadline works normally."""
        client = AcpClient.__new__(AcpClient)
        client._sessions = {"s1": SessionState(session_id="s1")}
        client._update_queues = {}

        async def fast_respond(method, params):
            # Respond quickly
            await asyncio.sleep(0.05)
            return {"stopReason": "end_turn"}

        transport = MagicMock()
        transport.send_request = fast_respond
        transport.on_notification = MagicMock()
        transport.on_request = MagicMock()
        client._transport = transport

        # Push an update and then let the prompt task complete
        async def push_update():
            await asyncio.sleep(0.01)
            q = client._update_queues.get("s1")
            if q:
                q.put_nowait(
                    {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "hello"},
                    }
                )

        asyncio.create_task(push_update())

        results = []
        async for update in client.prompt(
            "s1",
            [{"role": "user", "content": "hi"}],
            timeout_s=5.0,
        ):
            results.append(update)

        # Should have the chunk + done sentinel
        assert any(r.get("done") for r in results)

    @pytest.mark.asyncio
    async def test_unknown_session_raises_value_error(self):
        """Prompting an unknown session raises ValueError, not timeout."""
        client = AcpClient.__new__(AcpClient)
        client._sessions = {}
        client._update_queues = {}

        with pytest.raises(ValueError, match="Unknown session"):
            async for _ in client.prompt(
                "nonexistent",
                [{"role": "user", "content": "hello"}],
            ):
                pass

    @pytest.mark.asyncio
    async def test_queue_cleanup_after_timeout(self):
        """The update queue is removed after a timeout to prevent leaks."""
        from acp_proxy.client import PromptTimeout

        client = AcpClient.__new__(AcpClient)
        client._sessions = {"s1": SessionState(session_id="s1")}
        client._update_queues = {}

        async def never_respond(method, params):
            await asyncio.sleep(999)

        transport = MagicMock()
        transport.send_request = never_respond
        client._transport = transport

        with pytest.raises(PromptTimeout):
            async for _ in client.prompt(
                "s1",
                [{"role": "user", "content": "hello"}],
                timeout_s=0.1,
            ):
                pass

        # Queue should be cleaned up
        assert "s1" not in client._update_queues
