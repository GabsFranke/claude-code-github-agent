"""Unit tests for JobQueue module."""

import json
from unittest.mock import AsyncMock, patch

import pytest

from shared.exceptions import QueueError
from shared.job_queue import JobQueue


class TestJobQueueInitialization:
    """Test JobQueue initialization."""

    def test_initialization_with_defaults(self):
        """Test JobQueue initialization with default values."""
        queue = JobQueue(redis_url="redis://localhost:6379")
        assert queue.redis_url == "redis://localhost:6379"
        assert queue.password is None
        assert queue.job_ttl == 3600
        assert queue.redis is None

    def test_initialization_with_password(self):
        """Test JobQueue initialization with password."""
        queue = JobQueue(
            redis_url="redis://localhost:6379", password="secret", job_ttl=7200
        )
        assert queue.password == "secret"
        assert queue.job_ttl == 7200

    def test_redis_keys_are_set(self):
        """Test Redis key prefixes are properly set."""
        queue = JobQueue(redis_url="redis://localhost:6379")
        assert queue.pending_queue == "agent:jobs:pending"
        assert queue.processing_set == "agent:jobs:processing"
        assert queue.job_data_prefix == "agent:job:data:"
        assert queue.job_result_prefix == "agent:job:result:"
        assert queue.job_status_prefix == "agent:job:status:"


class TestJobQueueCreateJob:
    """Test create_job method."""

    @pytest.mark.asyncio
    async def test_create_job_success(self):
        """Test successful job creation."""
        queue = JobQueue(redis_url="redis://localhost:6379")

        # Mock Redis client
        mock_redis = AsyncMock()
        mock_redis.setex = AsyncMock()
        mock_redis.rpush = AsyncMock()
        queue.redis = mock_redis

        job_data = {
            "repo": "owner/repo",
            "issue_number": 123,
            "prompt": "Test prompt",
            "github_token": "token",
            "user": "testuser",
            "auto_review": False,
            "auto_triage": False,
        }

        job_id = await queue.create_job(job_data)

        # Verify job_id is a UUID
        assert len(job_id) == 36
        assert job_id.count("-") == 4

        # Verify Redis calls
        assert mock_redis.setex.call_count == 2  # job data + status
        assert mock_redis.rpush.call_count == 1

    @pytest.mark.asyncio
    async def test_create_job_serialization_error(self):
        """Test job creation with non-serializable data."""
        queue = JobQueue(redis_url="redis://localhost:6379")
        queue.redis = AsyncMock()

        # Non-serializable object
        job_data = {"data": object()}

        with pytest.raises(QueueError, match="Failed to serialize job data"):
            await queue.create_job(job_data)

    @pytest.mark.asyncio
    async def test_create_job_redis_error(self):
        """Test job creation with Redis error."""
        queue = JobQueue(redis_url="redis://localhost:6379")

        mock_redis = AsyncMock()
        mock_redis.setex = AsyncMock(side_effect=OSError("Connection failed"))
        queue.redis = mock_redis

        job_data = {"repo": "owner/repo", "issue_number": 123}

        with pytest.raises(QueueError, match="Failed to create job in Redis"):
            await queue.create_job(job_data)


class TestJobQueueGetNextJob:
    """Test get_next_job method."""

    @pytest.mark.asyncio
    async def test_get_next_job_success(self):
        """Test successfully getting next job."""
        queue = JobQueue(redis_url="redis://localhost:6379")

        job_id = "550e8400-e29b-41d4-a716-446655440000"  # Valid UUID
        job_data = {"repo": "owner/repo", "issue_number": 456}

        mock_redis = AsyncMock()
        mock_redis.blpop = AsyncMock(return_value=("agent:jobs:pending", job_id))
        mock_redis.get = AsyncMock(return_value=json.dumps(job_data))
        mock_redis.sadd = AsyncMock()
        mock_redis.setex = AsyncMock()
        queue.redis = mock_redis

        result = await queue.get_next_job(timeout=5)

        assert result is not None
        assert result[0] == job_id
        assert result[1] == job_data

        # Verify job marked as processing
        mock_redis.sadd.assert_called_once_with(queue.processing_set, job_id)

    @pytest.mark.asyncio
    async def test_get_next_job_timeout(self):
        """Test get_next_job returns None on timeout."""
        queue = JobQueue(redis_url="redis://localhost:6379")

        mock_redis = AsyncMock()
        mock_redis.blpop = AsyncMock(return_value=None)
        queue.redis = mock_redis

        result = await queue.get_next_job(timeout=1)

        assert result is None

    @pytest.mark.asyncio
    async def test_get_next_job_expired_data(self):
        """Test get_next_job when job data has expired."""
        queue = JobQueue(redis_url="redis://localhost:6379")

        mock_redis = AsyncMock()
        mock_redis.blpop = AsyncMock(return_value=("queue", "expired-job"))
        mock_redis.get = AsyncMock(return_value=None)  # Data expired
        queue.redis = mock_redis

        result = await queue.get_next_job(timeout=5)

        assert result is None

    @pytest.mark.asyncio
    async def test_get_next_job_corrupted_json(self):
        """Test get_next_job with corrupted JSON data."""
        queue = JobQueue(redis_url="redis://localhost:6379")

        mock_redis = AsyncMock()
        mock_redis.blpop = AsyncMock(return_value=("queue", "corrupted-job"))
        mock_redis.get = AsyncMock(return_value="invalid json{")
        queue.redis = mock_redis

        result = await queue.get_next_job(timeout=5)

        # Should return None and log error
        assert result is None

    @pytest.mark.asyncio
    async def test_get_next_job_redis_error(self):
        """Test get_next_job with Redis connection error."""
        queue = JobQueue(redis_url="redis://localhost:6379")

        mock_redis = AsyncMock()
        mock_redis.blpop = AsyncMock(side_effect=OSError("Connection lost"))
        queue.redis = mock_redis

        result = await queue.get_next_job(timeout=5)

        assert result is None


