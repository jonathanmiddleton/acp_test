"""Tests for binary discovery logic."""

from __future__ import annotations

import os
import platform
from unittest.mock import patch

from acp_proxy.discovery import (
    _compatible_path_pattern,
    _is_compatible_path,
    _platform_config,
)


def _make_path(*segments: str) -> str:
    """Build a path from segments, using the real home directory."""
    home = os.path.expanduser("~")
    return os.path.join(home, *segments)


class TestIsCompatiblePath:
    """_is_compatible_path accepts only the exact IntelliJ 2025.3 binary location."""

    def test_correct_path_matches(self):
        """The exact expected path is accepted."""
        expected = _compatible_path_pattern()
        assert _is_compatible_path(expected)

    def test_pycharm_rejected(self):
        """A PyCharm binary is not compatible."""
        cfg = _platform_config()
        bad = _make_path(
            "Library/Application Support/JetBrains",
            "PyCharm2025.3",
            "plugins/github-copilot-intellij/copilot-agent/native",
            cfg["arch"],
            cfg["binary_name"],
        )
        assert not _is_compatible_path(bad)

    def test_older_intellij_rejected(self):
        """An older IntelliJ version is not compatible."""
        cfg = _platform_config()
        bad = _make_path(
            "Library/Application Support/JetBrains",
            "IntelliJIdea2024.2",
            "plugins/github-copilot-intellij/copilot-agent/native",
            cfg["arch"],
            cfg["binary_name"],
        )
        assert not _is_compatible_path(bad)

    def test_homebrew_path_rejected(self):
        """A standalone/homebrew install is not compatible."""
        assert not _is_compatible_path("/usr/local/bin/copilot-language-server")

    def test_npm_global_path_rejected(self):
        """An npm global install is not compatible."""
        bad = _make_path(".npm-global/bin/copilot-language-server")
        assert not _is_compatible_path(bad)

    def test_empty_string_rejected(self):
        assert not _is_compatible_path("")

    def test_partial_path_rejected(self):
        """A partial path that contains the right directory names but is incomplete."""
        assert not _is_compatible_path(
            "IntelliJIdea2025.3/plugins/copilot-language-server"
        )

    def test_path_with_extra_suffix_rejected(self):
        """A path that extends beyond the expected binary location."""
        expected = _compatible_path_pattern()
        assert not _is_compatible_path(expected + "/extra")


class TestPlatformConfig:
    """_platform_config returns sane values for the current platform."""

    def test_config_has_required_keys(self):
        cfg = _platform_config()
        assert "base" in cfg
        assert "arch" in cfg
        assert "binary_name" in cfg
        assert "ide_dir" in cfg

    def test_ide_dir_is_intellij_2025_3(self):
        cfg = _platform_config()
        assert cfg["ide_dir"] == "IntelliJIdea2025.3"

    def test_binary_name_platform_appropriate(self):
        cfg = _platform_config()
        if platform.system() == "Windows":
            assert cfg["binary_name"].endswith(".exe")
        else:
            assert not cfg["binary_name"].endswith(".exe")
