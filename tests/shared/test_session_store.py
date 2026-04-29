"""Tests for shared/session_store.py — SessionStore and resolve_thread_type."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.constants import DEFAULT_SESSION_TTL_HOURS
from shared.session_store import SessionStore, _session_key, resolve_thread_type


def _make_redis():
    """Create a mock Redis client with explicit async methods."""
    redis = MagicMock()
    redis.get = AsyncMock(return_value=None)
    redis.setex = AsyncMock(return_value=True)
    redis.delete = AsyncMock(return_value=1)
    redis.expire = AsyncMock(return_value=True)
    redis.exists = AsyncMock(return_value=1)
    redis.eval = AsyncMock(return_value=1)
    redis.scan = AsyncMock(return_value=(0, []))
    redis.hset = AsyncMock(return_value=1)
    redis.hsetnx = AsyncMock(return_value=1)
    redis.hincrby = AsyncMock(return_value=1)
    redis.hgetall = AsyncMock(return_value={})
    redis.hget = AsyncMock(return_value=None)
    return redis


class TestResolveThreadType:
    def test_pr_from_event_type(self):
        assert resolve_thread_type({"event_type": "pull_request"}) == "pr"

    def test_pr_from_pull_request_opened(self):
        assert resolve_thread_type({"event_type": "pull_request.opened"}) == "pr"

    def test_pr_from_issue_comment_on_pr(self):
        data = {
            "event_type": "issue_comment",
            "payload": {"issue": {"pull_request": {"url": "https://..."}}},
        }
        assert resolve_thread_type(data) == "pr"

    def test_pr_from_is_pr_flag(self):
        assert (
            resolve_thread_type({"event_type": "issue_comment", "is_pr": True}) == "pr"
        )

    def test_discussion_from_event_type(self):
        assert resolve_thread_type({"event_type": "discussion.created"}) == "discussion"

    def test_issue_default(self):
        assert resolve_thread_type({"event_type": "issue_comment"}) == "issue"

    def test_empty_dict_returns_issue(self):
        assert resolve_thread_type({}) == "issue"


class TestSessionStoreSaveSession:
    @pytest.mark.asyncio
    async def test_new_session_sets_fields(self):
        redis = _make_redis()
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

        assert redis.hset.call_count == 9
        assert redis.hsetnx.call_count == 1
        _, field, _ = redis.hsetnx.call_args[0]
        assert field == "created_at"
        assert redis.hincrby.call_count == 0
        assert redis.expire.call_count == 1
        _, ttl = redis.expire.call_args[0]
        assert ttl == DEFAULT_SESSION_TTL_HOURS * 3600

    @pytest.mark.asyncio
    async def test_preserves_created_at(self):
        redis = _make_redis()
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
        redis = _make_redis()
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
    async def test_preserves_summary(self):
        redis = _make_redis()
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
            call
            for call in redis.hset.call_args_list
            if len(call[0]) >= 2 and call[0][1] == "summary"
        ]
        assert len(summary_calls) == 0

    @pytest.mark.asyncio
    async def test_preserves_streaming_token(self):
        redis = _make_redis()
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

        token_calls = [
            call
            for call in redis.hset.call_args_list
            if len(call[0]) >= 2 and call[0][1] == "streaming_token"
        ]
        assert len(token_calls) == 0

    @pytest.mark.asyncio
    async def test_ttl_calculation(self):
        redis = _make_redis()
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


class TestSessionStoreGetSession:
    @pytest.mark.asyncio
    async def test_returns_session_info(self):
        redis = _make_redis()
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

        result = await store.get_session("owner/repo", "issue", "42", "review-pr")
        assert result is not None
        assert result.session_id == "sess-123"
        assert result.repo == "owner/repo"

    @pytest.mark.asyncio
    async def test_returns_none_when_not_found(self):
        redis = _make_redis()
        store = SessionStore(redis_client=redis)

        result = await store.get_session("owner/repo", "issue", "42", "review-pr")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_for_corrupt_data(self):
        redis = _make_redis()
        redis.hgetall = AsyncMock(return_value={b"bad": b"data"})
        store = SessionStore(redis_client=redis)

        result = await store.get_session("owner/repo", "issue", "42", "review-pr")
        assert result is None


class TestSessionStoreCloseSession:
    @pytest.mark.asyncio
    async def test_deletes_session_key(self):
        redis = _make_redis()
        store = SessionStore(redis_client=redis)

        await store.close_session("owner/repo", "issue", "42", "review-pr")
        assert redis.delete.call_count == 1

    @pytest.mark.asyncio
    async def test_cleans_up_streaming(self):
        redis = _make_redis()
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
        assert redis.delete.call_count > 1

    @pytest.mark.asyncio
    async def test_no_streaming_cleanup_without_token(self):
        redis = _make_redis()
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
        assert redis.delete.call_count == 1

    @pytest.mark.asyncio
    async def test_still_deletes_on_corrupt_data(self):
        redis = _make_redis()
        redis.hgetall = AsyncMock(return_value={b"bad": b"data"})
        store = SessionStore(redis_client=redis)

        await store.close_session("owner/repo", "issue", "42", "review-pr")
        assert redis.delete.call_count == 1


class TestSessionStoreExpireSession:
    @pytest.mark.asyncio
    async def test_sets_ttl_when_exists(self):
        redis = _make_redis()
        redis.expire = AsyncMock(return_value=1)
        store = SessionStore(redis_client=redis)

        await store.expire_session("owner/repo", "issue", "42", "review-pr")
        assert redis.expire.call_count == 1
        _, ttl = redis.expire.call_args[0]
        assert ttl == 72 * 3600

    @pytest.mark.asyncio
    async def test_no_expire_when_not_found(self):
        redis = _make_redis()
        redis.expire = AsyncMock(return_value=0)
        store = SessionStore(redis_client=redis)

        await store.expire_session("owner/repo", "issue", "42", "review-pr")
        assert redis.expire.call_count == 1

    @pytest.mark.asyncio
    async def test_propagates_to_streaming(self):
        redis = _make_redis()
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
        assert redis.expire.call_count >= 1


class TestSessionStoreUpdateSummary:
    @pytest.mark.asyncio
    async def test_calls_hset(self):
        redis = _make_redis()
        store = SessionStore(redis_client=redis)

        await store.update_summary(
            "owner/repo", "issue", "42", "review-pr", "New summary"
        )

        assert redis.hset.call_count == 1
        _, field, value = redis.hset.call_args[0]
        assert field == "summary"
        assert value == "New summary"

    @pytest.mark.asyncio
    async def test_handles_hset_error(self):
        redis = _make_redis()
        redis.hset = AsyncMock(side_effect=RuntimeError("redis error"))
        store = SessionStore(redis_client=redis)

        await store.update_summary(
            "owner/repo", "issue", "42", "review-pr", "New summary"
        )


class TestSessionStoreIncrementTurnCount:
    @pytest.mark.asyncio
    async def test_calls_hincrby(self):
        redis = _make_redis()
        store = SessionStore(redis_client=redis)

        await store.increment_turn_count(
            "owner/repo", "issue", "42", "review-pr", additional_turns=3
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
        redis = _make_redis()
        redis.hincrby = AsyncMock(side_effect=RuntimeError("redis error"))
        store = SessionStore(redis_client=redis)

        await store.increment_turn_count(
            "owner/repo", "issue", "42", "review-pr", additional_turns=3
        )


class TestSessionStoreListSessions:
    @pytest.mark.asyncio
    async def test_scans_and_parses(self):
        redis = _make_redis()
        redis.scan = AsyncMock(
            return_value=(
                0,
                [
                    _session_key("owner/repo", "issue", "42", "review-pr"),
                ],
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
        assert result[0].session_id == "sess-123"

    @pytest.mark.asyncio
    async def test_empty_list(self):
        redis = _make_redis()
        store = SessionStore(redis_client=redis)

        result = await store.list_sessions("owner/repo")
        assert result == []

    @pytest.mark.asyncio
    async def test_skips_corrupt(self):
        redis = _make_redis()
        redis.scan = AsyncMock(
            return_value=(
                0,
                [
                    _session_key("owner/repo", "issue", "42", "review-pr"),
                    _session_key("owner/repo", "issue", "43", "review-pr"),
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
