"""Tests for the indexing worker module.

Tests cover: content hashing, embedding cache, batch_embed validation,
commit hash handling, metadata migration, dead-letter queue behavior,
collection name generation, ensure_collection, cached embeddings,
cache storage, embedding API calls, point ID generation, git diff,
metadata read/write, and worktree management.
"""

import hashlib
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.indexing_worker.indexing_worker import (
    _CACHE_KEY,
    _DLQ_KEY,
    _META_KEY,
    _QUEUE_KEY,
    EMBEDDING_BATCH_SIZE,
    MAX_JOB_RETRIES,
    _cache_embeddings,
    _cleanup_worktree,
    _collection_name,
    _content_hash,
    _create_worktree,
    _embed_texts,
    _enqueue_for_retry,
    _get_cached_embeddings,
    _get_previous_commit,
    _git_diff_files,
    _is_transient_error,
    _migrate_meta_key,
    _point_id,
    _update_indexing_metadata,
    batch_embed,
    ensure_collection,
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
    async def test_partial_embeddings_skips_missing(self):
        """If _embed_texts returns fewer embeddings than texts, batch_embed skips missing ones."""
        with patch(
            "services.indexing_worker.indexing_worker._embed_texts",
            new_callable=AsyncMock,
            return_value=(
                [[0.1, 0.2]],  # Only 1 embedding
                [0],  # Only index 0 is valid
            ),
        ):
            result = await batch_embed(["text1", "text2"])
            # Only the valid embedding should be returned
            assert len(result) == 1
            assert result[0] == [0.1, 0.2]


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
        assert dlq_entry["original_message"]["repo"] == "owner/repo"
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


# ---------------------------------------------------------------------------
# ensure_collection
# ---------------------------------------------------------------------------


class TestEnsureCollection:
    @pytest.mark.asyncio
    async def test_creates_new_collection(self):
        """Should create a new Qdrant collection when it does not exist."""
        mock_client = MagicMock()
        mock_collection = MagicMock()
        mock_collection.name = "other_collection"
        mock_client.get_collections.return_value = MagicMock(
            collections=[mock_collection]
        )
        mock_client.close = MagicMock()

        with patch(
            "qdrant_client.QdrantClient",
            return_value=mock_client,
        ):
            result = await ensure_collection("owner/repo")

        assert result == "owner__repo"
        mock_client.create_collection.assert_called_once()
        mock_client.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_if_collection_exists(self):
        """Should not create collection if it already exists."""
        mock_client = MagicMock()
        mock_collection = MagicMock()
        mock_collection.name = "owner__repo"
        mock_client.get_collections.return_value = MagicMock(
            collections=[mock_collection]
        )
        mock_client.close = MagicMock()

        with patch(
            "qdrant_client.QdrantClient",
            return_value=mock_client,
        ):
            result = await ensure_collection("owner/repo")

        assert result == "owner__repo"
        mock_client.create_collection.assert_not_called()

    @pytest.mark.asyncio
    async def test_closes_client_in_finally(self):
        """Should close the Qdrant client even when an error occurs."""
        mock_client = MagicMock()
        mock_client.get_collections.side_effect = Exception("Qdrant down")
        mock_client.close = MagicMock()

        with patch(
            "qdrant_client.QdrantClient",
            return_value=mock_client,
        ):
            with pytest.raises(Exception, match="Qdrant down"):
                await ensure_collection("owner/repo")

        mock_client.close.assert_called_once()


# ---------------------------------------------------------------------------
# _get_cached_embeddings
# ---------------------------------------------------------------------------


class TestGetCachedEmbeddings:
    @pytest.mark.asyncio
    async def test_all_hits(self):
        """All contents cached should return no miss indices."""
        redis_client = MagicMock()
        cached_val = json.dumps([0.1, 0.2, 0.3])
        pipe = MagicMock()
        pipe.execute = AsyncMock(return_value=[cached_val, cached_val])
        redis_client.pipeline = MagicMock(return_value=pipe)

        results, misses = await _get_cached_embeddings(
            redis_client, ["text A", "text B"]
        )

        assert misses == []
        assert all(r is not None for r in results)

    @pytest.mark.asyncio
    async def test_all_misses(self):
        """No cached values should return all indices as misses."""
        redis_client = MagicMock()
        pipe = MagicMock()
        pipe.execute = AsyncMock(return_value=[None, None])
        redis_client.pipeline = MagicMock(return_value=pipe)

        results, misses = await _get_cached_embeddings(
            redis_client, ["text A", "text B"]
        )

        assert misses == [0, 1]
        assert all(r is None for r in results)

    @pytest.mark.asyncio
    async def test_partial_hits(self):
        """Mix of cached and missing values should return correct indices."""
        redis_client = MagicMock()
        cached_val = json.dumps([0.5, 0.6])
        pipe = MagicMock()
        pipe.execute = AsyncMock(return_value=[cached_val, None, cached_val])
        redis_client.pipeline = MagicMock(return_value=pipe)

        results, misses = await _get_cached_embeddings(
            redis_client, ["text A", "text B", "text C"]
        )

        assert results[0] is not None
        assert results[1] is None
        assert results[2] is not None
        assert misses == [1]

    @pytest.mark.asyncio
    async def test_returns_all_misses_on_redis_error(self):
        """Redis error should be caught; return all None and all indices."""
        redis_client = MagicMock()
        pipe = MagicMock()
        pipe.execute = AsyncMock(side_effect=Exception("Redis down"))
        redis_client.pipeline = MagicMock(return_value=pipe)

        results, misses = await _get_cached_embeddings(
            redis_client, ["text A", "text B"]
        )

        assert results == [None, None]
        assert misses == [0, 1]


# ---------------------------------------------------------------------------
# _cache_embeddings
# ---------------------------------------------------------------------------


class TestCacheEmbeddings:
    @pytest.mark.asyncio
    async def test_stores_embeddings(self):
        """Should store embeddings in Redis hash via hset."""
        redis_client = AsyncMock()
        contents = ["chunk A", "chunk B"]
        embeddings = [[0.1, 0.2], [0.3, 0.4]]

        await _cache_embeddings(redis_client, contents, embeddings)

        redis_client.hset.assert_called_once()
        call_args = redis_client.hset.call_args
        assert call_args[0][0] == _CACHE_KEY
        mapping = call_args[1]["mapping"]
        assert len(mapping) == 2

    @pytest.mark.asyncio
    async def test_skips_on_empty_mapping(self):
        """Empty contents list should result in no Redis calls."""
        redis_client = AsyncMock()

        await _cache_embeddings(redis_client, [], [])

        redis_client.hset.assert_not_called()


# ---------------------------------------------------------------------------
# _embed_texts
# ---------------------------------------------------------------------------


class TestEmbedTexts:
    @pytest.mark.asyncio
    async def test_batches_correctly(self):
        """Should split texts into batches of EMBEDDING_BATCH_SIZE."""
        texts = [f"text {i}" for i in range(EMBEDDING_BATCH_SIZE + 5)]
        call_counts = []

        def fake_embed_content(*args, **kwargs):
            call_counts.append(1)
            batch_contents = kwargs.get("contents") or args[1]
            emb = [MagicMock(values=[0.1] * 10)] * len(batch_contents)
            result = MagicMock()
            result.embeddings = emb
            return result

        mock_client = MagicMock()
        mock_client.models.embed_content = fake_embed_content

        with (
            patch("google.genai.Client", return_value=mock_client),
            patch(
                "services.indexing_worker.indexing_worker.asyncio.to_thread",
                new_callable=AsyncMock,
                side_effect=lambda fn, *a, **kw: fn(*a, **kw),
            ),
        ):
            embeddings, indices = await _embed_texts(texts)

        expected_batches = (
            len(texts) + EMBEDDING_BATCH_SIZE - 1
        ) // EMBEDDING_BATCH_SIZE
        assert len(call_counts) == expected_batches
        assert len(embeddings) == len(texts)

    @pytest.mark.asyncio
    async def test_retries_on_rate_limit(self):
        """Should retry when encountering a 429 rate limit error."""
        call_count = 0

        def fake_embed_content(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("429 Rate Limited")
            result = MagicMock()
            result.embeddings = [MagicMock(values=[0.1] * 10)]
            return result

        mock_client = MagicMock()
        mock_client.models.embed_content = fake_embed_content

        with (
            patch("google.genai.Client", return_value=mock_client),
            patch(
                "services.indexing_worker.indexing_worker.asyncio.sleep",
                new_callable=AsyncMock,
            ) as mock_sleep,
            patch(
                "services.indexing_worker.indexing_worker.asyncio.to_thread",
                new_callable=AsyncMock,
                side_effect=lambda fn, *a, **kw: fn(*a, **kw),
            ),
        ):
            result_embeddings, valid_indices = await _embed_texts(["text1"])

        assert call_count == 2
        mock_sleep.assert_called_once()
        assert len(result_embeddings) == 1

    @pytest.mark.asyncio
    async def test_raises_after_max_retries(self):
        """Should raise when all retry attempts fail with rate limit."""
        mock_client = MagicMock()
        mock_client.models.embed_content = MagicMock(
            side_effect=Exception("429 Rate Limited")
        )

        with (
            patch("google.genai.Client", return_value=mock_client),
            patch(
                "services.indexing_worker.indexing_worker.asyncio.sleep",
                new_callable=AsyncMock,
            ),
            patch(
                "services.indexing_worker.indexing_worker.asyncio.to_thread",
                new_callable=AsyncMock,
                side_effect=lambda fn, *a, **kw: fn(*a, **kw),
            ),
        ):
            with pytest.raises(Exception, match="429 Rate Limited"):
                await _embed_texts(["text1"])


# ---------------------------------------------------------------------------
# _point_id
# ---------------------------------------------------------------------------


class TestPointId:
    def test_deterministic(self):
        """Same inputs should always produce the same UUID."""
        id1 = _point_id("src/app.py", 10, "function", "hello")
        id2 = _point_id("src/app.py", 10, "function", "hello")
        assert id1 == id2
        # Should be a valid UUID
        uuid.UUID(id1)

    def test_different_for_different_inputs(self):
        """Different inputs should produce different UUIDs."""
        id1 = _point_id("src/app.py", 10, "function", "hello")
        id2 = _point_id("src/app.py", 20, "function", "hello")
        id3 = _point_id("src/app.py", 10, "class", "Hello")
        assert id1 != id2
        assert id1 != id3
        assert id2 != id3


# ---------------------------------------------------------------------------
# _git_diff_files
# ---------------------------------------------------------------------------


class TestGitDiffFiles:
    @pytest.mark.asyncio
    async def test_changed_files(self):
        """Should parse changed file paths from git diff output."""
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(
            return_value=(b"src/app.py\nsrc/utils.py\n", b"")
        )
        mock_proc.returncode = 0

        with (
            patch(
                "services.indexing_worker.indexing_worker.asyncio.create_subprocess_exec",
                return_value=mock_proc,
            ),
            patch(
                "services.indexing_worker.indexing_worker.asyncio.wait_for",
                new_callable=AsyncMock,
                return_value=(b"src/app.py\nsrc/utils.py\n", b""),
            ),
        ):
            files = await _git_diff_files("/tmp/wt", "abc", "def")

        assert files == ["src/app.py", "src/utils.py"]

    @pytest.mark.asyncio
    async def test_deleted_files(self):
        """Should parse deleted file paths from --name-status output."""
        output = b"D\tsrc/old.py\nR100\tsrc/renamed_old.py\tsrc/renamed_new.py\n"
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(output, b""))
        mock_proc.returncode = 0

        with (
            patch(
                "services.indexing_worker.indexing_worker.asyncio.create_subprocess_exec",
                return_value=mock_proc,
            ),
            patch(
                "services.indexing_worker.indexing_worker.asyncio.wait_for",
                new_callable=AsyncMock,
                return_value=(output, b""),
            ),
        ):
            files = await _git_diff_files("/tmp/wt", "abc", "def", deleted_only=True)

        assert "src/old.py" in files
        assert "src/renamed_old.py" in files

    @pytest.mark.asyncio
    async def test_returns_empty_on_nonzero_exit(self):
        """Non-zero exit code should return empty list."""
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b"fatal: error"))
        mock_proc.returncode = 128

        with (
            patch(
                "services.indexing_worker.indexing_worker.asyncio.create_subprocess_exec",
                return_value=mock_proc,
            ),
            patch(
                "services.indexing_worker.indexing_worker.asyncio.wait_for",
                new_callable=AsyncMock,
                return_value=(b"", b"fatal: error"),
            ),
        ):
            files = await _git_diff_files("/tmp/wt", "abc", "def")

        assert files == []


