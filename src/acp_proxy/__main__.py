"""
Entry point for the ACP-to-OpenAI proxy.

Usage:
    acp-proxy [OPTIONS]

    Start the proxy from your project directory. The current working directory
    becomes the ACP workspace — the copilot-language-server scans it and scopes
    file operations to it.

    --binary PATH     Path to copilot-language-server binary.
                      Auto-discovered if omitted (IntelliJ 2025.3 plugin only).
    --port PORT       Port to listen on (default: 8765)
    --cwd PATH        Working directory for ACP sessions (default: current dir)
    --log-level LEVEL Console logging level (default: INFO)
    --log-file PATH   Log file path (default: logs/proxy.log)
    --system-prompt   Path to system prompt file injected into each new session.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import os
import signal
import sys

import uvicorn

from .client import AcpClient
from .discovery import find_binary
from .server import create_app

logger = logging.getLogger(__name__)

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
LOG_MAX_BYTES = 5 * 1024 * 1024  # 5 MB per file
LOG_BACKUP_COUNT = 3


def _configure_logging(console_level: str, log_file: str) -> None:
    """Set up dual logging: DEBUG to file (always), configurable to console."""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # Console handler — respects --log-level
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(getattr(logging, console_level))
    console.setFormatter(logging.Formatter(LOG_FORMAT))
    root.addHandler(console)

    # File handler — always DEBUG, with rotation
    log_dir = os.path.dirname(log_file)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
    root.addHandler(file_handler)

    # Route uvicorn access and error logs through the same handlers
    for uv_logger_name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        uv_logger = logging.getLogger(uv_logger_name)
        uv_logger.handlers.clear()
        uv_logger.propagate = True


async def run(
    binary: str,
    port: int,
    cwd: str,
    system_prompt: str | None = None,
) -> None:
    """Start the ACP client and HTTP server."""
    client = AcpClient(binary)
    await client.start()

    # Create an initial session to discover models
    await client.create_session(cwd)

    logger.info("Available models: %s", [m.model_id for m in client.models])
    logger.info("Default model: %s", client.default_model)

    app = create_app(client, cwd, system_prompt=system_prompt)

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",  # Suppress uvicorn's own logging; we route via root
    )
    server = uvicorn.Server(config)

    # Handle shutdown
    loop = asyncio.get_event_loop()
    shutdown_event = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("Shutdown signal received")
        shutdown_event.set()

    if sys.platform == "win32":
        # asyncio.loop.add_signal_handler is not supported on Windows.
        # Use signal.signal for SIGINT (Ctrl+C); SIGTERM does not exist
        # on Windows.
        signal.signal(signal.SIGINT, lambda *_: _signal_handler())
    else:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _signal_handler)

    # Run server in background task
    server_task = asyncio.create_task(server.serve())

    logger.info("Proxy listening on http://127.0.0.1:%d", port)
    logger.info("Models endpoint: http://127.0.0.1:%d/v1/models", port)
    logger.info("Completions endpoint: http://127.0.0.1:%d/v1/chat/completions", port)

    # Wait for shutdown signal or server to stop
    done, _ = await asyncio.wait(
        [server_task, asyncio.create_task(shutdown_event.wait())],
        return_when=asyncio.FIRST_COMPLETED,
    )

    # Clean up
    if not server_task.done():
        server.should_exit = True
        await server_task
    await client.stop()
    logger.info("Proxy stopped.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="ACP-to-OpenAI proxy for copilot-language-server"
    )
    parser.add_argument(
        "--binary",
        help="Path to copilot-language-server binary (auto-discovered if omitted)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="Port to listen on (default: 8765)",
    )
    parser.add_argument(
        "--cwd",
        default=os.getcwd(),
        help="Working directory for ACP sessions (default: current dir)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Console logging level (default: INFO). File always logs DEBUG.",
    )
    parser.add_argument(
        "--log-file",
        default="logs/proxy.log",
        help="Log file path (default: logs/proxy.log). DEBUG level always.",
    )
    parser.add_argument(
        "--system-prompt",
        help="Path to a file containing a system prompt to inject into each new session.",
    )
    args = parser.parse_args()

    _configure_logging(args.log_level, args.log_file)

    binary = args.binary
    if not binary:
        logger.info("Auto-discovering compatible copilot-language-server...")
        binary = find_binary()
    if not binary:
        logger.error(
            "Could not find a compatible copilot-language-server binary. "
            "Only the IntelliJ IDEA 2025.3 Copilot plugin binary is supported. "
            "Pass --binary /path/to/copilot-language-server to override."
        )
        sys.exit(1)

    system_prompt = None
    if args.system_prompt:
        with open(args.system_prompt) as f:
            system_prompt = f.read().strip()
        logger.info(
            "Loaded system prompt from %s (%d chars)",
            args.system_prompt,
            len(system_prompt),
        )

    logger.info("Using binary: %s", binary)

    asyncio.run(run(binary, args.port, args.cwd, system_prompt=system_prompt))


if __name__ == "__main__":
    main()
