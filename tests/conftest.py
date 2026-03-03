"""Pytest configuration and shared fixtures."""

import asyncio
import os
import sys
from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

# CRITICAL: Set test environment variables BEFORE any imports
# This allows worker.py and main.py to be imported without validation errors
os.environ.setdefault("GITHUB_APP_ID", "123456")
os.environ.setdefault("GITHUB_INSTALLATION_ID", "789012")
os.environ.setdefault(
    "GITHUB_PRIVATE_KEY",
    "-----BEGIN RSA PRIVATE KEY-----\ntest_key_content\n-----END RSA PRIVATE KEY-----",
)
os.environ.setdefault("GITHUB_WEBHOOK_SECRET", "test_webhook_secret")
os.environ.setdefault("ANTHROPIC_API_KEY", "test_anthropic_key")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")
os.environ.setdefault("LOG_LEVEL", "INFO")
os.environ.setdefault("PORT", "8000")

# CRITICAL: Mock dotenv BEFORE any imports that use pydantic-settings
sys.modules["dotenv"] = MagicMock()
sys.modules["dotenv.main"] = MagicMock()

from redis.asyncio import Redis  # noqa: E402


@pytest.fixture(scope="session")
def event_loop() -> Generator[asyncio.AbstractEventLoop, None, None]:
    """Create an event loop for the test session."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Clear environment variables for each test, but preserve test defaults."""
    # Store the test defaults
    test_defaults = {
        "GITHUB_APP_ID": "123456",
        "GITHUB_INSTALLATION_ID": "789012",
        "GITHUB_PRIVATE_KEY": "-----BEGIN RSA PRIVATE KEY-----\ntest_key_content\n-----END RSA PRIVATE KEY-----",
        "GITHUB_WEBHOOK_SECRET": "test_webhook_secret",
        "ANTHROPIC_API_KEY": "test_anthropic_key",
        "REDIS_URL": "redis://localhost:6379",
        "LOG_LEVEL": "INFO",
        "PORT": "8000",
    }

    # Clear all config vars
    config_vars = [
        "REDIS_URL",
        "REDIS_PASSWORD",
        "QUEUE_NAME",
        "GITHUB_APP_ID",
        "GITHUB_INSTALLATION_ID",
        "GITHUB_PRIVATE_KEY",
        "GITHUB_WEBHOOK_SECRET",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_VERTEX_PROJECT_ID",
        "ANTHROPIC_VERTEX_REGION",
        "LOG_LEVEL",
        "PORT",
    ]
    for var in config_vars:
        monkeypatch.delenv(var, raising=False)

    # Restore test defaults
    for key, value in test_defaults.items():
        monkeypatch.setenv(key, value)


@pytest.fixture
def mock_redis() -> MagicMock:
    """Mock Redis client."""
    redis_mock = MagicMock(spec=Redis)
    redis_mock.ping = AsyncMock(return_value=True)
    redis_mock.publish = AsyncMock(return_value=1)
    redis_mock.pubsub = MagicMock()
    return redis_mock


@pytest.fixture
def mock_httpx_client() -> AsyncMock:
    """Mock httpx AsyncClient."""
    client = AsyncMock()
    client.get = AsyncMock()
    client.post = AsyncMock()
    client.patch = AsyncMock()
    client.delete = AsyncMock()
    return client


@pytest_asyncio.fixture
async def redis_client():
    """Create real Redis client for integration testing."""
    # Use password from Docker setup
    client = Redis(
        host="localhost",
        port=6379,
        password="S5e_V7kdhPOI9DNJfBvYodxJgeQCG8Xup2mG3rBPwDU",
        db=15,
        decode_responses=True,
    )
    try:
        await client.ping()
    except Exception as e:
        pytest.skip(f"Redis not available: {e}")
    yield client
    await client.aclose()


@pytest.fixture
def sample_github_webhook_payload() -> dict:
    """Sample GitHub webhook payload for testing."""
    return {
        "action": "opened",
        "pull_request": {
            "number": 123,
            "title": "Test PR",
            "body": "Test description",
            "user": {"login": "testuser"},
            "head": {"ref": "feature-branch", "sha": "abc123"},
            "base": {"ref": "main", "repo": {"full_name": "owner/repo"}},
        },
        "repository": {
            "full_name": "owner/repo",
            "name": "repo",
            "owner": {"login": "owner"},
        },
        "installation": {"id": 12345},
    }


@pytest.fixture
def sample_issue_comment_payload() -> dict:
    """Sample GitHub issue comment webhook payload."""
    return {
        "action": "created",
        "issue": {
            "number": 456,
            "title": "Test Issue",
            "body": "Issue description",
            "user": {"login": "testuser"},
            "pull_request": {
                "url": "https://api.github.com/repos/owner/repo/pulls/456"
            },
        },
        "comment": {
            "id": 789,
            "body": "/agent review this PR",
            "user": {"login": "testuser"},
        },
        "repository": {"full_name": "owner/repo"},
        "installation": {"id": 12345},
    }
