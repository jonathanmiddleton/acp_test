"""Tests for user configuration loading, subprocess environment, and context injection."""

from __future__ import annotations

import json
import os
from unittest.mock import patch

from acp_proxy.config import (
    build_subprocess_env,
    compose_system_prompt,
    config_path,
    ensure_default_config,
    estimate_tokens,
    get_context_files,
    load_config,
    load_context_files,
)


class TestLoadConfig:
    """load_config reads ~/.acp_proxy/config.json."""

    def test_creates_default_when_no_file(self, tmp_path):
        """When no config exists, load_config creates a default and returns it."""
        cfg_file = tmp_path / ".acp_proxy" / "config.json"
        with (
            patch("acp_proxy.config.config_path", return_value=str(cfg_file)),
            patch("acp_proxy.config.config_dir", return_value=str(cfg_file.parent)),
        ):
            cfg = load_config()
        assert cfg_file.exists()
        assert "context_files" in cfg
        assert "https_proxy" in cfg

    def test_loads_valid_json(self, tmp_path):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"https_proxy": "http://proxy:8080"}))
        with patch("acp_proxy.config.config_path", return_value=str(cfg_file)):
            cfg = load_config()
        assert cfg == {"https_proxy": "http://proxy:8080"}

    def test_returns_empty_on_malformed_json(self, tmp_path):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text("not json {{{")
        with patch("acp_proxy.config.config_path", return_value=str(cfg_file)):
            assert load_config() == {}

    def test_returns_empty_when_not_object(self, tmp_path):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps(["a", "list"]))
        with patch("acp_proxy.config.config_path", return_value=str(cfg_file)):
            assert load_config() == {}

    def test_preserves_all_keys(self, tmp_path):
        data = {
            "https_proxy": "http://proxy:8080",
            "no_proxy": "localhost",
            "custom_key": "value",
        }
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps(data))
        with patch("acp_proxy.config.config_path", return_value=str(cfg_file)):
            assert load_config() == data


class TestConfigPath:
    """config_path returns a path under the user's home."""

    def test_under_home_directory(self):
        path = config_path()
        home = os.path.expanduser("~")
        assert path.startswith(home)
        assert ".acp_proxy" in path
        assert path.endswith("config.json")


class TestEnsureDefaultConfig:
    """ensure_default_config creates the config file on first run."""

    def test_creates_file_when_missing(self, tmp_path):
        cfg_file = tmp_path / ".acp_proxy" / "config.json"
        with (
            patch("acp_proxy.config.config_path", return_value=str(cfg_file)),
            patch("acp_proxy.config.config_dir", return_value=str(cfg_file.parent)),
        ):
            ensure_default_config()
        assert cfg_file.exists()
        data = json.loads(cfg_file.read_text())
        assert "context_files" in data
        assert "https_proxy" in data

    def test_does_not_overwrite_existing(self, tmp_path):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"custom": "value"}))
        with patch("acp_proxy.config.config_path", return_value=str(cfg_file)):
            ensure_default_config()
        data = json.loads(cfg_file.read_text())
        assert data == {"custom": "value"}


