"""Tests for binary discovery logic."""

from __future__ import annotations

import os
import platform
from unittest.mock import patch

from acp_proxy.discovery import (
    _compatible_path_pattern,
    _is_compatible_path,
    _platform_config,
    find_binary,
    find_binary_from_jetbrains,
    find_binary_from_processes,
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


class TestFindBinaryFromProcesses:
    """Process-based discovery (ADR-006): only compatible binaries accepted."""

    def _expected_path(self) -> str:
        return _compatible_path_pattern()

    def test_compatible_binary_found(self):
        """A running process with the compatible path is returned."""
        expected = self._expected_path()
        ps_output = f"COMMAND\n{expected} --acp --stdio\n/usr/bin/python3 script.py\n"
        with patch("subprocess.check_output", return_value=ps_output):
            result = find_binary_from_processes()
        assert result == expected

    def test_incompatible_binary_rejected(self):
        """A running copilot-language-server from the wrong IDE is rejected."""
        bad_path = self._expected_path().replace("IntelliJIdea2025.3", "PyCharm2025.3")
        ps_output = f"COMMAND\n{bad_path} --acp --stdio\n"
        with patch("subprocess.check_output", return_value=ps_output):
            result = find_binary_from_processes()
        assert result is None

    def test_mixed_compatible_and_incompatible(self):
        """Only the compatible binary is returned when both are running."""
        expected = self._expected_path()
        bad_path = self._expected_path().replace("IntelliJIdea2025.3", "PyCharm2025.3")
        ps_output = f"COMMAND\n{bad_path} --acp --stdio\n{expected} --acp --stdio\n"
        with patch("subprocess.check_output", return_value=ps_output):
            result = find_binary_from_processes()
        assert result == expected

    def test_duplicate_processes_deduplicated(self):
        """Same binary appearing multiple times in ps is deduplicated."""
        expected = self._expected_path()
        ps_output = (
            "COMMAND\n"
            f"{expected} --acp --stdio\n"
            f"{expected} --acp --stdio --some-other-flag\n"
        )
        with patch("subprocess.check_output", return_value=ps_output):
            result = find_binary_from_processes()
        assert result == expected

    def test_no_copilot_processes(self):
        """No copilot-language-server in ps output returns None."""
        ps_output = "COMMAND\n/usr/bin/python3\n/usr/bin/vim\n"
        with patch("subprocess.check_output", return_value=ps_output):
            result = find_binary_from_processes()
        assert result is None

    def test_ps_failure_returns_none(self):
        """If ps fails, return None instead of crashing."""
        with patch("subprocess.check_output", side_effect=OSError("ps not found")):
            result = find_binary_from_processes()
        assert result is None

    def test_grep_lines_filtered(self):
        """Lines containing 'grep' are excluded (standard ps filtering)."""
        expected = self._expected_path()
        ps_output = f"COMMAND\ngrep copilot-language-server\n{expected} --acp --stdio\n"
        with patch("subprocess.check_output", return_value=ps_output):
            result = find_binary_from_processes()
        assert result == expected


class TestFindBinaryFromJetbrains:
    """Filesystem-based discovery (ADR-006): checks expected path on disk."""

    def test_binary_exists_and_executable(self, tmp_path):
        """Returns path when binary exists and is executable."""
        expected = _compatible_path_pattern()
        with (
            patch(
                "acp_proxy.discovery._compatible_path_pattern",
                return_value=str(tmp_path / "binary"),
            ),
            patch("os.path.isfile", return_value=True),
            patch("os.access", return_value=True),
        ):
            result = find_binary_from_jetbrains()
        assert result == str(tmp_path / "binary")

    def test_binary_not_found_returns_none(self):
        """Returns None when binary doesn't exist on disk."""
        with (
            patch("os.path.isfile", return_value=False),
        ):
            result = find_binary_from_jetbrains()
        assert result is None


class TestFindBinaryFallback:
    """find_binary tries processes first, then filesystem (ADR-006)."""

    def test_processes_checked_first(self):
        """If processes find a binary, filesystem is not checked."""
        with (
            patch(
                "acp_proxy.discovery.find_binary_from_processes",
                return_value="/found/from/ps",
            ) as mock_ps,
            patch("acp_proxy.discovery.find_binary_from_jetbrains") as mock_jb,
        ):
            result = find_binary()
        assert result == "/found/from/ps"
        mock_ps.assert_called_once()
        mock_jb.assert_not_called()

    def test_falls_back_to_filesystem(self):
        """If processes find nothing, filesystem is checked."""
        with (
            patch("acp_proxy.discovery.find_binary_from_processes", return_value=None),
            patch(
                "acp_proxy.discovery.find_binary_from_jetbrains",
                return_value="/found/on/disk",
            ) as mock_jb,
        ):
            result = find_binary()
        assert result == "/found/on/disk"
        mock_jb.assert_called_once()

    def test_both_fail_returns_none(self):
        """If neither source finds a binary, returns None."""
        with (
            patch("acp_proxy.discovery.find_binary_from_processes", return_value=None),
            patch("acp_proxy.discovery.find_binary_from_jetbrains", return_value=None),
        ):
            result = find_binary()
        assert result is None
