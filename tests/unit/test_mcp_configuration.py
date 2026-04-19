"""Unit tests for MCP server configuration and auto-discovery.

These tests validate that MCP servers are properly configured without
requiring actual API keys or network access.
"""

import os
import sys
from pathlib import Path
from unittest.mock import patch

# Add parent directory to path for shared imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from shared.mcp_discovery import (  # noqa: E402
    MCPServerConfig,
    _interpolate_env_value,
    build_stdio_server_entry,
    discover_http_servers,
    discover_stdio_servers,
)
from shared.sdk_factory import SDKOptionsBuilder  # noqa: E402


class TestMCPDiscovery:
    """Test MCP server auto-discovery."""

    def test_discover_stdio_servers_finds_existing(self):
        """Test that discover_stdio_servers finds existing servers."""
        servers = discover_stdio_servers()
        server_names = [s.name for s in servers]

        assert "memory" in server_names
        assert "codebase_tools" in server_names

    def test_discover_stdio_servers_has_server_py(self):
        """Each discovered server must have a server.py path."""
        servers = discover_stdio_servers()
        for server in servers:
            assert server.server_path.endswith("server.py")
            assert os.path.basename(server.server_path) == "server.py"

    def test_interpolate_env_value_resolves_vars(self):
        """Test that ${VAR} patterns are resolved from environment."""
        with patch.dict(os.environ, {"MY_VAR": "hello"}):
            result = _interpolate_env_value("${MY_VAR}")
            assert result == "hello"

    def test_interpolate_env_value_returns_none_for_unset(self):
        """Test that unset variables cause None return."""
        with patch.dict(os.environ, {}, clear=True):
            # Ensure the var is not set
            os.environ.pop("DEFINITELY_NOT_SET_VAR", None)
            result = _interpolate_env_value("${DEFINITELY_NOT_SET_VAR}")
            assert result is None

    def test_interpolate_env_value_plain_string(self):
        """Plain strings without ${} pass through unchanged."""
        result = _interpolate_env_value("plain_value")
        assert result == "plain_value"

    def test_build_stdio_server_entry_skips_unresolved_env(self):
        """Server with unresolved env vars is skipped (returns None)."""
        config = MCPServerConfig(
            name="test_server",
            server_path="/tmp/test/server.py",
            env={"MISSING_VAR": "${DEFINITELY_NOT_SET_12345}"},
        )
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("DEFINITELY_NOT_SET_12345", None)
            result = build_stdio_server_entry(config)
            assert result is None

    def test_build_stdio_server_entry_includes_defaults(self):
        """Default env vars (PYTHONPATH, GITHUB_REPOSITORY, REPO_PATH) are set."""
        config = MCPServerConfig(
            name="test_server",
            server_path="/tmp/test/server.py",
            env={},
        )
        result = build_stdio_server_entry(
            config, app_root="/app", repo="owner/repo", worktree_path="/workspace"
        )
        assert result is not None
        assert result["env"]["PYTHONPATH"] == "/app"
        assert result["env"]["GITHUB_REPOSITORY"] == "owner/repo"
        assert result["env"]["REPO_PATH"] == "/workspace"

    def test_discover_http_servers_reads_config(self):
        """HTTP servers are discovered from mcp_servers/http.json."""
        servers = discover_http_servers()
        # May or may not have entries depending on env vars
        assert isinstance(servers, list)

    def test_discover_http_servers_skips_unresolved(self):
        """HTTP servers with unresolved env vars are skipped."""
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("GITHUB_TOKEN", None)
            servers = discover_http_servers()
            # github server should be skipped without GITHUB_TOKEN
            names = [name for name, _ in servers]
            assert "github" not in names


class TestSDKOptionsBuilderMCP:
    """Test SDKOptionsBuilder MCP methods."""

    def test_github_mcp_configuration(self):
        """Test that GitHub MCP server is configured correctly."""
        token = "test_token_123"
        builder = SDKOptionsBuilder(cwd="/tmp")
        builder.with_github_mcp(token)
        options = builder.build()

        assert "github" in options.mcp_servers
        github_config = options.mcp_servers["github"]
        assert github_config["type"] == "http"
        assert github_config["url"] == "https://api.githubcopilot.com/mcp"
        assert "Authorization" in github_config["headers"]
        assert github_config["headers"]["Authorization"] == f"Bearer {token}"

    def test_memory_mcp_configuration(self):
        """Test memory MCP server configuration."""
        repo = "owner/repo"
        builder = SDKOptionsBuilder(cwd="/tmp")
        builder.with_memory_mcp(repo)
        options = builder.build()

        assert "memory" in options.mcp_servers
        memory_config = options.mcp_servers["memory"]
        assert memory_config["type"] == "stdio"
        assert memory_config["command"] == "python3"
        assert memory_config["args"] == ["/app/mcp_servers/memory/server.py"]
        assert memory_config["env"]["GITHUB_REPOSITORY"] == repo
        assert memory_config["env"]["PYTHONPATH"] == "/app"

    def test_auto_discovered_mcp_servers_registers_stdio(self):
        """Auto-discovery registers stdio servers from mcp_servers/."""
        builder = SDKOptionsBuilder(cwd="/tmp")
        builder.with_auto_discovered_mcp_servers(
            repo="owner/repo", worktree_path="/workspace"
        )

        assert "memory" in builder._mcp_servers
        assert "codebase_tools" in builder._mcp_servers

    def test_auto_discovered_mcp_servers_populates_names(self):
        """Auto-discovery populates _discovered_server_names."""
        builder = SDKOptionsBuilder(cwd="/tmp")
        builder.with_auto_discovered_mcp_servers(
            repo="owner/repo", worktree_path="/workspace"
        )

        assert "memory" in builder._discovered_server_names
        assert "codebase_tools" in builder._discovered_server_names

    def test_full_toolset_generates_wildcard_patterns(self):
        """Full toolset generates mcp__<name>__* for discovered servers."""
        builder = SDKOptionsBuilder(cwd="/tmp")
        builder.with_auto_discovered_mcp_servers(
            repo="owner/repo", worktree_path="/workspace"
        )
        builder.with_full_toolset()
        options = builder.build()

        for name in builder._discovered_server_names:
            assert f"mcp__{name}__*" in options.allowed_tools

    def test_retrospector_toolset_includes_github_mcp(self):
        """Test that retrospector toolset includes GitHub MCP tools."""
        builder = SDKOptionsBuilder(cwd="/tmp")
        builder.with_retrospector_toolset()
        options = builder.build()

        assert "mcp__github__*" in options.allowed_tools
        assert "mcp__memory__*" not in options.allowed_tools

    def test_memory_toolset_includes_memory_mcp(self):
        """Test that memory toolset includes memory MCP tools."""
        builder = SDKOptionsBuilder(cwd="/tmp")
        builder.with_memory_toolset()
        options = builder.build()

        assert "mcp__memory__*" in options.allowed_tools
        assert "mcp__github__*" not in options.allowed_tools

    def test_builder_chaining(self):
        """Test that builder methods can be chained."""
        repo = "owner/repo"

        builder = (
            SDKOptionsBuilder(cwd="/tmp")
            .with_sonnet()
            .with_auto_discovered_mcp_servers(repo=repo, worktree_path="/workspace")
            .with_full_toolset()
        )

        options = builder.build()

        assert "memory" in options.mcp_servers
        assert options.model is not None
        assert len(options.allowed_tools) > 0


