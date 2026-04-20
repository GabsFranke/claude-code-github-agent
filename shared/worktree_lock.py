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
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

LOCK_PREFIX = "lock:worktree:"
PENDING_PREFIX = "pending:"
CANCEL_CHANNEL_PREFIX = "cancel:"
DEFAULT_LOCK_TTL = 600  # 10 minutes


@dataclass
class WorktreeKey:
    """Unique identifier for a worktree/session combination."""

    repo: str
    thread_type: str  # "pr", "issue", "discussion"
    thread_id: str
    workflow: str

    def __str__(self) -> str:
        safe_repo = self.repo.replace("/", "--")
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
    status: str = "running"  # "running" | "interrupted"
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

    def __init__(self, redis: Any, key: WorktreeKey):
        self.redis = redis
        self.key = key
        self._lock_acquired = False
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
            logger.info(f"Acquired lock for {self.key}")
            return True

        if timeout > 0:
            # Wait for lock to be released
            logger.info(f"Lock held for {self.key}, waiting up to {timeout}s...")
            await self.wait_for_release(timeout=timeout)
            # Try again after wait
            return await self.acquire(job_id, timeout=0, ttl=ttl)

        logger.info(f"Lock held for {self.key}, setting pending prompt")
        return False

    async def release(self) -> None:
        """Release the worktree lock."""
        if self._lock_acquired:
            await self.redis.delete(self.key.lock_key)
            self._lock_acquired = False
            logger.info(f"Released lock for {self.key}")

    async def set_session_id(self, session_id: str) -> None:
        """Update the lock with the current session ID.

        Called after SDK starts and session ID is known.
        """
        if not self._lock_acquired:
            return

        lock_value = await self.redis.get(self.key.lock_key)
        if not lock_value:
            return

        try:
            data = json.loads(lock_value)
            data["session_id"] = session_id
            ttl = await self.redis.ttl(self.key.lock_key)
            if ttl and ttl > 0:
                await self.redis.setex(self.key.lock_key, ttl, json.dumps(data))
            else:
                await self.redis.set(self.key.lock_key, json.dumps(data))
        except Exception as e:
            logger.warning(f"Failed to update session_id in lock: {e}")

    async def set_interrupted(self) -> None:
        """Mark the lock as interrupted (job will be superseded)."""
        if not self._lock_acquired:
            return

        lock_value = await self.redis.get(self.key.lock_key)
        if not lock_value:
            return

        try:
            data = json.loads(lock_value)
            data["status"] = "interrupted"
            ttl = await self.redis.ttl(self.key.lock_key)
            if ttl and ttl > 0:
                await self.redis.setex(self.key.lock_key, ttl, json.dumps(data))
            else:
                await self.redis.set(self.key.lock_key, json.dumps(data))
        except Exception as e:
            logger.warning(f"Failed to set interrupted status: {e}")

    async def get_lock_info(self) -> LockInfo | None:
        """Get current lock holder info."""
        lock_value = await self.redis.get(self.key.lock_key)
        if not lock_value:
            return None

        try:
            data = json.loads(lock_value)
            return LockInfo(**data)
        except Exception:
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
        """Get and clear the pending prompt."""
        raw = await self.redis.get(self.key.pending_key)
        if not raw:
            return None

        try:
            data = json.loads(raw)
            # Clear it
            await self.redis.delete(self.key.pending_key)
            return PendingPrompt(**data)
        except Exception:
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

    Args:
        pid: Process ID of the worker running the SDK

    Returns:
        True if signal sent, False if pid unavailable.
    """
    if pid is None or pid <= 0:
        logger.warning("Cannot interrupt: no PID available")
        return False

    try:
        os.kill(pid, signal.SIGINT)
        logger.info(f"Sent SIGINT to process {pid}")
        return True
    except ProcessLookupError:
        logger.warning(f"Process {pid} not found")
        return False
    except Exception as e:
        logger.error(f"Failed to send SIGINT to {pid}: {e}")
        return False
