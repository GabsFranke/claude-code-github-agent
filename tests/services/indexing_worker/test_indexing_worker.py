"""Tests for the indexing worker module.

Tests cover: content hashing, embedding cache, batch_embed validation,
commit hash handling, metadata migration, dead-letter queue behavior,
and collection name generation.
"""

import hashlib
import json
from unittest.mock import AsyncMock, patch

import pytest

from services.indexing_worker.indexing_worker import (
    _DLQ_KEY,
    _QUEUE_KEY,
    MAX_JOB_RETRIES,
    _collection_name,
    _content_hash,
    _enqueue_for_retry,
    _is_transient_error,
    _migrate_meta_key,
    batch_embed,
    get_dlq_count,
    inspect_dlq,
)

# ---------------------------------------------------------------------------
# _collection_name
# ---------------------------------------------------------------------------


class TestCollectionName:
    def test_converts_slash_to_double_underscore(self):
        assert _collection_name("owner/repo") == "owner__repo"

    def test_no_slash(self):
        assert _collection_name("repo") == "repo"

    def test_multiple_slashes(self):
        assert _collection_name("org/team/repo") == "org__team__repo"


# ---------------------------------------------------------------------------
# _content_hash
# ---------------------------------------------------------------------------


class TestContentHash:
    def test_produces_deterministic_hash(self):
        content = "def hello(): pass"
        h1 = _content_hash(content)
        h2 = _content_hash(content)
        assert h1 == h2
        assert len(h1) == 64  # SHA-256 hex digest

    def test_different_content_different_hash(self):
        h1 = _content_hash("content A")
        h2 = _content_hash("content B")
        assert h1 != h2

    def test_matches_sha256(self):
        content = "test content"
        expected = hashlib.sha256(content.encode()).hexdigest()
        assert _content_hash(content) == expected


# ---------------------------------------------------------------------------
# _is_transient_error
# ---------------------------------------------------------------------------


class TestIsTransientError:
    def test_timeout_is_transient(self):
        assert _is_transient_error(Exception("connection timeout"))

    def test_429_is_transient(self):
        assert _is_transient_error(Exception("429 Rate Limited"))

    def test_resource_exhausted_is_transient(self):
        assert _is_transient_error(Exception("RESOURCE_EXHAUSTED"))

    def test_connection_reset_is_transient(self):
        assert _is_transient_error(Exception("ECONNRESET"))

    def test_config_error_is_not_transient(self):
        assert not _is_transient_error(RuntimeError("git rev-parse HEAD failed"))

    def test_validation_error_is_not_transient(self):
        assert not _is_transient_error(ValueError("missing repo"))

    def test_generic_error_is_not_transient(self):
        assert not _is_transient_error(Exception("something went wrong"))


# ---------------------------------------------------------------------------
# _migrate_meta_key
# ---------------------------------------------------------------------------


class TestMigrateMetaKey:
    @pytest.mark.asyncio
    async def test_logs_on_failure(self):
        """Migration failure should be logged at DEBUG, not silently passed."""
        redis_client = AsyncMock()
        redis_client.delete.side_effect = Exception("Redis error")

        with patch("services.indexing_worker.indexing_worker.logger") as mock_logger:
            await _migrate_meta_key(redis_client, "some_key")
            mock_logger.debug.assert_called_once()
            assert "some_key" in mock_logger.debug.call_args[0][0]

    @pytest.mark.asyncio
    async def test_succeeds(self):
        redis_client = AsyncMock()
        await _migrate_meta_key(redis_client, "some_key")
        redis_client.delete.assert_called_once_with("some_key")


# ---------------------------------------------------------------------------
# batch_embed
# ---------------------------------------------------------------------------


class TestBatchEmbed:
    @pytest.mark.asyncio
    async def test_empty_input(self):
        result = await batch_embed([])
        assert result == []

    @pytest.mark.asyncio
    async def test_all_cached(self):
        """When all texts are cached, should return cached embeddings."""
        cached_embedding = [0.1, 0.2, 0.3]

        with patch(
            "services.indexing_worker.indexing_worker._get_cached_embeddings",
            new_callable=AsyncMock,
            return_value=([cached_embedding], []),  # 1 result, 0 misses
        ) as mock_cache:
            result = await batch_embed(["text1"], redis_client=AsyncMock())
            assert len(result) == 1
            assert result[0] == cached_embedding
            mock_cache.assert_called_once()

    @pytest.mark.asyncio
    async def test_count_mismatch_raises(self):
        """If _embed_texts returns wrong count, batch_embed should raise."""
        with patch(
            "services.indexing_worker.indexing_worker._embed_texts",
            new_callable=AsyncMock,
            return_value=[[0.1, 0.2]],  # Only 1 embedding for 2 texts
        ):
            with pytest.raises(ValueError, match="Embedding count mismatch"):
                await batch_embed(["text1", "text2"])


# ---------------------------------------------------------------------------
# Dead-letter queue
# ---------------------------------------------------------------------------