class TestModelSelection:
    """Test model selection methods."""

    def test_sonnet_model(self):
        builder = SDKOptionsBuilder(cwd="/tmp").with_sonnet()
        options = builder.build()
        assert "sonnet" in options.model.lower() or options.model == os.getenv(
            "ANTHROPIC_DEFAULT_SONNET_MODEL", "claude-sonnet-4-20250514"
        )

    def test_haiku_model(self):
        builder = SDKOptionsBuilder(cwd="/tmp").with_haiku()
        options = builder.build()
        assert "haiku" in options.model.lower() or options.model == os.getenv(
            "ANTHROPIC_DEFAULT_HAIKU_MODEL", "claude-haiku-4-5-20251001"
        )

    def test_custom_model(self):
        builder = SDKOptionsBuilder(cwd="/tmp").with_model("custom-model")
        options = builder.build()
        assert options.model == "custom-model"


class TestMCPServerFiles:
    """Test that MCP server files exist in expected locations."""

    def test_github_actions_server_in_mcp_servers(self):
        """GitHub Actions MCP server now lives in mcp_servers/."""
        project_root = Path(__file__).parent.parent.parent
        server_file = project_root / "mcp_servers/github_actions/server.py"
        assert server_file.exists(), f"Server file not found: {server_file}"

    def test_memory_server_exists(self):
        project_root = Path(__file__).parent.parent.parent
        assert (project_root / "mcp_servers/memory/server.py").exists()

    def test_codebase_tools_server_exists(self):
        project_root = Path(__file__).parent.parent.parent
        assert (project_root / "mcp_servers/codebase_tools/server.py").exists()

    def test_http_json_exists(self):
        project_root = Path(__file__).parent.parent.parent
        assert (project_root / "mcp_servers/http.json").exists()

    def test_github_actions_mcp_json_exists(self):
        project_root = Path(__file__).parent.parent.parent
        assert (project_root / "mcp_servers/github_actions/mcp.json").exists()


class TestRepositoryContext:
    """Test repository context injection."""

    def test_repository_context_injection(self):
        builder = SDKOptionsBuilder(cwd="/tmp")
        builder.with_repository_context(
            claude_md="# Repo\nContent", memory_index="# Memory\nContent"
        )
        options = builder.build()

        assert options.system_prompt is not None
        assert '<memory name="index.md">' in options.system_prompt
        assert "<repository_context>" in options.system_prompt

    def test_repository_context_with_existing_system_prompt(self):
        builder = SDKOptionsBuilder(cwd="/tmp")
        builder.with_system_prompt("Workflow context")
        builder.with_repository_context(claude_md="Repo", memory_index="Memory")
        options = builder.build()

        assert "Memory" in options.system_prompt
        assert "Repo" in options.system_prompt
        assert "Workflow context" in options.system_prompt

    def test_repository_context_with_none_values(self):
        builder = SDKOptionsBuilder(cwd="/tmp")
        builder.with_repository_context(claude_md=None, memory_index=None)
        options = builder.build()
        assert options.system_prompt is None

    def test_repository_context_with_empty_strings(self):
        builder = SDKOptionsBuilder(cwd="/tmp")
        builder.with_repository_context(claude_md="", memory_index="")
        options = builder.build()
        assert options.system_prompt is None

    def test_repository_context_partial_injection(self):
        builder1 = SDKOptionsBuilder(cwd="/tmp")
        builder1.with_repository_context(memory_index="Memory only")
        assert "Memory only" in builder1.build().system_prompt

        builder2 = SDKOptionsBuilder(cwd="/tmp")
        builder2.with_repository_context(claude_md="CLAUDE.md only")
        assert "CLAUDE.md only" in builder2.build().system_prompt