class TestJobQueueCompleteJob:
    """Test complete_job method."""

    @pytest.mark.asyncio
    async def test_complete_job_success(self):
        """Test successfully completing a job."""
        queue = JobQueue(redis_url="redis://localhost:6379")

        mock_redis = AsyncMock()
        mock_redis.setex = AsyncMock()
        mock_redis.srem = AsyncMock()
        queue.redis = mock_redis

        job_id = "test-job-789"
        result = {
            "status": "success",
            "response": "Job completed",
            "repo": "owner/repo",
            "issue_number": 123,
        }

        await queue.complete_job(job_id, result, status="success")

        # Verify result stored
        assert mock_redis.setex.call_count == 2  # result + status
        mock_redis.srem.assert_called_once_with(queue.processing_set, job_id)

    @pytest.mark.asyncio
    async def test_complete_job_error_status(self):
        """Test completing a job with error status."""
        queue = JobQueue(redis_url="redis://localhost:6379")

        mock_redis = AsyncMock()
        mock_redis.setex = AsyncMock()
        mock_redis.srem = AsyncMock()
        queue.redis = mock_redis

        job_id = "failed-job"
        result = {
            "status": "error",
            "error": "Execution failed",
            "repo": "owner/repo",
            "issue_number": 456,
        }

        await queue.complete_job(job_id, result, status="error")

        # Verify error status stored
        mock_redis.setex.assert_any_call(
            f"{queue.job_status_prefix}{job_id}", queue.job_ttl, "error"
        )

    @pytest.mark.asyncio
    async def test_complete_job_serialization_error(self):
        """Test complete_job with non-serializable result."""
        queue = JobQueue(redis_url="redis://localhost:6379")
        queue.redis = AsyncMock()

        result = {"data": object()}  # Non-serializable

        with pytest.raises(QueueError, match="Failed to serialize result"):
            await queue.complete_job("job-id", result)

    @pytest.mark.asyncio
    async def test_complete_job_redis_error(self):
        """Test complete_job with Redis error."""
        queue = JobQueue(redis_url="redis://localhost:6379")

        mock_redis = AsyncMock()
        mock_redis.setex = AsyncMock(side_effect=OSError("Connection failed"))
        queue.redis = mock_redis

        with pytest.raises(QueueError, match="Failed to complete job in Redis"):
            await queue.complete_job("job-id", {"status": "success"})


class TestJobQueueGetJobStatus:
    """Test get_job_status method."""

    @pytest.mark.asyncio
    async def test_get_job_status_success(self):
        """Test getting job status."""
        queue = JobQueue(redis_url="redis://localhost:6379")

        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value="processing")
        queue.redis = mock_redis

        status = await queue.get_job_status("test-job")

        assert status == "processing"

    @pytest.mark.asyncio
    async def test_get_job_status_not_found(self):
        """Test getting status for non-existent job."""
        queue = JobQueue(redis_url="redis://localhost:6379")

        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=None)
        queue.redis = mock_redis

        status = await queue.get_job_status("missing-job")

        assert status is None

    @pytest.mark.asyncio
    async def test_get_job_status_redis_error(self):
        """Test get_job_status with Redis error."""
        queue = JobQueue(redis_url="redis://localhost:6379")

        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(side_effect=OSError("Connection failed"))
        queue.redis = mock_redis

        status = await queue.get_job_status("job-id")

        assert status is None