class TestBuildSubprocessEnv:
    """build_subprocess_env merges config proxy settings into the environment."""

    def test_applies_proxy_from_config(self):
        cfg = {"https_proxy": "http://proxy:8080"}
        with patch.dict(os.environ, {}, clear=True):
            # Preserve PATH so the env is usable
            env = build_subprocess_env(cfg)
        assert env["HTTPS_PROXY"] == "http://proxy:8080"
        assert env["https_proxy"] == "http://proxy:8080"

    def test_env_var_takes_precedence_over_config(self):
        cfg = {"https_proxy": "http://from-config:8080"}
        with patch.dict(
            os.environ, {"HTTPS_PROXY": "http://from-env:9090"}, clear=True
        ):
            env = build_subprocess_env(cfg)
        assert env["HTTPS_PROXY"] == "http://from-env:9090"

    def test_no_config_returns_current_env(self):
        env = build_subprocess_env({})
        # Should be a copy of os.environ, not os.environ itself
        assert env is not os.environ
        assert env.get("PATH") == os.environ.get("PATH")

    def test_none_config_loads_from_disk(self, tmp_path):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"http_proxy": "http://disk-proxy:3128"}))
        with (
            patch("acp_proxy.config.config_path", return_value=str(cfg_file)),
            patch.dict(os.environ, {}, clear=True),
        ):
            env = build_subprocess_env(None)
        assert env["HTTP_PROXY"] == "http://disk-proxy:3128"
        assert env["http_proxy"] == "http://disk-proxy:3128"

    def test_all_proxy_vars_applied(self):
        cfg = {
            "http_proxy": "http://proxy:3128",
            "https_proxy": "http://proxy:3129",
            "no_proxy": "localhost,127.0.0.1",
        }
        with patch.dict(os.environ, {}, clear=True):
            env = build_subprocess_env(cfg)
        assert env["HTTP_PROXY"] == "http://proxy:3128"
        assert env["http_proxy"] == "http://proxy:3128"
        assert env["HTTPS_PROXY"] == "http://proxy:3129"
        assert env["https_proxy"] == "http://proxy:3129"
        assert env["NO_PROXY"] == "localhost,127.0.0.1"
        assert env["no_proxy"] == "localhost,127.0.0.1"

    def test_non_string_values_in_config_ignored(self):
        cfg = {"https_proxy": 12345, "http_proxy": "http://proxy:3128"}
        with patch.dict(os.environ, {}, clear=True):
            env = build_subprocess_env(cfg)
        assert "HTTPS_PROXY" not in env
        assert env["HTTP_PROXY"] == "http://proxy:3128"

    def test_case_insensitive_config_keys(self):
        cfg = {"HTTPS_PROXY": "http://proxy:8080"}
        with patch.dict(os.environ, {}, clear=True):
            env = build_subprocess_env(cfg)
        assert env["HTTPS_PROXY"] == "http://proxy:8080"
        assert env["https_proxy"] == "http://proxy:8080"

    def test_does_not_modify_os_environ(self):
        cfg = {"https_proxy": "http://proxy:8080"}
        original_env = dict(os.environ)
        build_subprocess_env(cfg)
        assert dict(os.environ) == original_env


class TestEstimateTokens:
    """estimate_tokens provides rough character-based estimates."""

    def test_empty_string(self):
        assert estimate_tokens("") == 0

    def test_proportional_to_length(self):
        short = estimate_tokens("hello")
        long = estimate_tokens("hello " * 100)
        assert long > short

    def test_approximate_ratio(self):
        # 400 chars should be ~100 tokens at 4 chars/token
        assert estimate_tokens("x" * 400) == 100


class TestGetContextFiles:
    """get_context_files reads config or falls back to defaults."""

    def test_default_includes_common_convention_files(self):
        result = get_context_files({})
        assert "AGENTS.md" in result
        assert "CLAUDE.md" in result
        assert "COPILOT-INSTRUCTIONS.md" in result

    def test_reads_from_config(self):
        cfg = {"context_files": ["AGENTS.md", "CLAUDE.md", "CODING_STANDARDS.md"]}
        assert get_context_files(cfg) == [
            "AGENTS.md",
            "CLAUDE.md",
            "CODING_STANDARDS.md",
        ]

    def test_non_list_falls_back_to_default(self):
        cfg = {"context_files": "AGENTS.md"}
        result = get_context_files(cfg)
        assert "AGENTS.md" in result
        assert isinstance(result, list)

    def test_non_string_entries_filtered(self):
        cfg = {"context_files": ["AGENTS.md", 42, "CLAUDE.md"]}
        assert get_context_files(cfg) == ["AGENTS.md", "CLAUDE.md"]

    def test_empty_list_respected(self):
        """User can explicitly disable context injection with an empty list."""
        cfg = {"context_files": []}
        assert get_context_files(cfg) == []