class TestDeadLetterQueue:
    @pytest.mark.asyncio
    async def test_first_retry_reenqueues(self):
        """First failure should re-enqueue the job, not send to DLQ."""
        redis_client = AsyncMock()
        message = {"repo": "owner/repo", "ref": "main"}

        await _enqueue_for_retry(redis_client, message, Exception("connection timeout"))

        # Should have re-enqueued (RPUSH to queue, not DLQ)
        redis_client.rpush.assert_called_once()
        call_args = redis_client.rpush.call_args
        assert call_args[0][0] == _QUEUE_KEY
        requeued = json.loads(call_args[0][1])
        assert requeued["attempts"] == 1

    @pytest.mark.asyncio
    async def test_max_retries_goes_to_dlq(self):
        """After MAX_JOB_RETRIES, the job should go to DLQ."""
        redis_client = AsyncMock()
        message = {"repo": "owner/repo", "ref": "main", "attempts": MAX_JOB_RETRIES - 1}

        await _enqueue_for_retry(redis_client, message, Exception("connection timeout"))

        # Should have pushed to DLQ
        redis_client.rpush.assert_called_once()
        call_args = redis_client.rpush.call_args
        assert call_args[0][0] == _DLQ_KEY
        dlq_entry = json.loads(call_args[0][1])
        assert dlq_entry["reason"] == "max_retries_exceeded"
        assert dlq_entry["repo"] == "owner/repo"
        assert dlq_entry["attempts"] == MAX_JOB_RETRIES

    @pytest.mark.asyncio
    async def test_attempt_counter_increments(self):
        """Each retry should increment the attempts counter."""
        redis_client = AsyncMock()
        message = {"repo": "owner/repo", "attempts": 1}

        await _enqueue_for_retry(redis_client, message, Exception("timeout"))

        requeued = json.loads(redis_client.rpush.call_args[0][1])
        assert requeued["attempts"] == 2
        assert "last_error" in requeued

    @pytest.mark.asyncio
    async def test_get_dlq_count(self):
        redis_client = AsyncMock()
        redis_client.llen.return_value = 5

        count = await get_dlq_count(redis_client)
        assert count == 5

    @pytest.mark.asyncio
    async def test_get_dlq_count_on_error(self):
        redis_client = AsyncMock()
        redis_client.llen.side_effect = Exception("Redis down")

        count = await get_dlq_count(redis_client)
        assert count == 0

    @pytest.mark.asyncio
    async def test_inspect_dlq(self):
        redis_client = AsyncMock()
        entries = [
            json.dumps({"repo": "a/b", "reason": "max_retries_exceeded"}),
            json.dumps({"repo": "c/d", "reason": "non_transient_error"}),
        ]
        redis_client.lrange.return_value = entries

        result = await inspect_dlq(redis_client, limit=10)
        assert len(result) == 2
        assert result[0]["repo"] == "a/b"
        assert result[1]["repo"] == "c/d"

    @pytest.mark.asyncio
    async def test_inspect_dlq_on_error(self):
        redis_client = AsyncMock()
        redis_client.lrange.side_effect = Exception("Redis down")

        result = await inspect_dlq(redis_client)
        assert result == []


# ---------------------------------------------------------------------------
# _get_commit_hash
# ---------------------------------------------------------------------------


class TestGetCommitHash:
    @pytest.mark.asyncio
    async def test_raises_on_git_failure(self):
        """Should raise RuntimeError, not return 'unknown'."""
        from services.indexing_worker.indexing_worker import _get_commit_hash

        with patch(
            "services.indexing_worker.indexing_worker.asyncio.create_subprocess_exec",
            side_effect=OSError("git not found"),
        ):
            with pytest.raises(RuntimeError, match="Failed to get commit hash"):
                await _get_commit_hash("/nonexistent/worktree")

    @pytest.mark.asyncio
    async def test_raises_on_nonzero_exit(self):
        """Should raise RuntimeError when git returns non-zero exit code."""
        from services.indexing_worker.indexing_worker import _get_commit_hash

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(
            return_value=(b"", b"fatal: not a git repository")
        )
        mock_proc.returncode = 128

        with patch(
            "services.indexing_worker.indexing_worker.asyncio.create_subprocess_exec",
            return_value=mock_proc,
        ):
            with pytest.raises(RuntimeError, match="git rev-parse HEAD failed"):
                await _get_commit_hash("/tmp/bad_worktree")

    @pytest.mark.asyncio
    async def test_returns_hash_on_success(self):
        """Should return the commit hash on success."""
        from services.indexing_worker.indexing_worker import _get_commit_hash

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"abc123def456\n", b""))
        mock_proc.returncode = 0

        with patch(
            "services.indexing_worker.indexing_worker.asyncio.create_subprocess_exec",
            return_value=mock_proc,
        ):
            result = await _get_commit_hash("/tmp/worktree")
            assert result == "abc123def456"