# ---------------------------------------------------------------------------
# _get_previous_commit
# ---------------------------------------------------------------------------


class TestGetPreviousCommit:
    @pytest.mark.asyncio
    async def test_returns_commit_from_metadata(self):
        """Should return indexed_commit from stored metadata."""
        redis_client = AsyncMock()
        meta = json.dumps({"indexed_commit": "abc123", "chunk_count": 10})
        redis_client.hget.return_value = meta

        result = await _get_previous_commit(redis_client, "owner/repo", "main")

        assert result == "abc123"
        key = _META_KEY.format(repo="owner/repo")
        redis_client.hget.assert_called_once_with(key, "main")

    @pytest.mark.asyncio
    async def test_returns_none_when_no_metadata(self):
        """Should return None when no metadata exists."""
        redis_client = AsyncMock()
        redis_client.hget.return_value = None

        result = await _get_previous_commit(redis_client, "owner/repo", "main")

        assert result is None


# ---------------------------------------------------------------------------
# _update_indexing_metadata
# ---------------------------------------------------------------------------


class TestUpdateIndexingMetadata:
    @pytest.mark.asyncio
    async def test_stores_metadata(self):
        """Should store metadata in Redis hash with correct fields."""
        redis_client = AsyncMock()

        await _update_indexing_metadata(
            repo="owner/repo",
            collection="owner__repo",
            commit_hash="abc123def",
            chunk_count=42,
            ref="main",
            redis_client=redis_client,
        )

        redis_client.hset.assert_called_once()
        call_args = redis_client.hset.call_args
        key = _META_KEY.format(repo="owner/repo")
        assert call_args[0][0] == key
        assert call_args[0][1] == "main"
        stored = json.loads(call_args[0][2])
        assert stored["collection_name"] == "owner__repo"
        assert stored["indexed_commit"] == "abc123def"
        assert stored["chunk_count"] == 42


