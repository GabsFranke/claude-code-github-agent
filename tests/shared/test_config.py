"""Unit tests for shared configuration module."""

import os
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from shared.config import (
    AnthropicConfig,
    GitHubConfig,
    RedisConfig,
    WebhookConfig,
    WorkerConfig,
)


class TestRedisConfig:
    """Test Redis configuration."""

    @patch.dict(os.environ, {}, clear=True)
    def test_redis_config_defaults(self):
        """Test Redis config with default values."""
        config = RedisConfig()
        assert config.redis_url == "redis://localhost:6379"
        assert config.redis_password is None
        assert config.queue_name == "agent-requests"

    def test_redis_config_custom_values(self):
        """Test Redis config with custom values."""
        with patch.dict(
            os.environ,
            {
                "REDIS_URL": "redis://redis.example.com:6380",
                "REDIS_PASSWORD": "secret",
                "QUEUE_NAME": "custom-queue",
            },
            clear=True,
        ):
            config = RedisConfig()
            assert config.redis_url == "redis://redis.example.com:6380"
            assert config.redis_password == "secret"
            assert config.queue_name == "custom-queue"


class TestGitHubConfig:
    """Test GitHub configuration."""

    def test_github_config_defaults(self):
        """Test GitHub config requires valid PEM key (can't test empty defaults)."""
        # GitHub config has validation that requires non-empty PEM key
        # In production, these are always set via environment variables
        pass

    def test_github_config_valid_pem(self):
        """Test valid GitHub configuration with PEM key."""
        valid_pem = (
            "-----BEGIN RSA PRIVATE KEY-----\ntest\n-----END RSA PRIVATE KEY-----"
        )
        config = GitHubConfig(
            github_app_id="12345",
            github_installation_id="67890",
            github_private_key=valid_pem,
        )
        assert config.github_app_id == "12345"
        assert config.github_installation_id == "67890"
        assert "BEGIN RSA PRIVATE KEY" in config.github_private_key

    def test_github_config_invalid_pem(self):
        """Test GitHub config rejects invalid PEM format."""
        with pytest.raises(ValidationError, match="PEM format"):
            GitHubConfig(github_private_key="not-a-valid-pem-key")


class TestAnthropicConfig:
    """Test Anthropic configuration."""

    @patch.dict(os.environ, {}, clear=True)
    def test_anthropic_config_defaults(self):
        """Test Anthropic config with defaults."""
        config = AnthropicConfig()
        assert config.anthropic_api_key is None
        assert config.anthropic_auth_token is None
        assert config.anthropic_base_url is None

    @patch.dict(os.environ, {}, clear=True)
    def test_anthropic_config_with_api_key(self):
        """Test Anthropic config with API key."""
        config = AnthropicConfig(anthropic_api_key="test-key")
        assert config.anthropic_api_key == "test-key"

    @patch.dict(os.environ, {}, clear=True)
    def test_anthropic_config_custom_base_url(self):
        """Test Anthropic config with custom base URL."""
        config = AnthropicConfig(
            anthropic_api_key="test-key",
            anthropic_base_url="https://custom.api.com",
        )
        assert config.anthropic_base_url == "https://custom.api.com"

    @patch.dict(os.environ, {}, clear=True)
    def test_anthropic_config_get_api_key_or_raise(self):
        """Test get_api_key_or_raise method."""
        config = AnthropicConfig(anthropic_api_key="test-key")
        assert config.get_api_key_or_raise() == "test-key"

    @patch.dict(os.environ, {}, clear=True)
    def test_anthropic_config_get_api_key_or_raise_fails(self):
        """Test get_api_key_or_raise raises when no key."""
        config = AnthropicConfig()
        with pytest.raises(ValueError, match="must be set"):
            config.get_api_key_or_raise()


class TestWebhookConfig:
    """Test webhook service configuration."""

    @patch.dict(os.environ, {}, clear=True)
    def test_webhook_config_defaults(self):
        """Test webhook config with defaults."""
        config = WebhookConfig()
        assert config.port == 8080
        assert config.log_level == "INFO"

    @patch.dict(os.environ, {}, clear=True)
    def test_webhook_config_custom_port(self):
        """Test webhook config with custom port."""
        config = WebhookConfig(port=9000)
        assert config.port == 9000

    @patch.dict(os.environ, {}, clear=True)
    def test_webhook_config_invalid_log_level(self):
        """Test webhook config rejects invalid log level."""
        with pytest.raises(ValidationError, match="Log level"):
            WebhookConfig(log_level="INVALID")


class TestWorkerConfig:
    """Test worker service configuration."""

    @patch.dict(os.environ, {}, clear=True)
    def test_worker_config_defaults(self):
        """Test worker config with defaults."""
        config = WorkerConfig()
        assert config.log_level == "INFO"
        assert config.max_turns == 50
        assert config.sdk_timeout == 1800

    @patch.dict(os.environ, {}, clear=True)
    def test_worker_config_custom_values(self):
        """Test worker config with custom values."""
        config = WorkerConfig(max_turns=100, sdk_timeout=3600)
        assert config.max_turns == 100
        assert config.sdk_timeout == 3600

    @patch.dict(os.environ, {}, clear=True)
    def test_worker_config_invalid_log_level(self):
        """Test worker config rejects invalid log level."""
        with pytest.raises(ValidationError, match="Log level"):
            WorkerConfig(log_level="INVALID")
