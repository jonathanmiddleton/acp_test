"""
ACP client for copilot-language-server.

Manages initialization, session lifecycle, model selection, and prompt
execution. Translates between ACP's stateful session model and the
request/response pattern needed by the OpenAI-compatible proxy layer.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from .transport import AcpError, AcpTransport

logger = logging.getLogger(__name__)


@dataclass
class ModelInfo:
    """A model available through the ACP agent."""

    model_id: str
    name: str
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class SessionState:
    """Tracks the state of an ACP session."""

    session_id: str
    model_id: str | None = None
    created_at: float = field(default_factory=time.time)


class AcpClient:
    """High-level ACP client wrapping transport + session management.

    Responsibilities:
    - ACP initialization handshake
    - Session creation with model selection
    - Prompt execution with streaming response collection
    - Handling agent callbacks (permission requests, fs, terminal)
    """

    def __init__(self, binary_path: str) -> None:
        self._binary_path = binary_path
        self._transport = AcpTransport()
        self._models: list[ModelInfo] = []
        self._default_model: str | None = None
        self._sessions: dict[str, SessionState] = {}
        self._update_queues: dict[str, asyncio.Queue[dict[str, Any] | None]] = {}
        self._agent_name: str | None = None
        self._agent_version: str | None = None

    @property
    def models(self) -> list[ModelInfo]:
        return list(self._models)

    @property
    def default_model(self) -> str | None:
        return self._default_model

    @property
    def agent_info(self) -> dict[str, str | None]:
        return {"name": self._agent_name, "version": self._agent_version}

    async def start(self) -> None:
        """Start the language server and complete ACP initialization."""
        self._transport.on_notification(self._handle_notification)
        self._transport.on_request(self._handle_agent_request)
        await self._transport.start(self._binary_path)
        await self._initialize()

    async def stop(self) -> None:
        """Shut down the transport and clean up sessions."""
        # Signal all active update queues to stop
        for q in self._update_queues.values():
            q.put_nowait(None)
        self._update_queues.clear()
        self._sessions.clear()
        await self._transport.stop()

    async def create_session(self, cwd: str, model_id: str | None = None) -> str:
        """Create a new ACP session.

        Returns the session ID. If model_id is provided, the model is
        set after session creation.
        """
        result = await self._transport.send_request(
            "session/new",
            {"cwd": cwd, "mcpServers": []},
        )
        session_id = result["sessionId"]

        # Extract models from session response if present
        if "models" in result:
            self._extract_models(result["models"])

        session = SessionState(
            session_id=session_id,
            model_id=model_id or self._default_model,
        )
        self._sessions[session_id] = session

        # Set model if specified and different from default
        if model_id and model_id != self._default_model:
            await self._try_set_model(session_id, model_id)

        logger.info("Created session %s with model %s", session_id, session.model_id)
        return session_id

    async def prompt(
        self, session_id: str, messages: list[dict[str, Any]]
    ) -> AsyncIterator[dict[str, Any]]:
        """Send a prompt and yield streaming update events.

        Translates the OpenAI messages array into an ACP prompt.
        Yields individual session/update events as they arrive.
        The final yield is a sentinel dict with 'done': True and the
        stop reason.

        Args:
            session_id: The ACP session ID.
            messages: OpenAI-format messages array.

        Yields:
            Update dicts from ACP session/update notifications.
        """
        if session_id not in self._sessions:
            raise ValueError(f"Unknown session: {session_id}")

        # Convert OpenAI messages to ACP prompt content blocks
        prompt_content = self._messages_to_prompt(messages)

        # Set up a queue to receive streaming updates for this session
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._update_queues[session_id] = queue

        try:
            # Send prompt — this returns when the turn is complete
            prompt_task = asyncio.create_task(
                self._transport.send_request(
                    "session/prompt",
                    {"sessionId": session_id, "prompt": prompt_content},
                )
            )

            # Yield updates as they arrive
            while True:
                # Check both the queue and the prompt completion
                try:
                    update = await asyncio.wait_for(queue.get(), timeout=0.1)
                    if update is None:
                        break
                    yield update
                except asyncio.TimeoutError:
                    if prompt_task.done():
                        # Drain remaining updates
                        while not queue.empty():
                            update = queue.get_nowait()
                            if update is not None:
                                yield update
                        break

            # Get the final response
            result = await prompt_task
            yield {"done": True, "stopReason": result.get("stopReason", "end_turn")}

        finally:
            self._update_queues.pop(session_id, None)

    async def set_model(self, session_id: str, model_id: str) -> None:
        """Change the model for an existing session."""
        await self._try_set_model(session_id, model_id)

    async def _initialize(self) -> None:
        """Complete the ACP initialization handshake."""
        result = await self._transport.send_request(
            "initialize",
            {
                "protocolVersion": 1,
                "clientInfo": {"name": "acp-proxy", "version": "0.1.0"},
                "clientCapabilities": {
                    "fs": {"readTextFile": True, "writeTextFile": True},
                    "terminal": True,
                },
            },
        )
        info = result.get("agentInfo", {})
        self._agent_name = info.get("name")
        self._agent_version = info.get("version")
        logger.info(
            "Initialized: %s v%s, protocol=%s",
            self._agent_name,
            self._agent_version,
            result.get("protocolVersion"),
        )

    def _extract_models(self, models_data: dict[str, Any]) -> None:
        """Extract available models from a session/new response."""
        self._models = []
        for m in models_data.get("availableModels", []):
            self._models.append(
                ModelInfo(
                    model_id=m["modelId"],
                    name=m.get("name", m["modelId"]),
                    meta=m.get("_meta", {}),
                )
            )
        self._default_model = models_data.get("currentModelId")

    async def _try_set_model(self, session_id: str, model_id: str) -> None:
        """Set the model for a session.

        Tries session/set_model (Copilot-specific) first, then falls back
        to session/set_config_option (ACP spec standard). Raises if neither
        method is supported — model selection is a required capability and
        silent degradation to the default model is not acceptable.
        """
        methods = [
            ("session/set_model", {"sessionId": session_id, "modelId": model_id}),
            (
                "session/set_config_option",
                {"sessionId": session_id, "configId": "model", "value": model_id},
            ),
        ]
        for method, params in methods:
            try:
                await self._transport.send_request(method, params)
                if session_id in self._sessions:
                    self._sessions[session_id].model_id = model_id
                logger.info(
                    "Set model for session %s to %s (via %s)",
                    session_id,
                    model_id,
                    method,
                )
                return
            except AcpError as e:
                if "not found" in str(e).lower():
                    logger.debug("%s not supported, trying next method", method)
                    continue
                raise

        raise RuntimeError(
            f"Model selection not supported by this server. "
            f"Tried: {[m for m, _ in methods]}. Requested model: {model_id}"
        )

    def _messages_to_prompt(
        self, messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Convert OpenAI messages array to ACP prompt content blocks.

        ACP prompts are a flat list of content blocks for a single turn.
        We concatenate all message content, with system messages prepended
        as context.
        """
        parts: list[str] = []

        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")

            if isinstance(content, list):
                # Handle content array format
                for block in content:
                    if block.get("type") == "text":
                        parts.append(block["text"])
            elif isinstance(content, str) and content:
                if role == "system":
                    parts.append(f"[System]: {content}")
                elif role == "assistant":
                    parts.append(f"[Previous response]: {content}")
                else:
                    parts.append(content)

        combined = "\n\n".join(parts)
        return [{"type": "text", "text": combined}]

    def _handle_notification(self, msg: dict[str, Any]) -> None:
        """Route incoming notifications to the appropriate session queue."""
        method = msg.get("method", "")
        params = msg.get("params", {})

        if method == "session/update":
            session_id = params.get("sessionId", "")
            queue = self._update_queues.get(session_id)
            if queue:
                queue.put_nowait(params.get("update", {}))

    def _handle_agent_request(self, msg: dict[str, Any]) -> Any:
        """Handle incoming requests from the agent.

        The agent may request:
        - session/request_permission: auto-approve in Agent mode
        - fs/read_text_file: read file from disk
        - fs/write_text_file: write file to disk
        - terminal/*: terminal operations

        For now, auto-approve permissions and handle fs operations directly.
        Terminal operations are handled with basic subprocess execution.
        """
        method = msg.get("method", "")
        params = msg.get("params", {})

        logger.info("Agent request: %s", method)

        if method == "session/request_permission":
            return self._handle_permission_request(params)
        elif method == "fs/read_text_file":
            return self._handle_read_file(params)
        elif method == "fs/write_text_file":
            return self._handle_write_file(params)
        elif method == "terminal/create":
            return self._handle_terminal_create(params)
        elif method == "terminal/output":
            return self._handle_terminal_output(params)
        elif method == "terminal/wait_for_exit":
            return self._handle_terminal_wait(params)
        elif method == "terminal/release":
            return self._handle_terminal_release(params)
        elif method == "terminal/kill":
            return self._handle_terminal_kill(params)
        else:
            logger.warning("Unhandled agent request: %s", method)
            return None

    def _handle_permission_request(self, params: dict[str, Any]) -> dict[str, Any]:
        """Auto-approve all permission requests."""
        options = params.get("options", [])
        # Prefer allow_always, then allow_once
        for opt in options:
            if opt.get("kind") == "allow_always":
                logger.info("Auto-approving (always): %s", opt.get("name"))
                return {"outcome": {"outcome": "selected", "optionId": opt["optionId"]}}
        for opt in options:
            if opt.get("kind") == "allow_once":
                logger.info("Auto-approving (once): %s", opt.get("name"))
                return {"outcome": {"outcome": "selected", "optionId": opt["optionId"]}}
        # Fallback: select first option
        if options:
            logger.info("Auto-selecting first option: %s", options[0].get("name"))
            return {
                "outcome": {"outcome": "selected", "optionId": options[0]["optionId"]}
            }
        return {"outcome": {"outcome": "cancelled"}}

    def _handle_read_file(self, params: dict[str, Any]) -> dict[str, Any]:
        """Read a file from disk."""
        path = params.get("path", "")
        line = params.get("line")
        limit = params.get("limit")
        try:
            with open(path) as f:
                lines = f.readlines()
            if line is not None:
                start = max(0, line - 1)  # 1-based to 0-based
                if limit is not None:
                    lines = lines[start : start + limit]
                else:
                    lines = lines[start:]
            content = "".join(lines)
            return {"content": content}
        except Exception as e:
            logger.error("Failed to read %s: %s", path, e)
            raise

    def _handle_write_file(self, params: dict[str, Any]) -> dict[str, Any]:
        """Write content to a file."""
        import os

        path = params.get("path", "")
        content = params.get("content", "")
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(content)
            return {}
        except Exception as e:
            logger.error("Failed to write %s: %s", path, e)
            raise

    # --- Terminal handling ---
    # Basic implementation using asyncio subprocesses.
    # Terminal state is tracked in _terminals dict.

    _terminals: dict[str, dict[str, Any]] = {}
    _terminal_counter: int = 0

    def _handle_terminal_create(self, params: dict[str, Any]) -> Any:
        """Create a terminal (run a command asynchronously)."""
        import subprocess as sp

        command = params.get("command", "")
        args = params.get("args", [])
        cwd = params.get("cwd")
        env_vars = params.get("env", [])

        import os

        env = dict(os.environ)
        for var in env_vars:
            env[var["name"]] = var["value"]

        self.__class__._terminal_counter += 1
        term_id = f"term_{self.__class__._terminal_counter}"

        try:
            proc = sp.Popen(
                [command] + args,
                cwd=cwd,
                env=env,
                stdout=sp.PIPE,
                stderr=sp.STDOUT,
                text=True,
            )
            self.__class__._terminals[term_id] = {
                "process": proc,
                "output": "",
                "byte_limit": params.get("outputByteLimit"),
            }
            logger.info("Created terminal %s: %s %s", term_id, command, args)
            return {"terminalId": term_id}
        except Exception as e:
            logger.error("Failed to create terminal: %s", e)
            raise

    def _handle_terminal_output(self, params: dict[str, Any]) -> dict[str, Any]:
        """Get current terminal output."""
        term_id = params.get("terminalId", "")
        term = self.__class__._terminals.get(term_id)
        if not term:
            return {"output": "", "truncated": False}

        proc = term["process"]
        # Read any available output
        if proc.stdout and proc.poll() is not None:
            remaining = proc.stdout.read()
            if remaining:
                term["output"] += remaining

        exit_status = None
        if proc.poll() is not None:
            exit_status = {"exitCode": proc.returncode, "signal": None}

        return {
            "output": term["output"],
            "truncated": False,
            "exitStatus": exit_status,
        }

    def _handle_terminal_wait(self, params: dict[str, Any]) -> dict[str, Any]:
        """Wait for terminal to exit."""
        term_id = params.get("terminalId", "")
        term = self.__class__._terminals.get(term_id)
        if not term:
            return {"exitCode": 1, "signal": None}

        proc = term["process"]
        try:
            stdout, _ = proc.communicate(timeout=120)
            if stdout:
                term["output"] += stdout
        except Exception:
            proc.kill()
        return {"exitCode": proc.returncode, "signal": None}

    def _handle_terminal_release(self, params: dict[str, Any]) -> dict[str, Any]:
        """Release a terminal."""
        term_id = params.get("terminalId", "")
        term = self.__class__._terminals.pop(term_id, None)
        if term:
            proc = term["process"]
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        return {}

    def _handle_terminal_kill(self, params: dict[str, Any]) -> dict[str, Any]:
        """Kill terminal command without releasing."""
        term_id = params.get("terminalId", "")
        term = self.__class__._terminals.get(term_id)
        if term:
            proc = term["process"]
            if proc.poll() is None:
                proc.kill()
        return {}