# ---------------------------------------------------------------------------
# _create_worktree
# ---------------------------------------------------------------------------


class TestCreateWorktree:
    @pytest.mark.asyncio
    async def test_creates_from_bare_repo(self):
        """Should run correct git worktree add command."""
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_proc.returncode = 0

        with (
            patch(
                "services.indexing_worker.indexing_worker.os.path.isdir",
                return_value=True,
            ),
            patch(
                "services.indexing_worker.indexing_worker.tempfile.mkdtemp",
                return_value="/tmp/idx_owner_repo_test",
            ),
            patch(
                "services.indexing_worker.indexing_worker.asyncio.create_subprocess_exec",
                return_value=mock_proc,
            ) as mock_exec,
            patch(
                "services.indexing_worker.indexing_worker.asyncio.wait_for",
                new_callable=AsyncMock,
                return_value=(b"", b""),
            ),
        ):
            result = await _create_worktree("owner/repo", "main")

        assert result == "/tmp/idx_owner_repo_test"
        # Verify the first call was to create worktree
        call_args = mock_exec.call_args_list[0]
        assert "worktree" in call_args[0]
        assert "add" in call_args[0]

    @pytest.mark.asyncio
    async def test_returns_none_on_missing_bare_repo(self):
        """Should return None when bare repo directory does not exist."""
        with patch(
            "services.indexing_worker.indexing_worker.os.path.isdir",
            return_value=False,
        ):
            result = await _create_worktree("owner/repo", "main")

        assert result is None


