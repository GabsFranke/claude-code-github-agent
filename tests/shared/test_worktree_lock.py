"""Tests for shared/worktree_lock.py — WorktreeLock, WorktreeKey, and helpers."""

import json
import os
import signal
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.worktree_lock import (
    CANCEL_CHANNEL_PREFIX,
    LOCK_PREFIX,
    PENDING_PREFIX,
    WorktreeKey,
    WorktreeLock,
    interrupt_sdk_process,
)


class TestWorktreeKey:
    def test_str_format(self):
        key = WorktreeKey(
            repo="owner/repo", thread_type="pr", thread_id="42", workflow="review"
        )
        assert str(key) == "owner--repo:pr-42:review"

    def test_lock_key(self):
        key = WorktreeKey(
            repo="owner/repo", thread_type="pr", thread_id="42", workflow="review"
        )
        assert key.lock_key.startswith(LOCK_PREFIX)
        assert key.lock_key == f"{LOCK_PREFIX}owner--repo:pr-42:review"

    def test_pending_key(self):
        key = WorktreeKey(
            repo="owner/repo", thread_type="pr", thread_id="42", workflow="review"
        )
        assert key.pending_key.startswith(PENDING_PREFIX)
        assert key.pending_key == f"{PENDING_PREFIX}owner--repo:pr-42:review"

    def test_cancel_channel(self):
        key = WorktreeKey(
            repo="owner/repo", thread_type="pr", thread_id="42", workflow="review"
        )
        assert key.cancel_channel.startswith(CANCEL_CHANNEL_PREFIX)
        assert key.cancel_channel == f"{CANCEL_CHANNEL_PREFIX}owner--repo:pr-42:review"


class TestAcquire:
    @pytest.mark.asyncio
    async def test_acquire_success(self):
        redis = MagicMock()
        redis.set = AsyncMock(return_value=True)
        key = WorktreeKey(
            repo="owner/repo", thread_type="pr", thread_id="42", workflow="review"
        )
        lock = WorktreeLock(redis, key)
        result = await lock.acquire(job_id="job-123")
        assert result is True
        assert lock._lock_acquired is True

    @pytest.mark.asyncio
    async def test_acquire_failure_no_wait(self):
        redis = MagicMock()
        redis.set = AsyncMock(return_value=None)
        key = WorktreeKey(
            repo="owner/repo", thread_type="pr", thread_id="42", workflow="review"
        )
        lock = WorktreeLock(redis, key)
        result = await lock.acquire(job_id="job-123")
        assert result is False
        assert lock._lock_acquired is False

    @pytest.mark.asyncio
    async def test_acquire_calls_redis_with_nx_and_ex(self):
        redis = MagicMock()
        redis.set = AsyncMock(return_value=True)
        key = WorktreeKey(
            repo="owner/repo", thread_type="pr", thread_id="42", workflow="review"
        )
        lock = WorktreeLock(redis, key)
        await lock.acquire(job_id="job-123", ttl=120)

        assert redis.set.call_args.kwargs["nx"] is True
        assert redis.set.call_args.kwargs["ex"] == 120

    @pytest.mark.asyncio
    async def test_acquire_lock_value_contains_pid(self):
        redis = MagicMock()
        redis.set = AsyncMock(return_value=True)
        key = WorktreeKey(
            repo="owner/repo", thread_type="pr", thread_id="42", workflow="review"
        )
        lock = WorktreeLock(redis, key)
        await lock.acquire(job_id="job-123")

        raw_value = redis.set.call_args[0][1]
        data = json.loads(raw_value)
        assert data["job_id"] == "job-123"
        assert data["status"] == "running"
        assert data["pid"] == os.getpid()
        assert data["session_id"] is None


class TestRelease:
    @pytest.mark.asyncio
    async def test_release_deletes_key(self):
        redis = MagicMock()
        redis.set = AsyncMock(return_value=True)
        redis.delete = AsyncMock(return_value=1)
        key = WorktreeKey(
            repo="owner/repo", thread_type="pr", thread_id="42", workflow="review"
        )
        lock = WorktreeLock(redis, key)
        await lock.acquire(job_id="job-123")
        await lock.release()

        redis.delete.assert_called_once_with(key.lock_key)

    @pytest.mark.asyncio
    async def test_release_resets_lock_acquired(self):
        redis = MagicMock()
        redis.set = AsyncMock(return_value=True)
        redis.delete = AsyncMock(return_value=1)
        key = WorktreeKey(
            repo="owner/repo", thread_type="pr", thread_id="42", workflow="review"
        )
        lock = WorktreeLock(redis, key)
        await lock.acquire(job_id="job-123")
        assert lock._lock_acquired is True
        await lock.release()
        assert lock._lock_acquired is False

    @pytest.mark.asyncio
    async def test_release_without_acquire_no_delete(self):
        redis = MagicMock()
        redis.delete = AsyncMock(return_value=1)
        key = WorktreeKey(
            repo="owner/repo", thread_type="pr", thread_id="42", workflow="review"
        )
        lock = WorktreeLock(redis, key)
        await lock.release()
        redis.delete.assert_not_called()


