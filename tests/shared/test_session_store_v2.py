"""Tests for SessionStore — UnifiedSessionInfo, SessionStatus, SessionStoreConfig,
public Redis key builders, AND SessionStore CRUD operations.

TDD: Tests are written BEFORE the SessionStore class exists (RED phase).
"""

import json
import warnings
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from shared.constants import (
    DEFAULT_SESSION_TTL_SECONDS,
    history_key,
    inbox_key,
    session_cleanup_key,
    session_key,
    session_pattern,
    streaming_lookup_key,
    streaming_session_key,
    subscribers_key,
)
from shared.session_store import (
    SessionStatus,
    SessionStoreConfig,
    UnifiedSessionInfo,
)

# ============================================================================
# Helpers
# ============================================================================


def _all_fields_dict() -> dict:
    """Return a dict covering ALL 21 fields from SessionInfo + StreamingSessionData."""
    return {
        "session_id": "abc",
        "repo": "owner/repo",
        "thread_type": "issue",
        "thread_id": "1",
        "workflow_name": "test-workflow",
        "ref": "main",
        "worktree_path": "/tmp/worktree",
        "created_at": "2024-01-01T00:00:00Z",
        "last_run": "2024-01-01T00:00:00Z",
        "turn_count": 0,
        "status": "active",
        "summary": None,
        "streaming_token": "tok-123",
        "installation_id": "123",
        "initial_query": "hello",
        "conversation_config": "{}",
        "transcript_path": "/tmp/t.jsonl",
        "run_count": 1,
        "session_proxy_url": "http://x",
        "issue_number": "1",
        "user": "test",
    }


def _make_redis_v2():
    """Create a mock Redis client with all async methods needed by SessionStore.

    Mirrors the combined needs of the unified SessionStore.
    """
    redis = MagicMock()
    pipeline = MagicMock()
    pipeline.execute = AsyncMock(return_value=[])
    pipeline.hset = MagicMock()
    pipeline.expire = MagicMock()
    pipeline.setex = MagicMock()
    redis.pipeline = MagicMock(return_value=pipeline)
    redis.hset = AsyncMock(return_value=1)
    redis.hsetnx = AsyncMock(return_value=1)
    redis.hincrby = AsyncMock(return_value=1)
    redis.hgetall = AsyncMock(return_value={})
    redis.hget = AsyncMock(return_value=None)
    redis.get = AsyncMock(return_value=None)
    redis.setex = AsyncMock(return_value=True)
    redis.delete = AsyncMock(return_value=1)
    redis.expire = AsyncMock(return_value=True)
    redis.exists = AsyncMock(return_value=1)
    redis.eval = AsyncMock(return_value=1)
    redis.scan = AsyncMock(return_value=(0, []))
    redis.rpush = AsyncMock(return_value=1)
    redis.lrange = AsyncMock(return_value=[])
    return redis


# ============================================================================
# SessionStatus enum
# ============================================================================


class TestSessionStatus:
    """Validate the SessionStatus enum values."""

    def test_all_status_values_exist(self):
        assert SessionStatus.active == "active"
        assert SessionStatus.running == "running"
        assert SessionStatus.completed == "completed"
        assert SessionStatus.error == "error"
        assert SessionStatus.expired == "expired"

    def test_status_enum_is_string_enum(self):
        assert SessionStatus("active") == SessionStatus.active


# ============================================================================
# UnifiedSessionInfo model
# ============================================================================


class TestUnifiedModelAllFields:
    """Create UnifiedSessionInfo with ALL 21 fields — verify no silent drops."""

    def test_all_fields_accepted_and_roundtrip(self):
        data = _all_fields_dict()
        obj = UnifiedSessionInfo(**data)
        dumped = obj.model_dump()
        assert len(dumped) == 21, (
            f"Expected 21 fields, got {len(dumped)}. "
            f"Missing: {set(data) - set(dumped)}. Extra: {set(dumped) - set(data)}"
        )

    @pytest.mark.parametrize(
        "field_name",
        [
            "session_id",
            "repo",
            "thread_type",
            "thread_id",
            "workflow_name",
            "ref",
            "worktree_path",
            "created_at",
            "last_run",
            "turn_count",
            "status",
            "streaming_token",
            "installation_id",
            "initial_query",
            "conversation_config",
            "transcript_path",
            "run_count",
            "session_proxy_url",
            "issue_number",
            "user",
        ],
    )
    def test_every_explicit_field_present_in_output(self, field_name):
        data = _all_fields_dict()
        obj = UnifiedSessionInfo(**data)
        dumped = obj.model_dump()
        assert field_name in dumped, f"Field '{field_name}' missing from output"

    def test_summary_nullable(self):
        data = _all_fields_dict()
        data["summary"] = None
        obj = UnifiedSessionInfo(**data)
        assert obj.summary is None

    def test_streaming_token_nullable(self):
        data = _all_fields_dict()
        data["streaming_token"] = None
        obj = UnifiedSessionInfo(**data)
        assert obj.streaming_token is None

    def test_default_values(self):
        minimal = {
            "session_id": "abc",
            "repo": "owner/repo",
            "thread_type": "issue",
            "thread_id": "1",
            "workflow_name": "test",
            "ref": "main",
            "worktree_path": "/tmp",
            "created_at": "2024-01-01T00:00:00Z",
            "last_run": "2024-01-01T00:00:00Z",
            "status": "active",
            "installation_id": "123",
            "initial_query": "hello",
            "conversation_config": "{}",
            "session_proxy_url": "http://x",
            "issue_number": "1",
            "user": "test",
        }
        obj = UnifiedSessionInfo(**minimal)
        assert obj.turn_count == 0
        assert obj.run_count == 1
        assert obj.transcript_path == ""
        assert obj.streaming_token is None
        assert obj.summary is None

    def test_model_dump_json_includes_all_fields(self):
        data = _all_fields_dict()
        obj = UnifiedSessionInfo(**data)
        json_str = obj.model_dump_json()
        for field_name in _all_fields_dict():
            if field_name in ("summary",):
                continue
            assert (
                field_name in json_str
            ), f"Field '{field_name}' missing from JSON output"