class TestJobQueueGetJobResult:
    """Test get_job_result method."""

    @pytest.mark.asyncio
    async def test_get_job_result_success(self):
        """Test getting job result."""
        queue = JobQueue(redis_url="redis://localhost:6379")

        result_data = {"status": "success", "response": "Done"}
        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=json.dumps(result_data))
        queue.redis = mock_redis

        result = await queue.get_job_result("test-job")

        assert result == result_data

    @pytest.mark.asyncio
    async def test_get_job_result_not_found(self):
        """Test getting result for non-existent job."""
        queue = JobQueue(redis_url="redis://localhost:6379")

        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=None)
        queue.redis = mock_redis

        result = await queue.get_job_result("missing-job")

        assert result is None

    @pytest.mark.asyncio
    async def test_get_job_result_corrupted_json(self):
        """Test get_job_result with corrupted JSON."""
        queue = JobQueue(redis_url="redis://localhost:6379")

        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value="invalid json{")
        queue.redis = mock_redis

        result = await queue.get_job_result("job-id")

        assert result is None


class TestJobQueueMetrics:
    """Test queue metrics methods."""

    @pytest.mark.asyncio
    async def test_get_queue_depth(self):
        """Test getting queue depth."""
        queue = JobQueue(redis_url="redis://localhost:6379")

        mock_redis = AsyncMock()
        mock_redis.llen = AsyncMock(return_value=5)
        queue.redis = mock_redis

        depth = await queue.get_queue_depth()

        assert depth == 5

    @pytest.mark.asyncio
    async def test_get_queue_depth_redis_error(self):
        """Test get_queue_depth with Redis error."""
        queue = JobQueue(redis_url="redis://localhost:6379")

        mock_redis = AsyncMock()
        mock_redis.llen = AsyncMock(side_effect=OSError("Connection failed"))
        queue.redis = mock_redis

        depth = await queue.get_queue_depth()

        assert depth == 0

    @pytest.mark.asyncio
    async def test_get_processing_count(self):
        """Test getting processing count."""
        queue = JobQueue(redis_url="redis://localhost:6379")

        mock_redis = AsyncMock()
        mock_redis.scard = AsyncMock(return_value=3)
        queue.redis = mock_redis

        count = await queue.get_processing_count()

        assert count == 3

    @pytest.mark.asyncio
    async def test_get_processing_count_redis_error(self):
        """Test get_processing_count with Redis error."""
        queue = JobQueue(redis_url="redis://localhost:6379")

        mock_redis = AsyncMock()
        mock_redis.scard = AsyncMock(side_effect=OSError("Connection failed"))
        queue.redis = mock_redis

        count = await queue.get_processing_count()

        assert count == 0


class TestJobQueueClose:
    """Test close method."""

    @pytest.mark.asyncio
    async def test_close_connection(self):
        """Test closing Redis connection."""
        queue = JobQueue(redis_url="redis://localhost:6379")

        mock_redis = AsyncMock()
        queue.redis = mock_redis

        await queue.close()

        mock_redis.aclose.assert_called_once()
        assert queue.redis is None

    @pytest.mark.asyncio
    async def test_close_when_not_connected(self):
        """Test closing when no connection exists."""
        queue = JobQueue(redis_url="redis://localhost:6379")
        queue.redis = None

        # Should not raise error
        await queue.close()

        assert queue.redis is None


class TestJobQueueConnect:
    """Test _connect method."""

    @pytest.mark.asyncio
    async def test_connect_success(self):
        """Test successful Redis connection."""
        queue = JobQueue(redis_url="redis://localhost:6379", password="secret")

        mock_redis = AsyncMock()

        async def mock_from_url(*args, **kwargs):
            return mock_redis

        with patch("redis.asyncio.from_url", side_effect=mock_from_url):
            await queue._connect()

            assert queue.redis == mock_redis

    @pytest.mark.asyncio
    async def test_connect_already_connected(self):
        """Test _connect when already connected."""
        queue = JobQueue(redis_url="redis://localhost:6379")
        existing_redis = AsyncMock()
        queue.redis = existing_redis

        await queue._connect()

        # Should not create new connection
        assert queue.redis == existing_redis

    @pytest.mark.asyncio
    async def test_connect_import_error(self):
        """Test _connect with missing redis package."""
        queue = JobQueue(redis_url="redis://localhost:6379")

        with patch("redis.asyncio.from_url", side_effect=ImportError("No redis")):
            with pytest.raises(QueueError, match="redis package is required"):
                await queue._connect()

    @pytest.mark.asyncio
    async def test_connect_connection_error(self):
        """Test _connect with connection failure."""
        queue = JobQueue(redis_url="redis://localhost:6379")

        with patch("redis.asyncio.from_url", side_effect=OSError("Connection refused")):
            with pytest.raises(QueueError, match="Failed to connect to Redis"):
                await queue._connect()


