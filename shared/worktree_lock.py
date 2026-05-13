"""Worktree concurrency lock with interrupt-continue support.

Prevents multiple jobs from using the same worktree simultaneously and
supports interrupting a running job to continue with a new prompt.

Redis schema::

    lock:worktree:{owner--repo}:{thread_type}-{thread_id}:{workflow}
        = {"job_id": "...", "session_id": "...", "status": "running"}

    pending:{worktree_key}
        = {"job_id": "...", "prompt": "...", "timestamp": "..."}

    cancel:{worktree_key}  (pub/sub channel for interrupt signals)
"""

import asyncio
import json
import logging
import os
import signal
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from shared.constants import sanitize_repo_key

try:
    import redis.asyncio as aioredis

    RedisClient = aioredis.Redis
except ImportError:
    RedisClient = Any  # type: ignore[assignment, misc]

logger = logging.getLogger(__name__)

LOCK_PREFIX = "lock:worktree:"
PENDING_PREFIX = "pending:"
CANCEL_CHANNEL_PREFIX = "cancel:"
DEFAULT_LOCK_TTL = 600  # 10 minutes


@dataclass
class WorktreeKey:
    """Unique identifier for a worktree/session combination."""

    repo: str
    thread_type: Literal["pr", "issue", "discussion"]
    thread_id: str
    workflow: str

    def __str__(self) -> str:
        safe_repo = sanitize_repo_key(self.repo)
        return f"{safe_repo}:{self.thread_type}-{self.thread_id}:{self.workflow}"

    @property
    def lock_key(self) -> str:
        return f"{LOCK_PREFIX}{self}"

    @property
    def pending_key(self) -> str:
        return f"{PENDING_PREFIX}{self}"

    @property
    def cancel_channel(self) -> str:
        return f"{CANCEL_CHANNEL_PREFIX}{self}"


@dataclass
class LockInfo:
    """Information stored in the lock key."""

    job_id: str
    session_id: str | None = None
    status: Literal["running", "interrupted"] = "running"
    pid: int | None = None  # Process ID of the worker holding the lock


@dataclass
class PendingPrompt:
    """A pending prompt waiting to interrupt a running job."""

    job_id: str
    prompt: str
    timestamp: str