class TestUnifiedModelValidation:
    """Validation rules for UnifiedSessionInfo."""

    def test_invalid_status_rejected(self):
        data = _all_fields_dict()
        data["status"] = "invalid_status"
        with pytest.raises(ValidationError) as exc_info:
            UnifiedSessionInfo(**data)
        errors = exc_info.value.errors()
        status_errors = [e for e in errors if "status" in str(e.get("loc", []))]
        assert len(status_errors) > 0

    def test_invalid_thread_type_rejected(self):
        data = _all_fields_dict()
        data["thread_type"] = "chat"
        with pytest.raises(ValidationError):
            UnifiedSessionInfo(**data)

    def test_missing_required_field_raises(self):
        data = _all_fields_dict()
        del data["session_id"]
        with pytest.raises(ValidationError):
            UnifiedSessionInfo(**data)


class TestUnifiedModelDocstrings:
    """Field-level docstrings documenting which original store each field came from."""

    def test_docstrings_present_for_key_fields(self):
        key_fields = [
            "streaming_token",
            "installation_id",
            "session_proxy_url",
            "issue_number",
            "user",
        ]
        for fname in key_fields:
            field = UnifiedSessionInfo.model_fields.get(fname)
            assert field is not None, f"Field '{fname}' missing"
            assert field.description is not None, f"Field '{fname}' missing docstring"
            assert (
                len(field.description) > 10
            ), f"Field '{fname}' docstring too short: {field.description!r}"


# ============================================================================
# SessionStoreConfig model
# ============================================================================


class TestSessionStoreConfig:
    """SessionStoreConfig extends ConversationConfig with streaming settings."""

    def test_inherits_from_conversation_config(self):
        cfg = SessionStoreConfig()
        assert hasattr(cfg, "persist")
        assert hasattr(cfg, "ttl_hours")
        assert hasattr(cfg, "auto_continue")
        assert hasattr(cfg, "summary_fallback")

    def test_default_values(self):
        cfg = SessionStoreConfig()
        assert cfg.persist is False
        assert cfg.ttl_hours > 0
        assert cfg.auto_continue is False
        assert cfg.summary_fallback is True

    def test_custom_values(self):
        cfg = SessionStoreConfig(
            persist=True,
            ttl_hours=24,
            auto_continue=True,
            summary_fallback=False,
        )
        assert cfg.persist is True
        assert cfg.ttl_hours == 24
        assert cfg.auto_continue is True
        assert cfg.summary_fallback is False


# ============================================================================
# Public Redis key builders
# ============================================================================


class TestPublicKeyBuilders:
    """Verify all key builders are public functions returning correct format strings."""

    def test_session_key(self):
        key = session_key("test/repo", "issue", "42", "my-workflow")
        assert key.startswith("session:map:")
        assert "test--repo" in key
        assert "issue" in key
        assert "42" in key
        assert "my-workflow" in key

    def test_session_pattern(self):
        pattern = session_pattern("test/repo")
        assert pattern.startswith("session:map:")
        assert pattern.endswith(":*")
        assert "test--repo" in pattern

    def test_history_key(self):
        key = history_key("my-token-123")
        assert key == "session:history:my-token-123"

    def test_inbox_key(self):
        key = inbox_key("my-token-456")
        assert key == "session:inbox:my-token-456"

    def test_subscribers_key(self):
        key = subscribers_key("my-token-789")
        assert key == "session:subscribers:my-token-789"

    def test_streaming_lookup_key(self):
        key = streaming_lookup_key(
            "test/repo", "42", "my-workflow", thread_type="issue"
        )
        assert key.startswith("session:stream:lookup:")
        assert "test--repo" in key
        assert "issue" in key
        assert "42" in key
        assert "my-workflow" in key

    def test_session_cleanup_key(self):
        key = session_cleanup_key("test/repo", "issue", "42")
        assert "test--repo" in key
        assert "issue" in key
        assert "42" in key


class TestDeprecatedKeyBuilders:
    """Backward-compatible deprecated wrappers produce DeprecationWarning."""

    def test_deprecated_session_key_emits_warning(self):
        from shared.session_store import _session_key

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            result = _session_key("test/repo", "issue", "42", "wf")
            deprecations = [x for x in w if issubclass(x.category, DeprecationWarning)]
            assert len(deprecations) > 0
            assert result == session_key("test/repo", "issue", "42", "wf")

    def test_deprecated_session_pattern_emits_warning(self):
        from shared.session_store import _session_pattern

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            result = _session_pattern("test/repo")
            deprecations = [x for x in w if issubclass(x.category, DeprecationWarning)]
            assert len(deprecations) > 0
            assert result == session_pattern("test/repo")


# ============================================================================
# SessionStore CRUD tests (RED phase — SessionStore does NOT exist yet)
# ============================================================================

# Import will fail until SessionStore is implemented.
# Use try/except so tests can be collected and fail with clear ImportError.
SessionStore = None
try:
    from shared.session_store import SessionStore  # noqa: F811, F401
except ImportError:
    pass


# ---------------------------------------------------------------------------
# PERSISTENCE: save_session
# ---------------------------------------------------------------------------


