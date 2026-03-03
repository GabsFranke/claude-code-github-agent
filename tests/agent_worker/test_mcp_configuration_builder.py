"""Unit tests for MCPConfigurationBuilder."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from services.agent_worker.processors.mcp_configuration_builder import (
    MCPConfigurationBuilder,
)


class TestMCPConfigurationBuilder:
    """Test MCPConfigurationBuilder class."""

    def test_initialization(self):
        """Test MCPConfigurationBuilder initialization."""
        token_manager = MagicMock()
        builder = MCPConfigurationBuilder(token_manager)

        assert builder.token_manager == token_manager

    @pytest.mark.asyncio
    async def test_create_mcp_config(self):
        """Test creating MCP configuration."""
        token_manager = AsyncMock()
        token_manager.get_token.return_value = "test-token"

        builder = MCPConfigurationBuilder(token_manager)
        config = await builder.create_mcp_config()

        assert "github" in config
        assert config["github"]["type"] == "http"
        assert config["github"]["url"] == "https://api.githubcopilot.com/mcp"
        assert config["github"]["headers"]["Authorization"] == "Bearer test-token"
        token_manager.get_token.assert_called_once()

    def test_create_agent_options(self):
        """Test creating agent options."""
        token_manager = MagicMock()
        builder = MCPConfigurationBuilder(token_manager)

        mcp_servers = {"github": {"type": "http"}}
        hooks = {"Stop": []}

        options = builder.create_agent_options(mcp_servers, hooks)

        assert options.mcp_servers == mcp_servers
        assert options.hooks == hooks
        assert options.max_turns == 50
        assert "Task" in options.allowed_tools
        assert "mcp__github__*" in options.allowed_tools
        assert options.permission_mode == "acceptEdits"
