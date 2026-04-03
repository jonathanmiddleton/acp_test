"""
Binary discovery for copilot-language-server.

The only supported binary is the one bundled with the GitHub Copilot plugin
for IntelliJ IDEA 2025.3. Other versions (older IntelliJ, other JetBrains
IDEs, standalone installs, Homebrew, npm) are known to be incompatible with
the ACP protocol surface this proxy requires.

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

# The only compatible binary path pattern.
# The wildcard covers the user-specific directory segment (e.g., a SOEID
# or version-specific plugin cache directory).
_INTELLIJ_2025_3_PATTERN_SUFFIX = (
    "plugins/github-copilot-intellij/copilot-agent/native/{arch}/"
    "copilot-language-server"
)


def _compatible_pattern() -> str:
    """Return the glob pattern for the compatible binary on this platform."""
    home = os.path.expanduser("~")

    if platform.system() == "Darwin":
        arch = "darwin-arm64" if platform.machine() == "arm64" else "darwin-x64"
        base = os.path.join(home, "Library/Application Support/JetBrains")
    else:
        arch = "linux-x64"
        base = os.path.join(home, ".local/share/JetBrains")

    suffix = _INTELLIJ_2025_3_PATTERN_SUFFIX.format(arch=arch)
    return os.path.join(base, "IntellijIdea2025.3", "*", suffix)


def _is_compatible_path(binary_path: str) -> bool:
    """Check whether a binary path matches the compatible IntelliJ 2025.3 pattern.

    Uses a regex derived from the glob pattern so that both filesystem-discovered
    and ps-discovered paths are validated against the same constraint.
    """
    home = os.path.expanduser("~")

    if platform.system() == "Darwin":
        arch = "darwin-arm64" if platform.machine() == "arm64" else "darwin-x64"
        base = os.path.join(home, "Library/Application Support/JetBrains")
    else:
        arch = "linux-x64"
        base = os.path.join(home, ".local/share/JetBrains")

    suffix = _INTELLIJ_2025_3_PATTERN_SUFFIX.format(arch=arch)
    # Build a regex: escape the fixed parts, replace the wildcard segment
    pattern_path = os.path.join(base, "IntellijIdea2025.3", "*", suffix)
    # Escape everything except the glob wildcard
    regex = re.escape(pattern_path).replace(r"\*", "[^/]+")
    return re.fullmatch(regex, binary_path) is not None


def find_binary_from_jetbrains() -> str | None:
    """Find the compatible binary from the JetBrains plugin directory.

    Returns the path to the newest compatible binary, or None.
    """
    pattern = _compatible_pattern()
    matches = glob.glob(pattern)
    if not matches:
        logger.debug("No binaries found matching pattern: %s", pattern)
        return None

    # Sort by modification time descending — newest first
    matches.sort(key=os.path.getmtime, reverse=True)
    logger.info("Found compatible binary candidates: %s", matches)
    return matches[0]


def find_binary_from_processes() -> str | None:
    """Find a compatible binary from running processes.

    Scans ``ps`` output for copilot-language-server processes, but only
    accepts those whose resolved path matches the IntelliJ 2025.3 plugin
    pattern. Incompatible binaries (other JetBrains versions, standalone
    installs, npm global, etc.) are explicitly rejected.
    """
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
