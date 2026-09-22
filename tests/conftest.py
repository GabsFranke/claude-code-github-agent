"""Pytest configuration and shared fixtures."""

import asyncio
import os
import sys
from collections.abc import Generator
from typing import Any
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

# CRITICAL: Mock claude_agent_sdk BEFORE any imports that use it
# This allows sandbox_executor tests to run without the SDK installed
mock_sdk = MagicMock()
mock_sdk.AssistantMessage = MagicMock
mock_sdk.ClaudeAgentOptions = MagicMock
mock_sdk.ClaudeSDKClient = MagicMock
mock_sdk.HookMatcher = MagicMock
mock_sdk.ResultMessage = MagicMock
mock_sdk.SystemMessage = MagicMock
mock_sdk.TextBlock = MagicMock
sys.modules["claude_agent_sdk"] = mock_sdk

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
    """Mock Redis client with all methods needed by SessionStore."""
    redis_mock = MagicMock(spec=Redis)
    redis_mock.ping = AsyncMock(return_value=True)
    redis_mock.publish = AsyncMock(return_value=1)
    redis_mock.pubsub = MagicMock()
    # Hash operations (needed by merged SessionStore)
    redis_mock.hset = AsyncMock(return_value=1)
    redis_mock.hgetall = AsyncMock(return_value={})
    redis_mock.hincrby = AsyncMock(return_value=1)
    # Lua scripting (needed for atomic subscriber count ops)
    redis_mock.eval = AsyncMock(return_value=1)
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


# Redis credentials for integration tests come from the environment so no
# real password is ever committed. Point TEST_REDIS_PASSWORD at your local
# Docker Redis (see .env.example); tests skip when Redis is unreachable.
TEST_REDIS_HOST = os.getenv("TEST_REDIS_HOST", "localhost")
TEST_REDIS_PORT = int(os.getenv("TEST_REDIS_PORT", "6379"))
TEST_REDIS_PASSWORD = os.getenv("TEST_REDIS_PASSWORD") or os.getenv("REDIS_PASSWORD")
TEST_REDIS_DB = int(os.getenv("TEST_REDIS_DB", "15"))
TEST_REDIS_URL = os.getenv(
    "TEST_REDIS_URL", f"redis://{TEST_REDIS_HOST}:{TEST_REDIS_PORT}"
)


@pytest_asyncio.fixture
async def redis_client():
    """Create real Redis client for integration testing."""
    client = Redis(
        host=TEST_REDIS_HOST,
        port=TEST_REDIS_PORT,
        password=TEST_REDIS_PASSWORD,
        db=TEST_REDIS_DB,
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


@pytest.fixture
def sample_session_token() -> str:
    """Sample UUID session token for SessionStore tests."""
    return "a1b2c3d4-e5f6-7890-abcd-ef1234567890"


@pytest.fixture
def sample_unified_session_data() -> dict[str, Any]:
    """Complete UnifiedSessionInfo data dict with all 21 fields.

    Fields breakdown (21 total):
      - 13 from SessionInfo: session_id, repo, thread_type, thread_id,
        workflow_name, ref, worktree_path, created_at, last_run,
        turn_count, status, summary, streaming_token
      - 8 from StreamingSessionData: installation_id, initial_query,
        conversation_config, transcript_path, run_count,
        session_proxy_url, issue_number, user
    """
    return {
        "session_id": "ses_abc123def456",
        "repo": "owner/test-repo",
        "thread_type": "issue",
        "thread_id": "42",
        "workflow_name": "test-workflow",
        "ref": "main",
        "worktree_path": "/tmp/worktrees/test-repo",
        "created_at": "2026-06-09T10:00:00+00:00",
        "last_run": "2026-06-09T12:30:00+00:00",
        "turn_count": 3,
        "status": "active",
        "summary": "Reviewed auth logic and fixed token refresh",
        "streaming_token": "strm_tok_abc123def456",
        "installation_id": "12345678",
        "initial_query": "/agent review this PR",
        "conversation_config": '{"persist":true,"ttl_hours":72,"max_turns":50}',
        "transcript_path": "/tmp/.claude/projects/owner/test-repo/session.jsonl",
        "run_count": 1,
        "session_proxy_url": "http://localhost:8000",
        "issue_number": "42",
        "user": "testuser",
    }


@pytest.fixture
def mock_redis_pubsub() -> MagicMock:
    """Mock Redis pub/sub for channel testing (SessionStreamBridge)."""
    pubsub = MagicMock()
    pubsub.subscribe = AsyncMock(return_value=None)
    pubsub.unsubscribe = AsyncMock(return_value=None)
    pubsub.get_message = AsyncMock(return_value=None)
    return pubsub


@pytest.fixture
def mock_session_store_v2(sample_unified_session_data: dict[str, Any]) -> MagicMock:
    """Mock SessionStore with AsyncMock for all merged store methods.

    Default return values are set for each method — override in individual
    tests where specific behavior is needed (e.g. error cases, specific data).
    """
    store = MagicMock()

    # ---- Session CRUD (from SessionStore) ----
    store.save_session = AsyncMock()
    store.get_session = AsyncMock(return_value=sample_unified_session_data)
    store.close_session = AsyncMock()
    store.expire_session = AsyncMock()
    store.list_sessions = AsyncMock(return_value=[sample_unified_session_data])
    store.update_summary = AsyncMock()
    store.increment_turn_count = AsyncMock()

    # ---- Streaming lifecycle ----
    store.create_session = AsyncMock()
    store.set_completed = AsyncMock()
    store.set_running = AsyncMock()
    store.delete_session = AsyncMock()
    store.set_ttl = AsyncMock()

    # ---- Session lookup ----
    store.find_session = AsyncMock(return_value=sample_unified_session_data)
    store.find_active_session = AsyncMock(return_value=sample_unified_session_data)

    # ---- Subscriber management ----
    store.increment_subscribers = AsyncMock(return_value=1)
    store.decrement_subscribers = AsyncMock(return_value=0)
    store.has_subscribers = AsyncMock(return_value=False)

    # ---- Inbox (user messages from browser) ----
    store.push_inbox_message = AsyncMock()
    store.pop_inbox_messages = AsyncMock(return_value=[])

    # ---- History / replay buffer ----
    store.get_history = AsyncMock(return_value=[])
    store.get_replay_buffer = AsyncMock(return_value=[])

    # ---- Metadata updates ----
    store.update_session_id = AsyncMock()
    store.update_transcript_path = AsyncMock()
    store.increment_run_count = AsyncMock(return_value=2)

    return store


@pytest_asyncio.fixture
async def clean_session_keys(redis_client):
    """Flush test session keys after integration tests.

    NOT autouse — only include in integration test classes that
    interact with real Redis session keys.

    Cleans: session:map:*, session:stream:*, session:inbox:*,
    session:subscribers:*, session:history:* patterns.
    """
    yield
    # Clean up all session-related key patterns
    patterns = [
        "session:map:*",
        "session:stream:*",
        "session:inbox:*",
        "session:subscribers:*",
        "session:history:*",
    ]
    for pattern in patterns:
        cursor = 0
        while True:
            cursor, keys = await redis_client.scan(
                cursor=cursor, match=pattern, count=100
            )
            if keys:
                await redis_client.delete(*keys)
            if cursor == 0:
                break
