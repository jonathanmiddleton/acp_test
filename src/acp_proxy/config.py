"""
User configuration for the ACP proxy.

Loads settings from ``~/.acp_proxy/config.json``.  This file is per-user,
not per-project — it stores environment-specific settings like proxy
configuration that apply to all repos on the machine.

Also handles workspace context injection: scanning the workspace for
markdown files (``AGENTS.md`` by default) and composing them into a
system prompt for ACP sessions.  See ADR-011 for the design rationale.

Environment variables take precedence over config file values.  This lets
users override per-session without editing the file.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_CONFIG_DIR = ".acp_proxy"
_CONFIG_FILE = "config.json"

# Proxy-related environment variable names.  Both upper and lowercase
# forms are checked (Node.js and curl respect both).
_PROXY_ENV_VARS = (
    "HTTP_PROXY",
    "http_proxy",
    "HTTPS_PROXY",
    "https_proxy",
    "NO_PROXY",
    "no_proxy",
)


def config_dir() -> str:
    """Return the path to the config directory (``~/.acp_proxy/``)."""
    return os.path.join(os.path.expanduser("~"), _CONFIG_DIR)


def config_path() -> str:
    """Return the path to the config file."""
    return os.path.join(config_dir(), _CONFIG_FILE)


def load_config() -> dict[str, Any]:
    """Load user configuration from disk.

    Returns an empty dict if the config file does not exist.
    Logs a warning and returns an empty dict if the file is malformed.
    """
    path = config_path()
    if not os.path.isfile(path):
        logger.debug("No config file at %s", path)
        return {}

    try:
        with open(path) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            logger.warning("Config file %s is not a JSON object, ignoring", path)
            return {}
        logger.info("Loaded config from %s", path)
        return data
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to read config file %s: %s", path, e)
        return {}


def build_subprocess_env(cfg: dict[str, Any] | None = None) -> dict[str, str]:
    """Build the environment dict for the language server subprocess.

    Starts with the current process environment, then applies proxy
    settings from the config file.  Environment variables already set
    in the current process take precedence over config file values —
    they are never overwritten.

    Config file keys are matched case-insensitively to the canonical
    environment variable names.  Recognized keys:

    - ``http_proxy`` / ``HTTP_PROXY``
    - ``https_proxy`` / ``HTTPS_PROXY``
    - ``no_proxy`` / ``NO_PROXY``

    Returns a new dict suitable for passing as ``env`` to subprocess
    creation.  The original ``os.environ`` is not modified.
    """
    env = dict(os.environ)

    if cfg is None:
        cfg = load_config()

    # Build a case-insensitive lookup of config proxy values
    cfg_lower = {k.lower(): v for k, v in cfg.items() if isinstance(v, str)}

    for var in _PROXY_ENV_VARS:
        # Skip if already set in the environment
        if var in env:
            logger.debug(
                "Proxy var %s already set in environment, skipping config", var
            )
            continue

        # Look up in config (case-insensitive)
        value = cfg_lower.get(var.lower())
        if value:
            env[var] = value
            logger.info("Set %s from config file", var)

    return env


# ---------------------------------------------------------------------------
# Context file discovery and system prompt composition (ADR-011)
# ---------------------------------------------------------------------------

_DEFAULT_CONTEXT_FILES = ["AGENTS.md"]

# Approximate tokens per character.  GPT tokenizers average ~4 chars/token
# for English prose.  This is an estimate — actual tokenization depends on
# the model's tokenizer, and we cannot observe what Copilot's backend
# injects (its own system prompt, safety policies, tool definitions).
_CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """Estimate token count from character length.

    Uses a rough heuristic of ~4 characters per token.  This is an
    *estimate* — actual tokenization varies by model and content.
    Copilot's backend also injects additional context that we cannot
    observe or measure.
    """
    return len(text) // _CHARS_PER_TOKEN


def get_context_files(cfg: dict[str, Any] | None = None) -> list[str]:
    """Return the list of context file names to scan for in the workspace.

    Reads from ``context_files`` in the config.  Falls back to the
    default list (``["AGENTS.md"]``) if not configured.
    """
    if cfg is None:
        cfg = load_config()

    files = cfg.get("context_files")
    if files is not None:
        if not isinstance(files, list):
            logger.warning("context_files in config is not a list, using default")
            return list(_DEFAULT_CONTEXT_FILES)
        # Filter to strings only
        result = [f for f in files if isinstance(f, str)]
        if len(result) != len(files):
            logger.warning("Non-string entries in context_files were ignored")
        return result

    return list(_DEFAULT_CONTEXT_FILES)


def load_context_files(
    cwd: str, cfg: dict[str, Any] | None = None
) -> list[tuple[str, str]]:
    """Load context files from the workspace directory.

    Scans ``cwd`` for each file in the context file list.  Files that
    don't exist are silently skipped.  Returns a list of
    ``(filename, content)`` tuples for files that were found and read.
    """
    file_names = get_context_files(cfg)
    loaded: list[tuple[str, str]] = []

    for name in file_names:
        path = os.path.join(cwd, name)
        if not os.path.isfile(path):
            logger.debug("Context file not found, skipping: %s", path)
            continue
        try:
            with open(path, encoding="utf-8") as f:
                content = f.read()
            if content.strip():
                loaded.append((name, content))
                logger.info(
                    "Loaded context file: %s (%d chars, ~%d tokens)",
                    name,
                    len(content),
                    estimate_tokens(content),
                )
            else:
                logger.debug("Context file is empty, skipping: %s", name)
        except OSError as e:
            logger.warning("Failed to read context file %s: %s", path, e)

    return loaded


def compose_system_prompt(
    explicit_prompt: str | None,
    cwd: str,
    cfg: dict[str, Any] | None = None,
) -> str | None:
    """Compose the system prompt from explicit prompt and workspace context.

    Composition order (per ADR-011):
    1. Explicit ``--system-prompt`` content (consumer's control surface)
    2. Workspace context files in configured order

    Returns the composed prompt string, or None if no content is available.
    """
    parts: list[str] = []

    # 1. Explicit system prompt comes first
    if explicit_prompt:
        parts.append(explicit_prompt)

    # 2. Context files from workspace
    context_files = load_context_files(cwd, cfg)
    for name, content in context_files:
        # Wrap each file with a header for clarity
        parts.append(f"# {name}\n\n{content}")

    if not parts:
        return None

    composed = "\n\n---\n\n".join(parts)

    total_tokens = estimate_tokens(composed)
    logger.info(
        "Composed system prompt: %d chars, ~%d estimated tokens "
        "(from %d source(s): %s%s)",
        len(composed),
        total_tokens,
        (1 if explicit_prompt else 0) + len(context_files),
        "explicit prompt" if explicit_prompt else "",
        (", " if explicit_prompt and context_files else "")
        + ", ".join(name for name, _ in context_files),
    )

    return composed