class TestJobQueueReclaimStaleJobs:
    """Test reclaim_stale_jobs method, especially github_token stripping."""

    @pytest.mark.asyncio
    async def test_reclaim_strips_github_token(self):
        """Reclaimed jobs have github_token removed so a fresh one is generated."""
        queue = JobQueue(redis_url="redis://localhost:6379")

        stale_job_id = "550e8400-e29b-41d4-a716-446655440099"
        original_data = {
            "repo": "owner/repo",
            "issue_number": 123,
            "prompt": "Test prompt",
            "github_token": "ghs_expired_token_12345",
            "installation_id": 112053500,
            "user": "testuser",
        }
        backup_json = json.dumps(original_data)

        mock_redis = AsyncMock()
        # smembers returns the stale job ID
        mock_redis.smembers = AsyncMock(return_value={stale_job_id})
        # exists returns 0 — job data has expired
        mock_redis.exists = AsyncMock(return_value=0)
        # get returns the backup JSON
        mock_redis.get = AsyncMock(return_value=backup_json.encode())
        mock_redis.setex = AsyncMock()
        mock_redis.rpush = AsyncMock()
        mock_redis.srem = AsyncMock()
        mock_redis.delete = AsyncMock()
        queue.redis = mock_redis

        reclaimed = await queue.reclaim_stale_jobs()

        assert reclaimed == 1

        # Find the setex call that restores job data and verify
        # github_token was stripped
        for call in mock_redis.setex.call_args_list:
            args = call[0]
            if args[0] == f"{queue.job_data_prefix}{stale_job_id}":
                restored_data = json.loads(args[2])
                assert "github_token" not in restored_data
                assert restored_data.get("installation_id") == 112053500
                break
        else:
            pytest.fail("Expected setex call for job data restoration")

    @pytest.mark.asyncio
    async def test_reclaim_preserves_data_without_token(self):
        """Jobs without github_token should pass through unchanged."""
        queue = JobQueue(redis_url="redis://localhost:6379")

        stale_job_id = "550e8400-e29b-41d4-a716-446655440100"
        original_data = {
            "repo": "owner/repo",
            "issue_number": 456,
            "prompt": "Another prompt",
            "installation_id": 999,
            "user": "testuser",
        }
        backup_json = json.dumps(original_data)

        mock_redis = AsyncMock()
        mock_redis.smembers = AsyncMock(return_value={stale_job_id})
        mock_redis.exists = AsyncMock(return_value=0)
        mock_redis.get = AsyncMock(return_value=backup_json.encode())
        mock_redis.setex = AsyncMock()
        mock_redis.rpush = AsyncMock()
        mock_redis.srem = AsyncMock()
        mock_redis.delete = AsyncMock()
        queue.redis = mock_redis

        reclaimed = await queue.reclaim_stale_jobs()

        assert reclaimed == 1

        for call in mock_redis.setex.call_args_list:
            args = call[0]
            if args[0] == f"{queue.job_data_prefix}{stale_job_id}":
                restored_data = json.loads(args[2])
                # No github_token was in original, and none should be added
                assert "github_token" not in restored_data
                assert restored_data == original_data
                break
        else:
            pytest.fail("Expected setex call for job data restoration")

    @pytest.mark.asyncio
    async def test_reclaim_handles_corrupt_json(self):
        """Corrupt backup JSON should be restored as-is (not crash)."""
        queue = JobQueue(redis_url="redis://localhost:6379")

        stale_job_id = "550e8400-e29b-41d4-a716-446655440101"

        mock_redis = AsyncMock()
        mock_redis.smembers = AsyncMock(return_value={stale_job_id})
        mock_redis.exists = AsyncMock(return_value=0)
        # Corrupt JSON (bytes, as Redis returns)
        corrupt_bytes = b"not valid json{"
        mock_redis.get = AsyncMock(return_value=corrupt_bytes)
        mock_redis.setex = AsyncMock()
        mock_redis.rpush = AsyncMock()
        mock_redis.srem = AsyncMock()
        mock_redis.delete = AsyncMock()
        queue.redis = mock_redis

        reclaimed = await queue.reclaim_stale_jobs()

        assert reclaimed == 1

        # Verify corrupt JSON was passed through unchanged
        for call in mock_redis.setex.call_args_list:
            args = call[0]
            if args[0] == f"{queue.job_data_prefix}{stale_job_id}":
                assert args[2] == corrupt_bytes
                break
        else:
            pytest.fail("Expected setex call for job data restoration")
