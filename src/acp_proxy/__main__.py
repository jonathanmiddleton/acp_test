"""
Entry point for the ACP-to-OpenAI proxy.

Usage:
    python -m acp_proxy [OPTIONS]

    --binary PATH     Path to copilot-language-server binary.
                      Auto-discovered from running processes if omitted.
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
import subprocess
import sys

import uvicorn

from .client import AcpClient
from .server import create_app

logger = logging.getLogger(__name__)


def find_binary() -> str | None:
    """Locate the copilot-language-server binary from running processes."""
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
    return None


def find_binary_from_jetbrains() -> str | None:
    """Try to find the binary from the JetBrains plugin directory."""
    import glob

    home = os.path.expanduser("~")
    patterns = [
        os.path.join(
            home,
            "Library/Application Support/JetBrains/*/plugins/"
            "github-copilot-intellij/copilot-agent/native/darwin-arm64/"
            "copilot-language-server",
        ),
        # Linux path pattern
        os.path.join(
            home,
            ".local/share/JetBrains/*/plugins/"
            "github-copilot-intellij/copilot-agent/native/linux-x64/"
            "copilot-language-server",
        ),
    ]
    for pattern in patterns:
        matches = glob.glob(pattern)
        if matches:
            # Return the most recently modified one
            matches.sort(key=os.path.getmtime, reverse=True)
            return matches[0]
    return None


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
        logger.info(
            "Auto-discovering copilot-language-server from running processes..."
        )
        binary = find_binary()
    if not binary:
        logger.info("Not found in processes, checking JetBrains plugin directories...")
        binary = find_binary_from_jetbrains()
    if not binary:
        logger.error(
            "Could not find copilot-language-server. "
            "Pass --binary /path/to/copilot-language-server"
        )
        sys.exit(1)

    logger.info("Using binary: %s", binary)

    asyncio.run(run(binary, args.port, args.cwd))


if __name__ == "__main__":
    main()