class WorktreeLock:
    """Manages worktree locks with interrupt-continue support.

    Usage::

        lock = WorktreeLock(redis, key)

        # Acquire lock before processing
        if await lock.acquire(job_id, timeout=30):
            try:
                # Subscribe to cancel signals
                async with lock.cancel_subscription(on_cancel):
                    # Run SDK
                    result = await run_sdk(...)
                    lock.set_session_id(session_id)
            finally:
                await lock.release()
        else:
            # Lock held by another job, pending prompt was set
            # Wait for lock to be released
            await lock.wait_for_release(timeout=300)
            if await lock.acquire(job_id, timeout=5):
                # Check for pending prompt
                pending = await lock.get_pending()
                if pending:
                    # Resume with pending prompt
                    ...
    """

    def __init__(self, redis: RedisClient, key: WorktreeKey):
        self.redis = redis
        self.key = key
        self._lock_acquired = False
        self._job_id: str | None = None
        self._cancel_event = asyncio.Event()

    async def acquire(
        self,
        job_id: str,
        timeout: int = 0,
        ttl: int = DEFAULT_LOCK_TTL,
    ) -> bool:
        """Try to acquire the worktree lock.

        Args:
            job_id: Job identifier
            timeout: Seconds to wait if lock is held (0 = don't wait)
            ttl: Lock TTL in seconds (auto-expires if worker crashes)

        Returns:
            True if lock acquired, False if lock held by another job.
        """
        lock_info = LockInfo(
            job_id=job_id,
            session_id=None,
            status="running",
            pid=os.getpid(),
        )
        lock_value = json.dumps(lock_info.__dict__)

        # Try to acquire lock (NX = only set if not exists)
        acquired = await self.redis.set(
            self.key.lock_key,
            lock_value,
            nx=True,
            ex=ttl,
        )

        if acquired:
            self._lock_acquired = True
            self._job_id = job_id
            logger.info(f"Acquired lock for {self.key}")
            return True

        if timeout > 0:
            # Wait for lock to be released
            logger.info(f"Lock held for {self.key}, waiting up to {timeout}s...")
            await self.wait_for_release(timeout=timeout)
            # Try again after wait
            acquired = await self.acquire(job_id, timeout=0, ttl=ttl)
            if not acquired:
                # Lock still held after waiting, set pending prompt
                logger.info(f"Lock still held for {self.key}, setting pending prompt")
                await self.set_pending_prompt(job_id, "")
            return acquired

        logger.info(f"Lock held for {self.key}, setting pending prompt")
        return False

    async def release(self) -> None:
        """Release the worktree lock."""
        if not self._lock_acquired:
            return

        lua_script = """
        local val = redis.call('GET', KEYS[1])
        if not val then return 0 end
        local data = cjson.decode(val)
        if data['job_id'] == ARGV[1] then
            redis.call('DEL', KEYS[1])
            return 1
        end
        return 0
        """
        if self._job_id is None:
            logger.warning(f"No job_id stored for lock {self.key}, skipping release")
            self._lock_acquired = False
            return
        result = await self.redis.eval(lua_script, 1, self.key.lock_key, self._job_id)  # type: ignore[misc]
        if not result:
            logger.warning(f"Lock for {self.key} was not owned by us, skipping release")
        self._lock_acquired = False
        logger.info(f"Released lock for {self.key}")

    async def _update_lock_field(self, field: str, value: str) -> bool:
        """Update a single field in the lock value while preserving TTL (atomic via Lua).

        Guards against TTL expiry between GET and SETEX: if the lock has
        already expired by the time we read its TTL, the operation is
        aborted rather than recreating a stale key.

        Args:
            field: JSON field name to update (e.g. ``session_id``, ``status``).
            value: New value for the field.

        Returns:
            True if the update succeeded, False if the key was missing or
            had already expired.
        """
        lua_script = """
        local val = redis.call('GET', KEYS[1])
        if not val then return 0 end
        local data = cjson.decode(val)
        data[ARGV[1]] = ARGV[2]
        local ttl = redis.call('TTL', KEYS[1])
        if ttl <= 0 then return 0 end
        local new_val = cjson.encode(data)
        redis.call('SETEX', KEYS[1], ttl, new_val)
        return 1
        """
        try:
            result = await self.redis.eval(  # type: ignore[misc]
                lua_script, 1, self.key.lock_key, field, value
            )
            if not result:
                logger.warning(
                    f"Lock key missing or expired when updating {field} for {self.key}"
                )
            return bool(result)
        except Exception as e:
            logger.warning(f"Failed to update {field} in lock: {e}")
            return False

    async def set_session_id(self, session_id: str) -> None:
        """Update the lock with the current session ID (atomic via Lua).

        Called after SDK starts and session ID is known.
        """
        if not self._lock_acquired:
            return

        await self._update_lock_field("session_id", session_id)

    async def set_interrupted(self) -> None:
        """Mark the lock as interrupted (atomic via Lua)."""
        if not self._lock_acquired:
            return

        await self._update_lock_field("status", "interrupted")

    async def get_lock_info(self) -> LockInfo | None:
        """Get current lock holder info."""
        lock_value = await self.redis.get(self.key.lock_key)
        if not lock_value:
            return None

        try:
            data = json.loads(lock_value)
            return LockInfo(**data)
        except Exception as e:
            logger.error(f"Corrupt lock data at {self.key.lock_key}: {e}")
            return None

    async def set_pending_prompt(self, job_id: str, prompt: str) -> None:
        """Set a pending prompt to interrupt the current job."""
        pending = PendingPrompt(
            job_id=job_id,
            prompt=prompt,
            timestamp=datetime.now(UTC).isoformat(),
        )
        # Store for 5 minutes (should be picked up quickly)
        await self.redis.setex(
            self.key.pending_key,
            300,
            json.dumps(pending.__dict__),
        )
        logger.info(f"Set pending prompt for {self.key}")

    async def get_pending_prompt(self) -> PendingPrompt | None:
        """Get and clear the pending prompt atomically."""
        lua_script = """
        local val = redis.call('GET', KEYS[1])
        if not val then return nil end
        redis.call('DEL', KEYS[1])
        return val
        """
        raw = await self.redis.eval(lua_script, 1, self.key.pending_key)  # type: ignore[misc]
        if not raw:
            return None
        try:
            data = json.loads(raw)
            return PendingPrompt(**data)
        except Exception as e:
            logger.error(f"Corrupt pending prompt at {self.key.pending_key}: {e}")
            return None

    async def send_cancel_signal(self) -> None:
        """Publish a cancel signal to the job holding the lock."""
        await self.redis.publish(
            self.key.cancel_channel, json.dumps({"action": "cancel"})
        )
        logger.info(f"Sent cancel signal for {self.key}")

    async def wait_for_release(self, timeout: int = 300) -> bool:
        """Wait for the lock to be released.

        Args:
            timeout: Maximum seconds to wait

        Returns:
            True if lock was released, False if timeout.
        """
        interval = 1.0
        elapsed = 0.0

        while elapsed < timeout:
            lock_value = await self.redis.get(self.key.lock_key)
            if not lock_value:
                return True

            await asyncio.sleep(interval)
            elapsed += interval

        return False

    def cancel_subscription(self, on_cancel: "CancelCallback"):
        """Context manager for subscribing to cancel signals.

        Usage::

            async with lock.cancel_subscription(handle_cancel):
                # Run SDK - handle_cancel will be called if cancel signal received
                ...

        Args:
            on_cancel: Async callback to invoke when cancel signal received

        Returns:
            Async context manager.
        """
        return _CancelSubscription(self, on_cancel)


