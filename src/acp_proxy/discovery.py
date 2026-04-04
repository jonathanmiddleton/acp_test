"""
Binary discovery for copilot-language-server.

The only supported binary is the one bundled with the GitHub Copilot plugin
for IntelliJ IDEA 2025.3. Other versions (older IntelliJ, other JetBrains
IDEs, standalone installs, Homebrew, npm) are known to be incompatible with
the ACP protocol surface this proxy requires.

Supported platforms:
- macOS (Darwin): binary at ~/Library/Application Support/JetBrains/IntelliJIdea2025.3/plugins/...
- Windows: binary at %APPDATA%/JetBrains/IntelliJIdea2025.3/plugins/... (roaming profile)
  NOTE: Windows path is provisional — needs verification on the target environment.

This module is the single source of truth for binary resolution. Both the
CLI entry point and the test suite import from here.
"""

from __future__ import annotations

import glob
import logging
import os
import platform
import re
import subprocess

logger = logging.getLogger(__name__)

_PLUGIN_SUFFIX_PARTS = (
    "plugins",
    "github-copilot-intellij",
    "copilot-agent",
    "native",
    "{arch}",
    "{binary_name}",
)


def _platform_config() -> dict[str, str]:
    """Return platform-specific discovery configuration.

    Returns a dict with keys: base, arch, binary_name, ide_dir.
    """
    system = platform.system()
    home = os.path.expanduser("~")

    if system == "Darwin":
        return {
            "base": os.path.join(home, "Library/Application Support/JetBrains"),
            "arch": "darwin-arm64" if platform.machine() == "arm64" else "darwin-x64",
            "binary_name": "copilot-language-server",
            "ide_dir": "IntelliJIdea2025.3",
        }
    elif system == "Windows":
        # Windows uses %APPDATA% (roaming profile) for JetBrains config.
        # NOTE: This path is provisional and needs verification on the
        # actual target environment. The binary is an .exe on Windows.
        appdata = os.environ.get("APPDATA", os.path.join(home, "AppData/Roaming"))
        return {
            "base": os.path.join(appdata, "JetBrains"),
            "arch": "win32-x64",
            "binary_name": "copilot-language-server.exe",
            "ide_dir": "IntelliJIdea2025.3",
        }
    else:
        # Linux — included for completeness but not a current target
        return {
            "base": os.path.join(home, ".local/share/JetBrains"),
            "arch": "linux-x64",
            "binary_name": "copilot-language-server",
            "ide_dir": "IntelliJIdea2025.3",
        }


def _compatible_path_pattern() -> str:
    """Return the expected full path for the compatible binary on this platform."""
    cfg = _platform_config()
    suffix_parts = [
        p.format(arch=cfg["arch"], binary_name=cfg["binary_name"])
        for p in _PLUGIN_SUFFIX_PARTS
    ]
    return os.path.join(cfg["base"], cfg["ide_dir"], *suffix_parts)


def _compatible_regex() -> re.Pattern[str]:
    """Compile a regex that matches the compatible binary path.

    The path is fully fixed (no wildcards) — there is exactly one valid
    location per platform.
    """
    pattern = _compatible_path_pattern()
    return re.compile(re.escape(pattern))


def _is_compatible_path(binary_path: str) -> bool:
    """Check whether a binary path matches the compatible IntelliJ 2025.3 pattern."""
    return _compatible_regex().fullmatch(binary_path) is not None


def find_binary_from_jetbrains() -> str | None:
    """Find the compatible binary from the JetBrains plugin directory.

    Returns the path if it exists and is executable, or None.
    """
    expected = _compatible_path_pattern()
    if os.path.isfile(expected) and os.access(expected, os.X_OK):
        logger.info("Found compatible binary on disk: %s", expected)
        return expected

    logger.debug("Binary not found at expected path: %s", expected)
    return None


def find_binary_from_processes() -> str | None:
    """Find a compatible binary from running processes.

    Scans ``ps`` output for copilot-language-server processes, but only
    accepts those whose resolved path matches the IntelliJ 2025.3 plugin
    location. Incompatible binaries (other JetBrains versions, standalone
    installs, npm global, etc.) are explicitly rejected.

    Only available on Unix-like systems (macOS, Linux). On Windows, returns
    None — process-based discovery is not yet implemented there.
    """
    if platform.system() == "Windows":
        logger.debug("Process-based discovery not implemented on Windows")
        return None

    try:
        out = subprocess.check_output(
            ["ps", "-eo", "command"], text=True, stderr=subprocess.DEVNULL
        )
    except Exception:
        logger.debug("Failed to run ps")
        return None

    candidates: list[str] = []
    rejected: list[str] = []

    for line in out.splitlines():
        if "copilot-language-server" not in line or "grep" in line:
            continue
        # Extract the binary path (everything before the first --flag)
        binary_path = line.split(" --")[0].strip()
        if _is_compatible_path(binary_path):
            candidates.append(binary_path)
        else:
            rejected.append(binary_path)

    if rejected:
        logger.warning(
            "Rejected incompatible copilot-language-server binaries from ps: %s",
            rejected,
        )

    if candidates:
        # Deduplicate (same binary may appear in multiple ps lines)
        seen: set[str] = set()
        unique = []
        for c in candidates:
            if c not in seen:
                seen.add(c)
                unique.append(c)
        logger.info("Found compatible binary from ps: %s", unique[0])
        return unique[0]

    return None


def find_binary() -> str | None:
    """Locate the compatible copilot-language-server binary.

    Discovery order:
    1. Running processes (filtered to compatible path pattern)
    2. JetBrains plugin directory on disk

    Returns None if no compatible binary is found.
    """
    binary = find_binary_from_processes()
    if binary:
        return binary

    logger.info("No compatible binary in running processes, checking disk...")
    return find_binary_from_jetbrains()