class TestSetSessionId:
    @pytest.mark.asyncio
    async def test_set_session_id_calls_eval(self):
        redis = MagicMock()
        redis.set = AsyncMock(return_value=True)
        redis.eval = AsyncMock(return_value=1)
        key = WorktreeKey(
            repo="owner/repo", thread_type="pr", thread_id="42", workflow="review"
        )
        lock = WorktreeLock(redis, key)
        await lock.acquire(job_id="job-123")
        await lock.set_session_id("sess-456")

        redis.eval.assert_called_once()
        call_args = redis.eval.call_args[0]
        assert call_args[1] == 1
        assert call_args[2] == key.lock_key
        assert call_args[3] == "sess-456"

    @pytest.mark.asyncio
    async def test_set_session_id_without_acquire_no_op(self):
        redis = MagicMock()
        redis.eval = AsyncMock(return_value=1)
        key = WorktreeKey(
            repo="owner/repo", thread_type="pr", thread_id="42", workflow="review"
        )
        lock = WorktreeLock(redis, key)
        await lock.set_session_id("sess-456")
        redis.eval.assert_not_called()


class TestSetInterrupted:
    @pytest.mark.asyncio
    async def test_set_interrupted_calls_eval(self):
        redis = MagicMock()
        redis.set = AsyncMock(return_value=True)
        redis.eval = AsyncMock(return_value=1)
        key = WorktreeKey(
            repo="owner/repo", thread_type="pr", thread_id="42", workflow="review"
        )
        lock = WorktreeLock(redis, key)
        await lock.acquire(job_id="job-123")
        await lock.set_interrupted()

        redis.eval.assert_called_once()
        call_args = redis.eval.call_args[0]
        assert call_args[1] == 1
        assert call_args[2] == key.lock_key

    @pytest.mark.asyncio
    async def test_set_interrupted_without_acquire_no_op(self):
        redis = MagicMock()
        redis.eval = AsyncMock(return_value=1)
        key = WorktreeKey(
            repo="owner/repo", thread_type="pr", thread_id="42", workflow="review"
        )
        lock = WorktreeLock(redis, key)
        await lock.set_interrupted()
        redis.eval.assert_not_called()


class TestGetLockInfo:
    @pytest.mark.asyncio
    async def test_get_lock_info_found(self):
        redis = MagicMock()
        lock_data = {
            "job_id": "job-123",
            "session_id": "sess-456",
            "status": "running",
            "pid": 1234,
        }
        redis.get = AsyncMock(return_value=json.dumps(lock_data))
        key = WorktreeKey(
            repo="owner/repo", thread_type="pr", thread_id="42", workflow="review"
        )
        lock = WorktreeLock(redis, key)
        info = await lock.get_lock_info()

        assert info is not None
        assert info.job_id == "job-123"
        assert info.session_id == "sess-456"
        assert info.status == "running"
        assert info.pid == 1234

    @pytest.mark.asyncio
    async def test_get_lock_info_not_found(self):
        redis = MagicMock()
        redis.get = AsyncMock(return_value=None)
        key = WorktreeKey(
            repo="owner/repo", thread_type="pr", thread_id="42", workflow="review"
        )
        lock = WorktreeLock(redis, key)
        info = await lock.get_lock_info()
        assert info is None

    @pytest.mark.asyncio
    async def test_get_lock_info_corrupt(self):
        redis = MagicMock()
        redis.get = AsyncMock(return_value="not-json{{{")
        key = WorktreeKey(
            repo="owner/repo", thread_type="pr", thread_id="42", workflow="review"
        )
        lock = WorktreeLock(redis, key)
        info = await lock.get_lock_info()
        assert info is None


