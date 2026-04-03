"""
Entry point for the ACP-to-OpenAI proxy.

Usage:
    python -m acp_proxy [OPTIONS]

    --binary PATH     Path to copilot-language-server binary.
                      Auto-discovered if omitted (IntelliJ 2025.3 plugin only).
    --port PORT       Port to listen on (default: 8765)
    --cwd PATH        Working directory for ACP sessions (default: current dir)
    --log-level LEVEL Logging level (default: INFO)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys

import uvicorn

from .client import AcpClient
from .discovery import find_binary
from .server import create_app

logger = logging.getLogger(__name__)


async def run(binary: str, port: int, cwd: str) -> None:
    """Start the ACP client and HTTP server."""
    client = AcpClient(binary)
    await client.start()

    # Create an initial session to discover models
    await client.create_session(cwd)

    logger.info("Available models: %s", [m.model_id for m in client.models])
    logger.info("Default model: %s", client.default_model)

    app = create_app(client, cwd)

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="info",
    )
    server = uvicorn.Server(config)

    # Handle shutdown
    loop = asyncio.get_event_loop()
    shutdown_event = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("Shutdown signal received")
        shutdown_event.set()

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
        help="Logging level (default: INFO)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

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

    logger.info("Using binary: %s", binary)

    asyncio.run(run(binary, args.port, args.cwd))


if __name__ == "__main__":
    main()