@pytest.mark.skipif(SessionStore is None, reason="SessionStore not yet implemented")
class TestSessionStoreSaveSession:
    """save_session with UnifiedSessionInfo fields — atomic HSET with mapping=."""

    @pytest.mark.asyncio
    async def test_new_session_sets_all_fields(self):
        redis = _make_redis_v2()
        store = SessionStore(redis_client=redis)

        await store.save_session(
            repo="owner/repo",
            thread_type="issue",
            thread_id="42",
            workflow="review-pr",
            session_id="sess-123",
            worktree_path="/tmp/wt",
            ref="main",
        )

        assert redis.hset.call_count == 1
        key = redis.hset.call_args[0][0]
        mapping = redis.hset.call_args[1]["mapping"]
        assert key == session_key("owner/repo", "issue", "42", "review-pr")
        assert mapping["status"] in ("active", "running")
        assert mapping["session_id"] == "sess-123"
        # Default fields should be in mapping
        assert "turn_count" in mapping
        assert "run_count" in mapping

    @pytest.mark.asyncio
    async def test_preserves_created_at_via_hsetnx(self):
        redis = _make_redis_v2()
        store = SessionStore(redis_client=redis)

        await store.save_session(
            repo="owner/repo",
            thread_type="issue",
            thread_id="42",
            workflow="review-pr",
            session_id="sess-123",
            worktree_path="/tmp/wt",
            ref="main",
        )

        assert redis.hsetnx.call_count == 1
        _, field, _ = redis.hsetnx.call_args[0]
        assert field == "created_at"

    @pytest.mark.asyncio
    async def test_accumulates_turn_count(self):
        redis = _make_redis_v2()
        store = SessionStore(redis_client=redis)

        await store.save_session(
            repo="owner/repo",
            thread_type="issue",
            thread_id="42",
            workflow="review-pr",
            session_id="sess-123",
            worktree_path="/tmp/wt",
            ref="main",
            turn_count=3,
        )

        assert redis.hincrby.call_count == 1
        _, field, value = redis.hincrby.call_args[0]
        assert field == "turn_count"
        assert value == 3

    @pytest.mark.asyncio
    async def test_only_sets_summary_when_provided(self):
        redis = _make_redis_v2()
        store = SessionStore(redis_client=redis)

        await store.save_session(
            repo="owner/repo",
            thread_type="issue",
            thread_id="42",
            workflow="review-pr",
            session_id="sess-123",
            worktree_path="/tmp/wt",
            ref="main",
        )

        summary_calls = [
            c
            for c in redis.hset.call_args_list
            if len(c[0]) >= 2 and c[0][1] == "summary"
        ]
        assert len(summary_calls) == 0

    @pytest.mark.asyncio
    async def test_sets_summary_when_provided(self):
        redis = _make_redis_v2()
        store = SessionStore(redis_client=redis)

        await store.save_session(
            repo="owner/repo",
            thread_type="issue",
            thread_id="42",
            workflow="review-pr",
            session_id="sess-123",
            worktree_path="/tmp/wt",
            ref="main",
            summary="Test summary",
        )

        summary_calls = [
            c
            for c in redis.hset.call_args_list
            if len(c[0]) >= 2 and c[0][1] == "summary"
        ]
        assert len(summary_calls) == 1

    @pytest.mark.asyncio
    async def test_sets_streaming_token_when_provided(self):
        redis = _make_redis_v2()
        store = SessionStore(redis_client=redis)

        await store.save_session(
            repo="owner/repo",
            thread_type="issue",
            thread_id="42",
            workflow="review-pr",
            session_id="sess-123",
            worktree_path="/tmp/wt",
            ref="main",
            streaming_token="tok-456",
        )

        token_calls = [
            c
            for c in redis.hset.call_args_list
            if len(c[0]) >= 2 and c[0][1] == "streaming_token"
        ]
        assert len(token_calls) == 1

    @pytest.mark.asyncio
    async def test_sets_all_streaming_fields_when_provided(self):
        """When streaming fields are provided, they must be written to the hash."""
        redis = _make_redis_v2()
        store = SessionStore(redis_client=redis)

        await store.save_session(
            repo="owner/repo",
            thread_type="issue",
            thread_id="42",
            workflow="review-pr",
            session_id="sess-123",
            worktree_path="/tmp/wt",
            ref="main",
            installation_id="inst-999",
            initial_query="Fix bug",
            conversation_config='{"persist":true}',
            transcript_path="/tmp/transcript.jsonl",
            run_count=2,
            session_proxy_url="https://proxy.example.com",
            issue_number="42",
            user="testuser",
        )

        mapping = redis.hset.call_args[1]["mapping"]
        assert mapping.get("installation_id") == "inst-999"
        assert mapping.get("initial_query") == "Fix bug"
        assert mapping.get("conversation_config") == '{"persist":true}'
        assert mapping.get("transcript_path") == "/tmp/transcript.jsonl"
        assert mapping.get("run_count") == "2"  # stored as str in Redis
        assert mapping.get("session_proxy_url") == "https://proxy.example.com"
        assert mapping.get("issue_number") == "42"
        assert mapping.get("user") == "testuser"

    @pytest.mark.asyncio
    async def test_ttl_calculation(self):
        redis = _make_redis_v2()
        store = SessionStore(redis_client=redis)

        await store.save_session(
            repo="owner/repo",
            thread_type="issue",
            thread_id="42",
            workflow="review-pr",
            session_id="sess-123",
            worktree_path="/tmp/wt",
            ref="main",
            ttl_hours=24,
        )

        assert redis.expire.call_count == 1
        _, ttl = redis.expire.call_args[0]
        assert ttl == 24 * 3600


# ---------------------------------------------------------------------------
# PERSISTENCE: get_session
# ---------------------------------------------------------------------------


@pytest.mark.skipif(SessionStore is None, reason="SessionStore not yet implemented")
class TestSessionStoreGetSession:
    """get_session by repo/thread_type/thread_id/workflow — returns UnifiedSessionInfo."""

    @pytest.mark.asyncio
    async def test_returns_unified_session_info(self):
        redis = _make_redis_v2()
        redis.hgetall = AsyncMock(
            return_value={
                b"session_id": b"sess-123",
                b"repo": b"owner/repo",
                b"thread_type": b"issue",
                b"thread_id": b"42",
                b"workflow_name": b"review-pr",
                b"ref": b"main",
                b"worktree_path": b"/tmp/wt",
                b"created_at": b"2025-01-01T00:00:00Z",
                b"last_run": b"2025-01-01T00:00:00Z",
                b"turn_count": b"0",
                b"run_count": b"1",
                b"installation_id": b"123",
                b"initial_query": b"hello",
                b"conversation_config": b"{}",
                b"session_proxy_url": b"http://x",
                b"issue_number": b"1",
                b"user": b"test",
            }
        )
        store = SessionStore(redis_client=redis)

        result = await store.get_session("owner/repo", "issue", "42", "review-pr")
        assert result is not None
        assert isinstance(result, UnifiedSessionInfo)
        assert result.session_id == "sess-123"
        assert result.repo == "owner/repo"
        assert result.turn_count == 0

    @pytest.mark.asyncio
    async def test_returns_none_when_not_found(self):
        redis = _make_redis_v2()
        store = SessionStore(redis_client=redis)

        result = await store.get_session("owner/repo", "issue", "42", "review-pr")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_for_corrupt_data(self):
        redis = _make_redis_v2()
        redis.hgetall = AsyncMock(return_value={b"bad": b"data"})
        store = SessionStore(redis_client=redis)

        result = await store.get_session("owner/repo", "issue", "42", "review-pr")
        assert result is None


# ---------------------------------------------------------------------------
# PERSISTENCE: close_session
# ---------------------------------------------------------------------------