class TestLoadContextFiles:
    """load_context_files reads markdown from the workspace directory."""

    def test_loads_existing_file(self, tmp_path):
        (tmp_path / "AGENTS.md").write_text("# Project\nThis is a project.")
        result = load_context_files(str(tmp_path))
        assert len(result) == 1
        assert result[0][0] == "AGENTS.md"
        assert "This is a project" in result[0][1]

    def test_skips_missing_files(self, tmp_path):
        result = load_context_files(str(tmp_path))
        assert result == []

    def test_skips_empty_files(self, tmp_path):
        (tmp_path / "AGENTS.md").write_text("   \n  ")
        result = load_context_files(str(tmp_path))
        assert result == []

    def test_loads_multiple_files_in_order(self, tmp_path):
        (tmp_path / "AGENTS.md").write_text("agents content")
        (tmp_path / "CLAUDE.md").write_text("claude content")
        cfg = {"context_files": ["AGENTS.md", "CLAUDE.md"]}
        result = load_context_files(str(tmp_path), cfg)
        assert len(result) == 2
        assert result[0][0] == "AGENTS.md"
        assert result[1][0] == "CLAUDE.md"

    def test_partial_files_loaded(self, tmp_path):
        """Only files that exist are loaded; missing ones are skipped."""
        (tmp_path / "AGENTS.md").write_text("agents content")
        cfg = {"context_files": ["AGENTS.md", "MISSING.md", "ALSO_MISSING.md"]}
        result = load_context_files(str(tmp_path), cfg)
        assert len(result) == 1
        assert result[0][0] == "AGENTS.md"


class TestComposeSystemPrompt:
    """compose_system_prompt merges explicit prompt with workspace context."""

    def test_explicit_only(self, tmp_path):
        result = compose_system_prompt("You are helpful.", str(tmp_path))
        assert result == "You are helpful."

    def test_context_only(self, tmp_path):
        (tmp_path / "AGENTS.md").write_text("# Project\nDetails here.")
        result = compose_system_prompt(None, str(tmp_path))
        assert "# AGENTS.md" in result
        assert "Details here." in result

    def test_explicit_comes_first(self, tmp_path):
        (tmp_path / "AGENTS.md").write_text("agents content")
        result = compose_system_prompt("explicit instructions", str(tmp_path))
        explicit_pos = result.index("explicit instructions")
        agents_pos = result.index("agents content")
        assert explicit_pos < agents_pos

    def test_returns_none_when_nothing_available(self, tmp_path):
        result = compose_system_prompt(None, str(tmp_path))
        assert result is None

    def test_separator_between_parts(self, tmp_path):
        (tmp_path / "AGENTS.md").write_text("agents content")
        result = compose_system_prompt("explicit", str(tmp_path))
        assert "---" in result

    def test_multiple_context_files(self, tmp_path):
        (tmp_path / "AGENTS.md").write_text("agents")
        (tmp_path / "CLAUDE.md").write_text("claude")
        cfg = {"context_files": ["AGENTS.md", "CLAUDE.md"]}
        result = compose_system_prompt(None, str(tmp_path), cfg)
        assert "# AGENTS.md" in result
        assert "# CLAUDE.md" in result
        # Order preserved
        assert result.index("agents") < result.index("claude")

    def test_empty_context_files_config(self, tmp_path):
        """Empty context_files list means no auto-injection."""
        (tmp_path / "AGENTS.md").write_text("should not appear")
        cfg = {"context_files": []}
        result = compose_system_prompt(None, str(tmp_path), cfg)
        assert result is None

    def test_explicit_prompt_with_empty_context_files(self, tmp_path):
        cfg = {"context_files": []}
        result = compose_system_prompt("explicit only", str(tmp_path), cfg)
        assert result == "explicit only"
