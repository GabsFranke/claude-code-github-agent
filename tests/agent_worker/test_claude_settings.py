"""Tests for Claude settings configuration."""

import json
import os
from pathlib import Path
from unittest.mock import mock_open, patch

from services.agent_worker.config.claude_settings import setup_claude_settings


class TestSetupClaudeSettings:
    """Test setup_claude_settings function."""

    @patch("services.agent_worker.config.claude_settings.Path.home")
    @patch("services.agent_worker.config.claude_settings.Path.mkdir")
    @patch("builtins.open", new_callable=mock_open)
    def test_creates_settings_file(self, mock_file, mock_mkdir, mock_home):
        """Test that settings file is created."""
        mock_home.return_value = Path("/tmp/test_home")

        setup_claude_settings()

        # Verify directory creation
        mock_mkdir.assert_called_once_with(parents=True, exist_ok=True)

        # Verify file was written
        mock_file.assert_called()
        handle = mock_file()
        handle.write.assert_called()

    @patch("services.agent_worker.config.claude_settings.Path.home")
    @patch("pathlib.Path.exists")
    @patch("builtins.open", new_callable=mock_open, read_data='{"existing": "data"}')
    def test_preserves_existing_settings(self, mock_file, mock_exists, mock_home):
        """Test that existing settings are preserved."""
        mock_home.return_value = Path("/tmp/test_home")
        mock_exists.return_value = True

        setup_claude_settings()

        # Should read existing file
        calls = mock_file.call_args_list
        assert any("r" in str(call) or "encoding" in str(call) for call in calls)

    @patch("services.agent_worker.config.claude_settings.Path.home")
    @patch("pathlib.Path.exists")
    @patch("builtins.open", new_callable=mock_open)
    def test_sets_permissions(self, mock_file, mock_exists, mock_home):
        """Test that permissions are set correctly."""
        mock_home.return_value = Path("/tmp/test_home")
        mock_exists.return_value = False

        setup_claude_settings()

        # Get the written content
        handle = mock_file()
        written_data = "".join(call.args[0] for call in handle.write.call_args_list)
        settings = json.loads(written_data)

        assert "permissions" in settings
        assert settings["permissions"]["allow"] == ["Task", "mcp__github"]
        assert settings["permissions"]["deny"] == []
        assert settings["permissions"]["ask"] == []

    @patch("services.agent_worker.config.claude_settings.Path.home")
    @patch("pathlib.Path.exists")
    @patch("builtins.open", new_callable=mock_open)
    def test_enables_project_mcp_servers(self, mock_file, mock_exists, mock_home):
        """Test that project MCP servers are enabled."""
        mock_home.return_value = Path("/tmp/test_home")
        mock_exists.return_value = False

        setup_claude_settings()

        handle = mock_file()
        written_data = "".join(call.args[0] for call in handle.write.call_args_list)
        settings = json.loads(written_data)

        assert settings["enableAllProjectMcpServers"] is True

    @patch("services.agent_worker.config.claude_settings.Path.home")
    @patch("pathlib.Path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch.dict(
        os.environ,
        {
            "ANTHROPIC_BASE_URL": "https://custom.api.com",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "custom-haiku",
        },
    )
    def test_includes_custom_env_vars(self, mock_file, mock_exists, mock_home):
        """Test that custom env vars are included."""
        mock_home.return_value = Path("/tmp/test_home")
        mock_exists.return_value = False

        setup_claude_settings()

        handle = mock_file()
        written_data = "".join(call.args[0] for call in handle.write.call_args_list)
        settings = json.loads(written_data)

        assert "env" in settings
        assert settings["env"]["ANTHROPIC_BASE_URL"] == "https://custom.api.com"
        assert settings["env"]["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "custom-haiku"

    @patch("services.agent_worker.config.claude_settings.Path.home")
    @patch("pathlib.Path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch.dict(
        os.environ,
        {
            "LANGFUSE_PUBLIC_KEY": "pk_test",
            "LANGFUSE_SECRET_KEY": "sk_test",
            "LANGFUSE_HOST": "http://localhost:3000",
        },
    )
    def test_includes_langfuse_config(self, mock_file, mock_exists, mock_home):
        """Test that Langfuse config is included when keys present."""
        mock_home.return_value = Path("/tmp/test_home")
        mock_exists.return_value = False

        setup_claude_settings()

        handle = mock_file()
        written_data = "".join(call.args[0] for call in handle.write.call_args_list)
        settings = json.loads(written_data)

        assert "env" in settings
        assert settings["env"]["LANGFUSE_PUBLIC_KEY"] == "pk_test"
        assert settings["env"]["LANGFUSE_SECRET_KEY"] == "sk_test"
        assert settings["env"]["LANGFUSE_BASE_URL"] == "http://localhost:3000"
        assert settings["env"]["LANGSMITH_OTEL_ENABLED"] == "true"
        assert settings["env"]["LANGSMITH_OTEL_ONLY"] == "true"
        assert settings["env"]["LANGSMITH_TRACING"] == "true"

    @patch("services.agent_worker.config.claude_settings.Path.home")
    @patch("pathlib.Path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch.dict(os.environ, {"LANGFUSE_PUBLIC_KEY": "pk_test"}, clear=True)
    def test_no_langfuse_without_both_keys(self, mock_file, mock_exists, mock_home):
        """Test that Langfuse config is not included without both keys."""
        mock_home.return_value = Path("/tmp/test_home")
        mock_exists.return_value = False

        setup_claude_settings()

        handle = mock_file()
        written_data = "".join(call.args[0] for call in handle.write.call_args_list)
        settings = json.loads(written_data)

        # Should not have Langfuse config
        if "env" in settings:
            assert "LANGSMITH_OTEL_ENABLED" not in settings["env"]

    @patch("services.agent_worker.config.claude_settings.Path.home")
    @patch("pathlib.Path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch.dict(os.environ, {}, clear=True)
    def test_no_custom_env_when_not_set(self, mock_file, mock_exists, mock_home):
        """Test that custom env section is not added when no vars set."""
        mock_home.return_value = Path("/tmp/test_home")
        mock_exists.return_value = False

        setup_claude_settings()

        handle = mock_file()
        written_data = "".join(call.args[0] for call in handle.write.call_args_list)
        settings = json.loads(written_data)

        # Should not have env section or it should be empty
        assert "env" not in settings or settings["env"] == {}

    @patch("services.agent_worker.config.claude_settings.Path.home")
    @patch("pathlib.Path.exists")
    @patch("builtins.open", new_callable=mock_open, read_data="invalid json")
    def test_handles_corrupted_settings_file(self, mock_file, mock_exists, mock_home):
        """Test that corrupted settings file is handled gracefully."""
        mock_home.return_value = Path("/tmp/test_home")
        mock_exists.return_value = True

        # Should not raise exception
        setup_claude_settings()

        # Should still write new settings
        handle = mock_file()
        assert handle.write.called

    @patch("services.agent_worker.config.claude_settings.Path.home")
    @patch("pathlib.Path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch.dict(
        os.environ,
        {
            "ANTHROPIC_VERTEX_PROJECT_ID": "my-project",
            "ANTHROPIC_VERTEX_REGION": "us-central1",
        },
    )
    def test_includes_vertex_ai_config(self, mock_file, mock_exists, mock_home):
        """Test that Vertex AI config is included."""
        mock_home.return_value = Path("/tmp/test_home")
        mock_exists.return_value = False

        setup_claude_settings()

        handle = mock_file()
        written_data = "".join(call.args[0] for call in handle.write.call_args_list)
        settings = json.loads(written_data)

        assert "env" in settings
        assert settings["env"]["ANTHROPIC_VERTEX_PROJECT_ID"] == "my-project"
        assert settings["env"]["ANTHROPIC_VERTEX_REGION"] == "us-central1"

    @patch("services.agent_worker.config.claude_settings.Path.home")
    @patch("pathlib.Path.exists")
    @patch(
        "builtins.open", new_callable=mock_open, read_data='{"env": {"OLD": "value"}}'
    )
    def test_merges_with_existing_env(self, mock_file, mock_exists, mock_home):
        """Test that new env vars are merged with existing ones."""
        mock_home.return_value = Path("/tmp/test_home")
        mock_exists.return_value = True

        with patch.dict(os.environ, {"ANTHROPIC_BASE_URL": "https://new.api.com"}):
            setup_claude_settings()

        handle = mock_file()
        written_data = "".join(call.args[0] for call in handle.write.call_args_list)
        settings = json.loads(written_data)

        # Should have both old and new env vars
        assert "env" in settings
        assert settings["env"]["OLD"] == "value"
        assert settings["env"]["ANTHROPIC_BASE_URL"] == "https://new.api.com"
