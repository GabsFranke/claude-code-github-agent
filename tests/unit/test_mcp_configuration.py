"""Unit tests for MCP server configuration.

These tests validate that MCP servers are properly configured without
requiring actual API keys or network access.
"""

import os
import sys
from pathlib import Path

import pytest

# Add parent directory to path for shared imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from shared.sdk_factory import SDKOptionsBuilder  # noqa: E402


class TestMCPConfiguration:
    """Test MCP server configuration."""

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

    def test_github_actions_mcp_configuration_docker(self):
        """Test GitHub Actions MCP is now configured via .mcp.json auto-discovery."""
        # with_github_actions_mcp was removed: MCP servers are now declared in
        # .mcp.json written to each worktree by write_mcp_json(). This test
        # verifies the builder does NOT raise when .mcp.json-based flow is used.
        builder = SDKOptionsBuilder(cwd="/tmp")
        # No explicit MCP method needed — .mcp.json handles github-actions server
        options = builder.build()
        # github-actions is NOT in mcp_servers (it's in .mcp.json instead)
        assert "github-actions" not in options.mcp_servers

    def test_github_actions_mcp_configuration_local(self):
        """Test GitHub Actions MCP is now configured via .mcp.json auto-discovery."""
        # Same as docker test — method removed, coverage via write_mcp_json tests
        builder = SDKOptionsBuilder(cwd="/tmp")
        options = builder.build()
        assert "github-actions" not in options.mcp_servers

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

    def test_full_toolset_includes_mcp_tools(self):
        """Test that full toolset includes MCP tool patterns."""
        builder = SDKOptionsBuilder(cwd="/tmp")
        builder.with_full_toolset()
        options = builder.build()

        assert "mcp__github__*" in options.allowed_tools
        # github_actions uses underscore (matches .mcp.json server key)
        assert "mcp__github_actions__*" in options.allowed_tools
        assert "mcp__memory__memory_read" in options.allowed_tools

    def test_retrospector_toolset_includes_github_mcp(self):
        """Test that retrospector toolset includes GitHub MCP tools."""
        builder = SDKOptionsBuilder(cwd="/tmp")
        builder.with_retrospector_toolset()
        options = builder.build()

        assert "mcp__github__*" in options.allowed_tools
        # Should not include github-actions or memory
        assert "mcp__github-actions__*" not in options.allowed_tools
        assert "mcp__memory__memory_read" not in options.allowed_tools

    def test_memory_toolset_includes_memory_mcp(self):
        """Test that memory toolset includes memory MCP tools."""
        builder = SDKOptionsBuilder(cwd="/tmp")
        builder.with_memory_toolset()
        options = builder.build()

        assert "mcp__memory__*" in options.allowed_tools
        # Should not include github or github-actions
        assert "mcp__github__*" not in options.allowed_tools
        assert "mcp__github-actions__*" not in options.allowed_tools

    def test_builder_chaining(self):
        """Test that builder methods can be chained."""
        token = "test_token_123"
        repo = "owner/repo"

        builder = (
            SDKOptionsBuilder(cwd="/tmp")
            .with_sonnet()
            .with_github_mcp(token)
            .with_memory_mcp(repo)
            .with_full_toolset()
        )

        options = builder.build()

        # Verify configured MCP servers
        assert "github" in options.mcp_servers
        assert "memory" in options.mcp_servers
        # github-actions is now in .mcp.json, not programmatic mcp_servers
        assert "github-actions" not in options.mcp_servers

        # Verify model is set
        assert options.model is not None

        # Verify tools are configured
        assert len(options.allowed_tools) > 0

    def test_model_selection(self):
        """Test model selection methods."""
        # Test with_sonnet
        builder = SDKOptionsBuilder(cwd="/tmp").with_sonnet()
        options = builder.build()
        assert "sonnet" in options.model.lower() or options.model == os.getenv(
            "ANTHROPIC_DEFAULT_SONNET_MODEL", "claude-sonnet-4-20250514"
        )

        # Test with_haiku
        builder = SDKOptionsBuilder(cwd="/tmp").with_haiku()
        options = builder.build()
        assert "haiku" in options.model.lower() or options.model == os.getenv(
            "ANTHROPIC_DEFAULT_HAIKU_MODEL", "claude-haiku-4-5-20251001"
        )

        # Test with_model
        builder = SDKOptionsBuilder(cwd="/tmp").with_model("custom-model")
        options = builder.build()
        assert options.model == "custom-model"

    def test_github_actions_server_file_exists(self):
        """Test that GitHub Actions MCP server file exists in project."""
        project_root = Path(__file__).parent.parent.parent
        server_file = (
            project_root / "plugins/ci-failure-toolkit/servers/github_actions_server.py"
        )

        assert (
            server_file.exists()
        ), f"GitHub Actions MCP server file not found: {server_file}"
        assert server_file.is_file()

    def test_github_actions_server_is_executable_python(self):
        """Test that GitHub Actions MCP server is valid Python."""
        project_root = Path(__file__).parent.parent.parent
        server_file = (
            project_root / "plugins/ci-failure-toolkit/servers/github_actions_server.py"
        )

        if not server_file.exists():
            pytest.skip("Server file not found")

        # Check it has a shebang or is valid Python
        with open(server_file, encoding="utf-8") as f:
            first_line = f.readline()
            assert (
                first_line.startswith("#!/usr/bin/env python")
                or first_line.startswith("#!")
                or first_line.startswith('"""')
                or first_line.startswith("import")
            )

    def test_plugin_structure(self):
        """Test that plugin directory structure is correct."""
        project_root = Path(__file__).parent.parent.parent
        plugin_dir = project_root / "plugins/ci-failure-toolkit"

        if not plugin_dir.exists():
            pytest.skip("Plugin directory not found")

        # Check required directories
        assert (plugin_dir / "servers").exists()
        assert (plugin_dir / "tools").exists()

        # Check required files
        assert (plugin_dir / ".mcp.json").exists()
        assert (plugin_dir / "servers/github_actions_server.py").exists()

    def test_repository_context_injection(self):
        """Test that repository context is properly injected into system prompt."""
        builder = SDKOptionsBuilder(cwd="/tmp")

        claude_md = "# Repository Instructions\nThis is CLAUDE.md content"
        memory_index = "# Memory\nThis is memory content"

        builder.with_repository_context(claude_md=claude_md, memory_index=memory_index)
        options = builder.build()

        # Verify system prompt contains both contexts
        assert options.system_prompt is not None
        assert '<memory name="index.md">' in options.system_prompt
        assert "This is memory content" in options.system_prompt
        assert "<repository_context>" in options.system_prompt
        assert "This is CLAUDE.md content" in options.system_prompt

        # Verify memory comes before CLAUDE.md
        memory_pos = options.system_prompt.index('<memory name="index.md">')
        claude_pos = options.system_prompt.index("<repository_context>")
        assert memory_pos < claude_pos

    def test_repository_context_with_existing_system_prompt(self):
        """Test that repository context is prepended to existing system prompt."""
        builder = SDKOptionsBuilder(cwd="/tmp")

        workflow_context = "Workflow-specific instructions"
        claude_md = "Repository instructions"
        memory_index = "Memory content"

        builder.with_system_prompt(workflow_context)
        builder.with_repository_context(claude_md=claude_md, memory_index=memory_index)
        options = builder.build()

        # Verify all contexts are present
        assert options.system_prompt is not None
        assert "Memory content" in options.system_prompt
        assert "Repository instructions" in options.system_prompt
        assert "Workflow-specific instructions" in options.system_prompt

        # Verify order: memory -> CLAUDE.md -> workflow context
        memory_pos = options.system_prompt.index("Memory content")
        claude_pos = options.system_prompt.index("Repository instructions")
        workflow_pos = options.system_prompt.index("Workflow-specific instructions")
        assert memory_pos < claude_pos < workflow_pos

    def test_repository_context_with_none_values(self):
        """Test that None values are handled gracefully."""
        builder = SDKOptionsBuilder(cwd="/tmp")

        # Test with None values
        builder.with_repository_context(claude_md=None, memory_index=None)
        options = builder.build()

        # System prompt should be None if no context provided
        assert options.system_prompt is None

    def test_repository_context_with_empty_strings(self):
        """Test that empty strings are handled gracefully."""
        builder = SDKOptionsBuilder(cwd="/tmp")

        # Test with empty strings
        builder.with_repository_context(claude_md="", memory_index="")
        options = builder.build()

        # System prompt should be None if only empty strings provided
        assert options.system_prompt is None

    def test_repository_context_partial_injection(self):
        """Test that partial context (only memory or only CLAUDE.md) works."""
        # Test with only memory
        builder1 = SDKOptionsBuilder(cwd="/tmp")
        builder1.with_repository_context(memory_index="Memory only")
        options1 = builder1.build()

        assert options1.system_prompt is not None
        assert "Memory only" in options1.system_prompt
        assert "<repository_context>" not in options1.system_prompt

        # Test with only CLAUDE.md
        builder2 = SDKOptionsBuilder(cwd="/tmp")
        builder2.with_repository_context(claude_md="CLAUDE.md only")
        options2 = builder2.build()

        assert options2.system_prompt is not None
        assert "CLAUDE.md only" in options2.system_prompt
        assert '<memory name="index.md">' not in options2.system_prompt