@pytest.mark.skipif(SessionStore is None, reason="SessionStore not yet implemented")
class TestSessionStoreCloseSession:
    """close_session — deletes session key + cleans up streaming sub-keys."""

    @pytest.mark.asyncio
    async def test_deletes_session_key(self):
        redis = _make_redis_v2()
        store = SessionStore(redis_client=redis)

        await store.close_session("owner/repo", "issue", "42", "review-pr")
        assert redis.delete.call_count == 1

    @pytest.mark.asyncio
    async def test_cleans_up_streaming_keys_with_token(self):
        redis = _make_redis_v2()
        redis.hgetall = AsyncMock(
            return_value={
                b"session_id": b"sess-123",
                b"repo": b"owner/repo",
                b"thread_type": b"issue",
                b"thread_id": b"42",
                b"workflow_name": b"review-pr",
                b"ref": b"main",
                b"worktree_path": b"/tmp/wt",
                b"created_at": b"2025-01-01T00:00:00Z",
                b"last_run": b"2025-01-01T00:00:00Z",
                b"turn_count": b"0",
                b"streaming_token": b"stream-123",
            }
        )
        store = SessionStore(redis_client=redis)

        await store.close_session("owner/repo", "issue", "42", "review-pr")
        # Should delete: session map key + streaming session + lookup + inbox + subscribers + history
        assert redis.delete.call_count >= 4

    @pytest.mark.asyncio
    async def test_no_streaming_cleanup_without_token(self):
        redis = _make_redis_v2()
        redis.hgetall = AsyncMock(
            return_value={
                b"session_id": b"sess-123",
                b"repo": b"owner/repo",
                b"thread_type": b"issue",
                b"thread_id": b"42",
                b"workflow_name": b"review-pr",
                b"ref": b"main",
                b"worktree_path": b"/tmp/wt",
                b"created_at": b"2025-01-01T00:00:00Z",
                b"last_run": b"2025-01-01T00:00:00Z",
                b"turn_count": b"0",
            }
        )
        store = SessionStore(redis_client=redis)

        await store.close_session("owner/repo", "issue", "42", "review-pr")
        # Should only delete the session map key (no streaming token to clean up)
        assert redis.delete.call_count == 1

    @pytest.mark.asyncio
    async def test_still_deletes_on_corrupt_data(self):
        redis = _make_redis_v2()
        redis.hgetall = AsyncMock(return_value={b"bad": b"data"})
        store = SessionStore(redis_client=redis)

        await store.close_session("owner/repo", "issue", "42", "review-pr")
        assert redis.delete.call_count == 1


# ---------------------------------------------------------------------------
# PERSISTENCE: expire_session
# ---------------------------------------------------------------------------


@pytest.mark.skipif(SessionStore is None, reason="SessionStore not yet implemented")
class TestSessionStoreExpireSession:
    """expire_session — sets TTL on session + propagates to streaming sub-keys."""

    @pytest.mark.asyncio
    async def test_sets_ttl_when_exists(self):
        redis = _make_redis_v2()
        redis.expire = AsyncMock(return_value=1)
        store = SessionStore(redis_client=redis)

        await store.expire_session("owner/repo", "issue", "42", "review-pr")
        assert redis.expire.call_count == 1
        _, ttl = redis.expire.call_args[0]
        assert ttl == 72 * 3600

    @pytest.mark.asyncio
    async def test_no_expire_when_not_found(self):
        redis = _make_redis_v2()
        redis.expire = AsyncMock(return_value=0)
        store = SessionStore(redis_client=redis)

        await store.expire_session("owner/repo", "issue", "42", "review-pr")
        assert redis.expire.call_count == 1

    @pytest.mark.asyncio
    async def test_propagates_ttl_to_streaming_keys(self):
        redis = _make_redis_v2()
        redis.expire = AsyncMock(return_value=1)
        redis.hgetall = AsyncMock(
            return_value={
                b"session_id": b"sess-123",
                b"repo": b"owner/repo",
                b"thread_type": b"issue",
                b"thread_id": b"42",
                b"workflow_name": b"review-pr",
                b"ref": b"main",
                b"worktree_path": b"/tmp/wt",
                b"created_at": b"2025-01-01T00:00:00Z",
                b"last_run": b"2025-01-01T00:00:00Z",
                b"turn_count": b"0",
                b"streaming_token": b"stream-123",
            }
        )
        store = SessionStore(redis_client=redis)

        await store.expire_session("owner/repo", "issue", "42", "review-pr")
        # expire called on: session map + streaming session + inbox + subscribers + lookup
        assert redis.expire.call_count >= 3

    @pytest.mark.asyncio
    async def test_custom_ttl(self):
        redis = _make_redis_v2()
        redis.expire = AsyncMock(return_value=1)
        store = SessionStore(redis_client=redis)

        await store.expire_session(
            "owner/repo",
            "issue",
            "42",
            "review-pr",
            ttl_hours=24,
        )
        _, ttl = redis.expire.call_args[0]
        assert ttl == 24 * 3600


# ---------------------------------------------------------------------------
# PERSISTENCE: list_sessions
# ---------------------------------------------------------------------------


@pytest.mark.skipif(SessionStore is None, reason="SessionStore not yet implemented")
class TestSessionStoreListSessions:
    """list_sessions — SCAN-based listing with corrupt-entry skipping."""

    @pytest.mark.asyncio
    async def test_scans_and_parses(self):
        redis = _make_redis_v2()
        redis.scan = AsyncMock(
            return_value=(
                0,
                [session_key("owner/repo", "issue", "42", "review-pr")],
            )
        )
        redis.hgetall = AsyncMock(
            return_value={
                b"session_id": b"sess-123",
                b"repo": b"owner/repo",
                b"thread_type": b"issue",
                b"thread_id": b"42",
                b"workflow_name": b"review-pr",
                b"ref": b"main",
                b"worktree_path": b"/tmp/wt",
                b"created_at": b"2025-01-01T00:00:00Z",
                b"last_run": b"2025-01-01T00:00:00Z",
                b"turn_count": b"0",
            }
        )
        store = SessionStore(redis_client=redis)

        result = await store.list_sessions("owner/repo")
        assert len(result) == 1
        assert isinstance(result[0], UnifiedSessionInfo)
        assert result[0].session_id == "sess-123"

    @pytest.mark.asyncio
    async def test_empty_list(self):
        redis = _make_redis_v2()
        store = SessionStore(redis_client=redis)

        result = await store.list_sessions("owner/repo")
        assert result == []

    @pytest.mark.asyncio
    async def test_skips_corrupt(self):
        redis = _make_redis_v2()
        redis.scan = AsyncMock(
            return_value=(
                0,
                [
                    session_key("owner/repo", "issue", "42", "review-pr"),
                    session_key("owner/repo", "issue", "43", "review-pr"),
                ],
            )
        )
        redis.hgetall = AsyncMock(
            side_effect=[
                {b"bad": b"data"},
                {
                    b"session_id": b"sess-456",
                    b"repo": b"owner/repo",
                    b"thread_type": b"issue",
                    b"thread_id": b"43",
                    b"workflow_name": b"review-pr",
                    b"ref": b"main",
                    b"worktree_path": b"/tmp/wt",
                    b"created_at": b"2025-01-01T00:00:00Z",
                    b"last_run": b"2025-01-01T00:00:00Z",
                    b"turn_count": b"0",
                },
            ]
        )
        store = SessionStore(redis_client=redis)

        result = await store.list_sessions("owner/repo")
        assert len(result) == 1
        assert result[0].session_id == "sess-456"


