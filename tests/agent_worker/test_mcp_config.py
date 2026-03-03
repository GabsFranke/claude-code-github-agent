"""Unit tests for MCP configuration."""

import json
from pathlib import Path
from unittest.mock import mock_open, patch

from services.agent_worker.config.mcp_config import setup_mcp_config


class TestMCPConfig:
    """Test MCP configuration setup."""

    @patch("services.agent_worker.config.mcp_config.Path.home")
    @patch("builtins.open", new_callable=mock_open)
    @patch("pathlib.Path.mkdir")
    def test_setup_mcp_config_creates_file(self, mock_mkdir, mock_file, mock_home):
        """Test that setup_mcp_config creates MCP config file."""
        mock_home.return_value = Path("/home/user")
        token = "test-github-token"

        setup_mcp_config(token)

        # Verify directory creation
        mock_mkdir.assert_called_once_with(parents=True, exist_ok=True)

        # Verify file was opened for writing
        expected_path = Path("/home/user") / ".claude" / "mcp.json"
        mock_file.assert_called_once_with(expected_path, "w", encoding="utf-8")

        # Verify correct JSON was written
        handle = mock_file()
        written_content = "".join(call.args[0] for call in handle.write.call_args_list)
        config = json.loads(written_content)

        assert "mcpServers" in config
        assert "github" in config["mcpServers"]
        assert config["mcpServers"]["github"]["type"] == "http"
        assert (
            config["mcpServers"]["github"]["url"] == "https://api.githubcopilot.com/mcp"
        )
        assert (
            config["mcpServers"]["github"]["headers"]["Authorization"]
            == f"Bearer {token}"
        )

    @patch("services.agent_worker.config.mcp_config.Path.home")
    @patch("builtins.open", new_callable=mock_open)
    @patch("pathlib.Path.mkdir")
    def test_setup_mcp_config_with_different_token(
        self, mock_mkdir, mock_file, mock_home
    ):
        """Test setup_mcp_config with different token."""
        mock_home.return_value = Path("/home/user")
        token = "another-token-12345"

        setup_mcp_config(token)

        handle = mock_file()
        written_content = "".join(call.args[0] for call in handle.write.call_args_list)
        config = json.loads(written_content)

        assert (
            config["mcpServers"]["github"]["headers"]["Authorization"]
            == f"Bearer {token}"
        )

    @patch("services.agent_worker.config.mcp_config.Path.home")
    @patch("builtins.open", new_callable=mock_open)
    @patch("pathlib.Path.mkdir")
    def test_setup_mcp_config_json_format(self, mock_mkdir, mock_file, mock_home):
        """Test that MCP config is properly formatted JSON."""
        mock_home.return_value = Path("/home/user")
        token = "test-token"

        setup_mcp_config(token)

        handle = mock_file()
        written_content = "".join(call.args[0] for call in handle.write.call_args_list)

        # Should be valid JSON
        config = json.loads(written_content)

        # Verify structure
        assert isinstance(config, dict)
        assert isinstance(config["mcpServers"], dict)
        assert isinstance(config["mcpServers"]["github"], dict)
        assert isinstance(config["mcpServers"]["github"]["headers"], dict)