class TestPendingPrompt:
    @pytest.mark.asyncio
    async def test_set_pending_prompt_stores_with_ttl(self):
        redis = MagicMock()
        redis.setex = AsyncMock(return_value=True)
        key = WorktreeKey(
            repo="owner/repo", thread_type="pr", thread_id="42", workflow="review"
        )
        lock = WorktreeLock(redis, key)
        await lock.set_pending_prompt("job-123", "hello")

        redis.setex.assert_called_once()
        call_args = redis.setex.call_args[0]
        assert call_args[0] == key.pending_key
        assert call_args[1] == 300

    @pytest.mark.asyncio
    async def test_get_pending_prompt_returns_and_clears(self):
        redis = MagicMock()
        data = {
            "job_id": "job-123",
            "prompt": "hello",
            "timestamp": datetime.now(UTC).isoformat(),
        }
        redis.get = AsyncMock(return_value=json.dumps(data))
        redis.delete = AsyncMock(return_value=1)
        key = WorktreeKey(
            repo="owner/repo", thread_type="pr", thread_id="42", workflow="review"
        )
        lock = WorktreeLock(redis, key)
        pending = await lock.get_pending_prompt()

        assert pending is not None
        assert pending.job_id == "job-123"
        assert pending.prompt == "hello"
        redis.delete.assert_called_once_with(key.pending_key)

    @pytest.mark.asyncio
    async def test_get_pending_prompt_not_found(self):
        redis = MagicMock()
        redis.get = AsyncMock(return_value=None)
        key = WorktreeKey(
            repo="owner/repo", thread_type="pr", thread_id="42", workflow="review"
        )
        lock = WorktreeLock(redis, key)
        pending = await lock.get_pending_prompt()
        assert pending is None

    @pytest.mark.asyncio
    async def test_get_pending_prompt_corrupt_clears(self):
        redis = MagicMock()
        redis.get = AsyncMock(return_value="not-json{{{")
        redis.delete = AsyncMock(return_value=1)
        key = WorktreeKey(
            repo="owner/repo", thread_type="pr", thread_id="42", workflow="review"
        )
        lock = WorktreeLock(redis, key)
        pending = await lock.get_pending_prompt()

        assert pending is None
        redis.delete.assert_called_once_with(key.pending_key)


class TestCancelSignal:
    @pytest.mark.asyncio
    async def test_send_cancel_signal_publishes(self):
        redis = MagicMock()
        redis.publish = AsyncMock(return_value=1)
        key = WorktreeKey(
            repo="owner/repo", thread_type="pr", thread_id="42", workflow="review"
        )
        lock = WorktreeLock(redis, key)
        await lock.send_cancel_signal()

        redis.publish.assert_called_once()
        call_args = redis.publish.call_args[0]
        assert call_args[0] == key.cancel_channel
        assert json.loads(call_args[1]) == {"action": "cancel"}


class TestWaitForRelease:
    @pytest.mark.asyncio
    async def test_wait_for_release_lock_gone(self):
        redis = MagicMock()
        redis.get = AsyncMock(return_value=None)
        key = WorktreeKey(
            repo="owner/repo", thread_type="pr", thread_id="42", workflow="review"
        )
        lock = WorktreeLock(redis, key)

        with patch("shared.worktree_lock.asyncio.sleep", new_callable=AsyncMock):
            result = await lock.wait_for_release(timeout=5)

        assert result is True

    @pytest.mark.asyncio
    async def test_wait_for_release_timeout(self):
        redis = MagicMock()
        redis.get = AsyncMock(return_value='{"job_id":"j1"}')
        key = WorktreeKey(
            repo="owner/repo", thread_type="pr", thread_id="42", workflow="review"
        )
        lock = WorktreeLock(redis, key)

        with patch("shared.worktree_lock.asyncio.sleep", new_callable=AsyncMock):
            result = await lock.wait_for_release(timeout=2)

        assert result is False
        assert redis.get.call_count > 1


class TestInterruptSdkProcess:
    @pytest.mark.asyncio
    async def test_sends_sigint(self):
        with patch("shared.worktree_lock.os.kill") as mock_kill:
            result = await interrupt_sdk_process(1234)

        assert result is True
        mock_kill.assert_called_once_with(1234, signal.SIGINT)

    @pytest.mark.asyncio
    async def test_none_pid_returns_false(self):
        result = await interrupt_sdk_process(None)
        assert result is False

    @pytest.mark.asyncio
    async def test_zero_pid_returns_false(self):
        result = await interrupt_sdk_process(0)
        assert result is False

    @pytest.mark.asyncio
    async def test_process_not_found_returns_false(self):
        with patch("shared.worktree_lock.os.kill", side_effect=ProcessLookupError):
            result = await interrupt_sdk_process(1234)
        assert result is False

    @pytest.mark.asyncio
    async def test_kill_error_returns_false(self):
        with patch("shared.worktree_lock.os.kill", side_effect=PermissionError):
            result = await interrupt_sdk_process(1234)
        assert result is False