# ---------------------------------------------------------------------------
# _cleanup_worktree
# ---------------------------------------------------------------------------


class TestCleanupWorktree:
    @pytest.mark.asyncio
    async def test_removes_via_git(self):
        """Should call git worktree remove --force."""
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))

        with (
            patch(
                "services.indexing_worker.indexing_worker.asyncio.create_subprocess_exec",
                return_value=mock_proc,
            ),
            patch(
                "services.indexing_worker.indexing_worker.shutil.rmtree"
            ) as mock_rmtree,
        ):
            await _cleanup_worktree("owner/repo", "/tmp/worktree")

        mock_rmtree.assert_called_once_with("/tmp/worktree", ignore_errors=True)

    @pytest.mark.asyncio
    async def test_falls_back_to_shutil_on_git_failure(self):
        """Should still call shutil.rmtree even if git worktree remove fails."""
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b"error"))

        with (
            patch(
                "services.indexing_worker.indexing_worker.asyncio.create_subprocess_exec",
                return_value=mock_proc,
            ),
            patch(
                "services.indexing_worker.indexing_worker.shutil.rmtree"
            ) as mock_rmtree,
        ):
            await _cleanup_worktree("owner/repo", "/tmp/worktree")

        mock_rmtree.assert_called_once_with("/tmp/worktree", ignore_errors=True)