class TestSessionStoreJobId:
    """set_job_id / get_job_id — tracks the JobQueue job servicing a
    streaming session so the recovery sweep can check real job status
    instead of guessing from elapsed time (see streaming_job_key)."""

    @pytest.mark.asyncio
    async def test_set_job_id_writes_with_ttl(self):
        redis = _make_redis_v2()
        redis.set = AsyncMock(return_value=True)
        store = SessionStore(redis_client=redis)

        await store.set_job_id("tok-1", "job-123")

        redis.set.assert_awaited_once()
        args, kwargs = redis.set.call_args
        assert args[0] == streaming_session_key("tok-1").replace(
            "session:stream:", "session:stream:job:"
        )
        assert args[1] == "job-123"
        assert kwargs["ex"] == DEFAULT_SESSION_TTL_SECONDS

    @pytest.mark.asyncio
    async def test_get_job_id_returns_value(self):
        redis = _make_redis_v2()
        redis.get = AsyncMock(return_value="job-123")
        store = SessionStore(redis_client=redis)

        result = await store.get_job_id("tok-1")

        assert result == "job-123"

    @pytest.mark.asyncio
    async def test_get_job_id_returns_none_when_absent(self):
        redis = _make_redis_v2()
        redis.get = AsyncMock(return_value=None)
        store = SessionStore(redis_client=redis)

        result = await store.get_job_id("tok-1")

        assert result is None

    @pytest.mark.asyncio
    async def test_get_job_id_decodes_bytes(self):
        redis = _make_redis_v2()
        redis.get = AsyncMock(return_value=b"job-123")
        store = SessionStore(redis_client=redis)

        result = await store.get_job_id("tok-1")

        assert result == "job-123"


class TestSessionStoreListStaleRunningSessions:
    """list_stale_running_sessions — recovery sweep for orphaned streaming
    sessions whose job died before it ever started (see
    STREAMING_SESSION_STALE_SECONDS in shared/constants.py)."""

    @staticmethod
    def _streaming_hash(
        *,
        status: str = "running",
        worktree_path: str = "",
        session_id: str = "",
        last_run_age_seconds: int = 1000,
    ) -> dict:
        last_run = (
            datetime.now(UTC) - timedelta(seconds=last_run_age_seconds)
        ).isoformat()
        return {
            b"token": b"tok-orphan",
            b"repo": b"owner/repo",
            b"thread_type": b"pr",
            b"thread_id": b"40",
            b"issue_number": b"40",
            b"workflow": b"review-pr",
            b"status": status.encode(),
            b"worktree_path": worktree_path.encode(),
            b"session_id": session_id.encode(),
            b"last_run": last_run.encode(),
            b"created_at": last_run.encode(),
            b"installation_id": b"999",
            b"initial_query": b"",
            b"user": b"someone",
            b"ref": b"refs/pull/40/head",
        }

    @pytest.mark.asyncio
    async def test_finds_orphaned_session(self):
        redis = _make_redis_v2()
        redis.scan = AsyncMock(return_value=(0, [streaming_session_key("tok-orphan")]))
        redis.hgetall = AsyncMock(return_value=self._streaming_hash())
        store = SessionStore(redis_client=redis)

        result = await store.list_stale_running_sessions(stale_after_seconds=300)

        assert len(result) == 1
        assert result[0].streaming_token == "tok-orphan"
        assert result[0].repo == "owner/repo"

    @pytest.mark.asyncio
    async def test_skips_lookup_keys(self):
        redis = _make_redis_v2()
        redis.scan = AsyncMock(
            return_value=(
                0,
                [streaming_lookup_key("owner/repo", "40", "review-pr", "pr")],
            )
        )
        store = SessionStore(redis_client=redis)

        result = await store.list_stale_running_sessions(stale_after_seconds=300)

        assert result == []
        redis.hgetall.assert_not_called()

    @pytest.mark.asyncio
    async def test_excludes_recently_updated_session(self):
        redis = _make_redis_v2()
        redis.scan = AsyncMock(return_value=(0, [streaming_session_key("tok-fresh")]))
        redis.hgetall = AsyncMock(
            return_value=self._streaming_hash(last_run_age_seconds=5)
        )
        store = SessionStore(redis_client=redis)

        result = await store.list_stale_running_sessions(stale_after_seconds=300)

        assert result == []

    @pytest.mark.asyncio
    async def test_excludes_session_that_actually_started(self):
        redis = _make_redis_v2()
        redis.scan = AsyncMock(return_value=(0, [streaming_session_key("tok-started")]))
        redis.hgetall = AsyncMock(
            return_value=self._streaming_hash(worktree_path="/tmp/wt")
        )
        store = SessionStore(redis_client=redis)

        result = await store.list_stale_running_sessions(stale_after_seconds=300)

        assert result == []

    @pytest.mark.asyncio
    async def test_excludes_non_running_status(self):
        redis = _make_redis_v2()
        redis.scan = AsyncMock(return_value=(0, [streaming_session_key("tok-done")]))
        redis.hgetall = AsyncMock(return_value=self._streaming_hash(status="error"))
        store = SessionStore(redis_client=redis)

        result = await store.list_stale_running_sessions(stale_after_seconds=300)

        assert result == []


# ---------------------------------------------------------------------------
# PERSISTENCE: update_summary
# ---------------------------------------------------------------------------


@pytest.mark.skipif(SessionStore is None, reason="SessionStore not yet implemented")
class TestSessionStoreUpdateSummary:
    """update_summary — HSET summary field."""

    @pytest.mark.asyncio
    async def test_calls_hset(self):
        redis = _make_redis_v2()
        store = SessionStore(redis_client=redis)

        await store.update_summary(
            "owner/repo",
            "issue",
            "42",
            "review-pr",
            "New summary",
        )
        assert redis.hset.call_count == 1
        _, field, value = redis.hset.call_args[0]
        assert field == "summary"
        assert value == "New summary"

    @pytest.mark.asyncio
    async def test_handles_hset_error(self):
        redis = _make_redis_v2()
        redis.hset = AsyncMock(side_effect=RuntimeError("redis error"))
        store = SessionStore(redis_client=redis)

        # Should not raise — error is logged but swallowed
        await store.update_summary(
            "owner/repo",
            "issue",
            "42",
            "review-pr",
            "New summary",
        )