CancelCallback = Any  # Async callable


class _CancelSubscription:
    """Async context manager for cancel subscription."""

    def __init__(self, lock: WorktreeLock, on_cancel: CancelCallback):
        self.lock = lock
        self.on_cancel = on_cancel
        self._pubsub: Any = None
        self._task: asyncio.Task | None = None

    async def __aenter__(self) -> "_CancelSubscription":
        """Start listening for cancel signals."""
        self._pubsub = self.lock.redis.pubsub()
        await self._pubsub.subscribe(self.lock.key.cancel_channel)

        pubsub = self._pubsub  # Capture for closure

        async def listen():
            try:
                async for message in pubsub.listen():
                    if message["type"] == "message":
                        try:
                            data = json.loads(message["data"])
                            if data.get("action") == "cancel":
                                logger.info(
                                    f"Cancel signal received for {self.lock.key}"
                                )
                                await self.on_cancel()
                        except Exception as e:
                            logger.warning(f"Error processing cancel message: {e}")
            except asyncio.CancelledError:
                pass

        self._task = asyncio.create_task(listen())
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Stop listening for cancel signals."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        if self._pubsub:
            await self._pubsub.unsubscribe(self.lock.key.cancel_channel)
            await self._pubsub.close()


async def interrupt_sdk_process(pid: int | None) -> bool:
    """Send SIGINT to the SDK process for graceful interruption.

    The SDK handles SIGINT by finishing the current turn and saving
    the session file, allowing clean continuation.

    SECURITY NOTE: This uses os.kill(pid, SIGINT). If the original process
    has exited and the PID has been recycled, this signal may go to an
    unrelated process. On Linux, consider validating via /proc/{pid}/cmdline.
    On Windows, this uses CTRL_C_EVENT which is process-group specific.

    Args:
        pid: Process ID of the worker running the SDK

    Returns:
        True if signal sent, False if pid unavailable.
    """
    if pid is None or pid <= 0:
        logger.warning("Cannot interrupt: no PID available")
        return False

    try:
        if sys.platform == "win32":
            # Windows: use CTRL_C_EVENT for the process, or SIGTERM as fallback
            os.kill(pid, signal.CTRL_C_EVENT)
        else:
            os.kill(pid, signal.SIGINT)
        logger.info(f"Sent interrupt signal to process {pid}")
        return True
    except OSError:
        logger.warning(f"Process {pid} not found")
        return False
    except Exception as e:
        logger.error(f"Failed to send interrupt signal to {pid}: {e}")
        return False