# ---------------------------------------------------------------------------
# PERSISTENCE: increment_turn_count
# ---------------------------------------------------------------------------


@pytest.mark.skipif(SessionStore is None, reason="SessionStore not yet implemented")
class TestSessionStoreIncrementTurnCount:
    """increment_turn_count — HINCRBY + last_run update."""

    @pytest.mark.asyncio
    async def test_calls_hincrby_and_updates_last_run(self):
        redis = _make_redis_v2()
        store = SessionStore(redis_client=redis)

        await store.increment_turn_count(
            "owner/repo",
            "issue",
            "42",
            "review-pr",
            additional_turns=3,
        )
        assert redis.hincrby.call_count == 1
        _, field, value = redis.hincrby.call_args[0]
        assert field == "turn_count"
        assert value == 3
        assert redis.hset.call_count == 1
        _, field2, _ = redis.hset.call_args[0]
        assert field2 == "last_run"

    @pytest.mark.asyncio
    async def test_handles_hincrby_error(self):
        redis = _make_redis_v2()
        redis.hincrby = AsyncMock(side_effect=RuntimeError("redis error"))
        store = SessionStore(redis_client=redis)

        await store.increment_turn_count(
            "owner/repo",
            "issue",
            "42",
            "review-pr",
            additional_turns=3,
        )


# ============================================================================
# STREAMING: create_session
# ============================================================================


@pytest.mark.skipif(SessionStore is None, reason="SessionStore not yet implemented")
class TestSessionStoreCreateSession:
    """create_session — creates streaming session hash + lookup via pipeline."""

    @pytest.mark.asyncio
    async def test_hset_expire_and_lookup(self):
        redis = _make_redis_v2()
        store = SessionStore(redis_client=redis)

        await store.create_session(
            token="test-token",
            repo="owner/repo",
            issue_number=42,
            workflow="review-pr",
        )

        pipeline = redis.pipeline.return_value
        assert pipeline.hset.call_count == 1
        args, kwargs = pipeline.hset.call_args
        assert args[0] == "session:stream:test-token"
        mapping = kwargs.get("mapping")
        assert mapping["status"] == "running"
        assert mapping["repo"] == "owner/repo"
        assert mapping["issue_number"] == "42"
        assert mapping["workflow"] == "review-pr"
        assert pipeline.expire.call_count == 1
        assert pipeline.setex.call_count == 1
        pipeline.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_custom_ttl(self):
        redis = _make_redis_v2()
        store = SessionStore(redis_client=redis)

        await store.create_session(
            token="test-token",
            repo="owner/repo",
            issue_number=42,
            workflow="review-pr",
            ttl_seconds=600,
        )

        pipeline = redis.pipeline.return_value
        assert pipeline.expire.call_args[0][1] == 600

    @pytest.mark.asyncio
    async def test_all_optional_fields_written(self):
        redis = _make_redis_v2()
        store = SessionStore(redis_client=redis)

        await store.create_session(
            token="test-token",
            repo="owner/repo",
            issue_number=42,
            workflow="review-pr",
            session_proxy_url="https://proxy",
            installation_id="inst-1",
            initial_query="Fix this",
            thread_type="pr",
            ref="feature/branch",
            user="dev",
            conversation_config='{"persist":true}',
            session_id="sdk-sess-1",
        )

        pipeline = redis.pipeline.return_value
        mapping = pipeline.hset.call_args[1]["mapping"]
        assert mapping["session_proxy_url"] == "https://proxy"
        assert mapping["installation_id"] == "inst-1"
        assert mapping["initial_query"] == "Fix this"
        assert mapping["thread_type"] == "pr"
        assert mapping["ref"] == "feature/branch"
        assert mapping["user"] == "dev"
        assert mapping["conversation_config"] == '{"persist":true}'
        assert mapping["session_id"] == "sdk-sess-1"


# ============================================================================
# STREAMING: get_streaming_session
# ============================================================================


@pytest.mark.skipif(SessionStore is None, reason="SessionStore not yet implemented")
class TestSessionStoreGetStreamingSession:
    """get_streaming_session — returns UnifiedSessionInfo or None by token."""

    @pytest.mark.asyncio
    async def test_returns_unified_session_info(self):
        redis = _make_redis_v2()
        redis.hgetall = AsyncMock(
            return_value={
                b"session_id": b"sess-123",
                b"repo": b"owner/repo",
                b"thread_type": b"issue",
                b"thread_id": b"42",
                b"workflow_name": b"review-pr",
                b"ref": b"main",
                b"worktree_path": b"/tmp/wt",
                b"created_at": b"2025-01-01T00:00:00Z",
                b"last_run": b"2025-01-01T00:00:00Z",
                b"turn_count": b"0",
            }
        )
        store = SessionStore(redis_client=redis)

        result = await store.get_streaming_session("test-token")
        assert result is not None
        assert isinstance(result, UnifiedSessionInfo)

    @pytest.mark.asyncio
    async def test_returns_none_for_empty_hash(self):
        redis = _make_redis_v2()
        store = SessionStore(redis_client=redis)

        result = await store.get_streaming_session("test-token")
        assert result is None

    @pytest.mark.asyncio
    async def test_decodes_mixed_bytes_and_strings(self):
        redis = _make_redis_v2()
        redis.hgetall = AsyncMock(
            return_value={
                "token": "abc",
                b"status": b"running",
                b"session_id": b"sess-123",
                b"repo": b"owner/repo",
                b"thread_type": b"issue",
                b"thread_id": b"42",
                b"workflow_name": b"review-pr",
                b"ref": b"main",
                b"worktree_path": b"/tmp/wt",
                b"created_at": b"2025-01-01T00:00:00Z",
                b"last_run": b"2025-01-01T00:00:00Z",
                b"turn_count": b"0",
            }
        )
        store = SessionStore(redis_client=redis)

        result = await store.get_streaming_session("abc")
        assert result is not None
        assert result.status == SessionStatus.running


# ============================================================================
# STREAMING: find_session / find_active_session
# ============================================================================


@pytest.mark.skipif(SessionStore is None, reason="SessionStore not yet implemented")
class TestSessionStoreFindSession:
    """find_session — token lookup by repo/issue/workflow with legacy fallback."""

    @pytest.mark.asyncio
    async def test_returns_token_when_found(self):
        redis = _make_redis_v2()
        redis.get = AsyncMock(return_value="test-token")
        redis.hgetall = AsyncMock(return_value={"status": "running"})
        store = SessionStore(redis_client=redis)

        result = await store.find_session("owner/repo", 42, "review-pr")
        assert result == "test-token"

    @pytest.mark.asyncio
    async def test_returns_none_when_not_found(self):
        redis = _make_redis_v2()
        store = SessionStore(redis_client=redis)

        result = await store.find_session("owner/repo", 42, "review-pr")
        assert result is None

    @pytest.mark.asyncio
    async def test_stale_lookup_cleaned_up(self):
        redis = _make_redis_v2()
        redis.get = AsyncMock(return_value="stale-token")
        redis.hgetall = AsyncMock(return_value={})
        store = SessionStore(redis_client=redis)

        result = await store.find_session("owner/repo", 42, "review-pr")
        assert result is None
        assert redis.delete.call_count == 1

    @pytest.mark.asyncio
    async def test_legacy_fallback(self):
        redis = _make_redis_v2()

        async def _get(key):
            if ":issue:" in key:
                return None
            return "legacy-token"

        redis.get = AsyncMock(side_effect=_get)
        redis.hgetall = AsyncMock(return_value={"status": "running"})
        store = SessionStore(redis_client=redis)

        result = await store.find_session(
            "owner/repo",
            42,
            "review-pr",
            thread_type="issue",
        )
        assert result == "legacy-token"

    @pytest.mark.asyncio
    async def test_decodes_bytes_token(self):
        redis = _make_redis_v2()
        redis.get = AsyncMock(return_value=b"byte-token")
        redis.hgetall = AsyncMock(return_value={"status": "running"})
        store = SessionStore(redis_client=redis)

        result = await store.find_session("owner/repo", 42, "review-pr")
        assert result == "byte-token"


@pytest.mark.skipif(SessionStore is None, reason="SessionStore not yet implemented")
class TestSessionStoreFindActiveSession:
    """find_active_session — only returns token for running sessions."""

    @pytest.mark.asyncio
    async def test_returns_token_for_running(self):
        redis = _make_redis_v2()
        redis.get = AsyncMock(return_value="test-token")
        redis.hgetall = AsyncMock(return_value={"status": "running"})
        store = SessionStore(redis_client=redis)

        result = await store.find_active_session("owner/repo", 42, "review-pr")
        assert result == "test-token"

    @pytest.mark.asyncio
    async def test_returns_none_for_completed(self):
        redis = _make_redis_v2()
        redis.get = AsyncMock(return_value="test-token")
        redis.hgetall = AsyncMock(return_value={"status": "completed"})
        store = SessionStore(redis_client=redis)

        result = await store.find_active_session("owner/repo", 42, "review-pr")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_no_session(self):
        redis = _make_redis_v2()
        store = SessionStore(redis_client=redis)

        result = await store.find_active_session("owner/repo", 42, "review-pr")
        assert result is None


# ============================================================================
# STREAMING: set_completed / set_running
# ============================================================================


@pytest.mark.skipif(SessionStore is None, reason="SessionStore not yet implemented")
class TestSessionStoreSetCompleted:
    """set_completed — marks session as completed or error."""

    @pytest.mark.asyncio
    async def test_status_completed(self):
        redis = _make_redis_v2()
        store = SessionStore(redis_client=redis)

        await store.set_completed("test-token")
        assert redis.hset.call_count == 1
        key, field, value = redis.hset.call_args[0]
        assert key == "session:stream:test-token"
        assert field == "status"
        assert value == "completed"

    @pytest.mark.asyncio
    async def test_status_error(self):
        redis = _make_redis_v2()
        store = SessionStore(redis_client=redis)

        await store.set_completed("test-token", is_error=True)
        key, field, value = redis.hset.call_args[0]
        assert field == "status"
        assert value == "error"

    @pytest.mark.asyncio
    async def test_with_session_id_uses_mapping(self):
        redis = _make_redis_v2()
        store = SessionStore(redis_client=redis)

        await store.set_completed("test-token", session_id="sess-123")
        mapping = redis.hset.call_args[1]["mapping"]
        assert mapping["status"] == "completed"
        assert mapping["session_id"] == "sess-123"


@pytest.mark.skipif(SessionStore is None, reason="SessionStore not yet implemented")
class TestSessionStoreSetRunning:
    """set_running — resets status to running, clears stale fields, applies TTL."""

    @pytest.mark.asyncio
    async def test_updates_status_and_ttl(self):
        redis = _make_redis_v2()
        store = SessionStore(redis_client=redis)

        await store.set_running("test-token", ttl_seconds=3600)
        assert redis.hset.call_count == 1
        key = redis.hset.call_args[0][0]
        mapping = redis.hset.call_args[1]["mapping"]
        assert key == "session:stream:test-token"
        assert mapping["status"] == "running"
        assert mapping["session_id"] == ""
        assert redis.expire.call_count == 1


# ============================================================================
# STREAMING: delete / set_ttl
# ============================================================================


@pytest.mark.skipif(SessionStore is None, reason="SessionStore not yet implemented")
class TestSessionStoreDeleteSession:
    """delete_session — removes streaming session + sub-keys."""

    @pytest.mark.asyncio
    async def test_removes_all_keys(self):
        redis = _make_redis_v2()
        store = SessionStore(redis_client=redis)

        await store.delete_session("test-token")
        # session:stream:{token} + inbox + subscribers
        assert redis.delete.call_count == 3


@pytest.mark.skipif(SessionStore is None, reason="SessionStore not yet implemented")
class TestSessionStoreSetTtl:
    """set_ttl — sets TTL on streaming session + sub-keys."""

    @pytest.mark.asyncio
    async def test_applies_to_all_keys(self):
        redis = _make_redis_v2()
        store = SessionStore(redis_client=redis)

        await store.set_ttl("test-token", ttl_seconds=7200)
        # session:stream + inbox + subscribers
        assert redis.expire.call_count == 3


# ============================================================================
# SUBSCRIBERS
# ============================================================================


@pytest.mark.skipif(SessionStore is None, reason="SessionStore not yet implemented")
class TestSessionStoreSubscribers:
    """Subscriber count operations — atomic Lua scripts."""

    @pytest.mark.asyncio
    async def test_increment_subscribers(self):
        redis = _make_redis_v2()
        redis.eval = AsyncMock(return_value=3)
        store = SessionStore(redis_client=redis)

        result = await store.increment_subscribers("test-token")
        assert result == 3
        assert redis.eval.call_count == 1

    @pytest.mark.asyncio
    async def test_decrement_subscribers(self):
        redis = _make_redis_v2()
        redis.eval = AsyncMock(return_value=1)
        store = SessionStore(redis_client=redis)

        result = await store.decrement_subscribers("test-token")
        assert result == 1
        assert redis.eval.call_count == 1

    @pytest.mark.asyncio
    async def test_has_subscribers_true(self):
        redis = _make_redis_v2()
        redis.get = AsyncMock(return_value="2")
        store = SessionStore(redis_client=redis)

        result = await store.has_subscribers("test-token")
        assert result is True

    @pytest.mark.asyncio
    async def test_has_subscribers_false_none(self):
        redis = _make_redis_v2()
        redis.get = AsyncMock(return_value=None)
        store = SessionStore(redis_client=redis)

        result = await store.has_subscribers("test-token")
        assert result is False

    @pytest.mark.asyncio
    async def test_has_subscribers_false_zero(self):
        redis = _make_redis_v2()
        redis.get = AsyncMock(return_value="0")
        store = SessionStore(redis_client=redis)

        result = await store.has_subscribers("test-token")
        assert result is False

    @pytest.mark.asyncio
    async def test_has_subscribers_decodes_bytes(self):
        redis = _make_redis_v2()
        redis.get = AsyncMock(return_value=b"1")
        store = SessionStore(redis_client=redis)

        result = await store.has_subscribers("test-token")
        assert result is True


# ============================================================================
# HISTORY
# ============================================================================


@pytest.mark.skipif(SessionStore is None, reason="SessionStore not yet implemented")
class TestSessionStoreHistory:
    """get_history — LRANGE with JSON parsing."""

    @pytest.mark.asyncio
    async def test_parses_json(self):
        redis = _make_redis_v2()
        redis.lrange = AsyncMock(
            return_value=[b'{"type": "stream_event", "data": {}}'],
        )
        store = SessionStore(redis_client=redis)

        result = await store.get_history("test-token")
        assert len(result) == 1
        assert result[0]["type"] == "stream_event"

    @pytest.mark.asyncio
    async def test_empty_returns_empty_list(self):
        redis = _make_redis_v2()
        store = SessionStore(redis_client=redis)

        result = await store.get_history("test-token")
        assert result == []

    @pytest.mark.asyncio
    async def test_skips_invalid_json(self):
        redis = _make_redis_v2()
        redis.lrange = AsyncMock(
            return_value=[b"invalid json", b'{"type": "ok"}'],
        )
        store = SessionStore(redis_client=redis)

        result = await store.get_history("test-token")
        assert len(result) == 1
        assert result[0]["type"] == "ok"


# ============================================================================
# INBOX
# ============================================================================


@pytest.mark.skipif(SessionStore is None, reason="SessionStore not yet implemented")
class TestSessionStoreInbox:
    """push_inbox_message / pop_inbox_messages — Lua drain script."""

    @pytest.mark.asyncio
    async def test_push_inbox_message(self):
        redis = _make_redis_v2()
        store = SessionStore(redis_client=redis)

        await store.push_inbox_message("test-token", "Hello")
        assert redis.rpush.call_count == 1
        assert redis.expire.call_count == 1
        payload = json.loads(redis.rpush.call_args[0][1])
        assert payload["type"] == "user_message"
        assert payload["content"] == "Hello"

    @pytest.mark.asyncio
    async def test_pop_inbox_messages(self):
        redis = _make_redis_v2()
        redis.eval = AsyncMock(
            return_value=[b'{"type": "user_message", "content": "Hello"}'],
        )
        store = SessionStore(redis_client=redis)

        result = await store.pop_inbox_messages("test-token")
        assert result == ["Hello"]

    @pytest.mark.asyncio
    async def test_pop_inbox_messages_empty(self):
        redis = _make_redis_v2()
        redis.eval = AsyncMock(return_value=[])
        store = SessionStore(redis_client=redis)

        result = await store.pop_inbox_messages("test-token")
        assert result == []

    @pytest.mark.asyncio
    async def test_pop_inbox_messages_eval_error(self):
        redis = _make_redis_v2()
        redis.eval = AsyncMock(side_effect=RuntimeError("redis error"))
        store = SessionStore(redis_client=redis)

        with pytest.raises(RuntimeError, match="redis error"):
            await store.pop_inbox_messages("test-token")

    @pytest.mark.asyncio
    async def test_pop_filters_non_user_messages(self):
        redis = _make_redis_v2()
        redis.eval = AsyncMock(
            return_value=[
                b'{"type": "system_message", "content": "ignore"}',
                b'{"type": "user_message", "content": "keep"}',
            ]
        )
        store = SessionStore(redis_client=redis)

        result = await store.pop_inbox_messages("test-token")
        assert result == ["keep"]


# ============================================================================
# FIELD UPDATES (session_id, transcript_path, run_count)
# ============================================================================


@pytest.mark.skipif(SessionStore is None, reason="SessionStore not yet implemented")
class TestSessionStoreUpdateFields:
    """update_session_id, update_transcript_path, increment_run_count."""

    @pytest.mark.asyncio
    async def test_update_session_id(self):
        redis = _make_redis_v2()
        store = SessionStore(redis_client=redis)

        await store.update_session_id("test-token", "sess-123")
        assert redis.hset.call_count == 1
        key, field, value = redis.hset.call_args[0]
        assert key == "session:stream:test-token"
        assert field == "session_id"
        assert value == "sess-123"

    @pytest.mark.asyncio
    async def test_update_transcript_path(self):
        redis = _make_redis_v2()
        store = SessionStore(redis_client=redis)

        await store.update_transcript_path("test-token", "/path/to/transcript")
        key, field, value = redis.hset.call_args[0]
        assert field == "transcript_path"
        assert value == "/path/to/transcript"

    @pytest.mark.asyncio
    async def test_increment_run_count(self):
        redis = _make_redis_v2()
        redis.hincrby = AsyncMock(return_value=5)
        store = SessionStore(redis_client=redis)

        result = await store.increment_run_count("test-token")
        assert result == 5
        key, field, amount = redis.hincrby.call_args[0]
        assert field == "run_count"
        assert amount == 1
