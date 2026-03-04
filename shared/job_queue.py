"""Job queue for managing long-running agent execution jobs."""

import json
import logging
import uuid
from typing import Any

from .exceptions import QueueError

logger = logging.getLogger(__name__)


class JobQueue:
    """Redis-based job queue for agent execution tasks.

    This queue manages the lifecycle of agent jobs:
    1. Worker creates job and adds to pending queue
    2. Sandbox worker pulls job and executes
    3. Sandbox worker stores result
    4. Result poster retrieves result and posts to GitHub
    """

    def __init__(
        self,
        redis_url: str,
        password: str | None = None,
        job_ttl: int = 3600,
    ):
        """Initialize job queue.

        Args:
            redis_url: Redis connection URL
            password: Redis password (optional)
            job_ttl: Job data TTL in seconds (default: 1 hour)
        """
        self.redis_url = redis_url
        self.password = password
        self.job_ttl = job_ttl
        self.redis: Any = None

        # Redis keys
        self.pending_queue = "agent:jobs:pending"
        self.processing_set = "agent:jobs:processing"
        self.job_data_prefix = "agent:job:data:"
        self.job_result_prefix = "agent:job:result:"
        self.job_status_prefix = "agent:job:status:"

    async def _connect(self) -> None:
        """Connect to Redis if not already connected."""
        if self.redis is None:
            try:
                import redis.asyncio as redis

                self.redis = await redis.from_url(
                    self.redis_url,
                    decode_responses=True,
                    password=self.password,
                )
                logger.debug("Connected to Redis for job queue")
            except ImportError as e:
                raise QueueError("redis package is required for JobQueue") from e
            except OSError as e:
                raise QueueError(f"Failed to connect to Redis: {e}") from e

    async def create_job(self, job_data: dict[str, Any]) -> str:
        """Create a new job and add to pending queue.

        Args:
            job_data: Job data dictionary containing:
                - repo: Repository name
                - issue_number: Issue/PR number
                - prompt: Agent prompt
                - github_token: GitHub API token
                - user: User who triggered the job
                - auto_review: Whether this is an auto-review
                - auto_triage: Whether this is an auto-triage

        Returns:
            job_id: Unique job identifier
        """
        await self._connect()

        job_id = str(uuid.uuid4())
        logger.info(
            f"Creating job {job_id} for {job_data.get('repo')}#{job_data.get('issue_number')}"
        )

        try:
            # Store job data with TTL
            await self.redis.setex(
                f"{self.job_data_prefix}{job_id}",
                self.job_ttl,
                json.dumps(job_data),
            )

            # Set initial status
            await self.redis.setex(
                f"{self.job_status_prefix}{job_id}",
                self.job_ttl,
                "pending",
            )

            # Add to pending queue
            await self.redis.rpush(self.pending_queue, job_id)

            logger.info(f"Job {job_id} created and queued")
            return job_id

        except (TypeError, ValueError) as e:
            raise QueueError(f"Failed to serialize job data: {e}") from e
        except OSError as e:
            raise QueueError(f"Failed to create job in Redis: {e}") from e

    async def get_next_job(
        self, timeout: int = 30
    ) -> tuple[str, dict[str, Any]] | None:
        """Pull next job from pending queue (blocking).

        This atomically moves the job from pending to processing.

        Args:
            timeout: Blocking timeout in seconds

        Returns:
            Tuple of (job_id, job_data) or None if timeout
        """
        await self._connect()

        try:
            # Blocking pop from pending queue
            result = await self.redis.blpop(self.pending_queue, timeout=timeout)

            if not result:
                return None

            _, job_id = result

            # Get job data
            job_data_json = await self.redis.get(f"{self.job_data_prefix}{job_id}")

            if not job_data_json:
                logger.warning(f"Job {job_id} data not found (expired?)")
                return None

            job_data = json.loads(job_data_json)

            # Mark as processing
            await self.redis.sadd(self.processing_set, job_id)
            await self.redis.setex(
                f"{self.job_status_prefix}{job_id}",
                self.job_ttl,
                "processing",
            )

            logger.info(f"Job {job_id} pulled for processing")
            return job_id, job_data

        except json.JSONDecodeError as e:
            logger.error(f"Failed to decode job data: {e}", exc_info=True)
            return None
        except OSError as e:
            logger.error(f"Redis error getting next job: {e}", exc_info=True)
            return None

    async def complete_job(
        self, job_id: str, result: dict[str, Any], status: str = "success"
    ) -> None:
        """Mark job as complete and store result.

        Args:
            job_id: Job identifier
            result: Result data dictionary containing:
                - status: "success" or "error"
                - response: Agent response (if success)
                - error: Error message (if error)
                - repo: Repository name
                - issue_number: Issue/PR number
            status: Job status ("success" or "error")
        """
        await self._connect()

        try:
            # Store result with TTL
            await self.redis.setex(
                f"{self.job_result_prefix}{job_id}",
                self.job_ttl,
                json.dumps(result),
            )

            # Update status
            await self.redis.setex(
                f"{self.job_status_prefix}{job_id}",
                self.job_ttl,
                status,
            )

            # Remove from processing set
            await self.redis.srem(self.processing_set, job_id)

            logger.info(f"Job {job_id} completed with status: {status}")

        except (TypeError, ValueError) as e:
            raise QueueError(f"Failed to serialize result: {e}") from e
        except OSError as e:
            raise QueueError(f"Failed to complete job in Redis: {e}") from e

    async def get_job_status(self, job_id: str) -> str | None:
        """Get current job status.

        Args:
            job_id: Job identifier

        Returns:
            Status string: "pending", "processing", "success", "error", or None if not found
        """
        await self._connect()

        try:
            status: str | None = await self.redis.get(
                f"{self.job_status_prefix}{job_id}"
            )
            return status
        except OSError as e:
            logger.error(f"Failed to get job status: {e}", exc_info=True)
            return None

    async def get_job_result(self, job_id: str) -> dict[str, Any] | None:
        """Get job result if available.

        Args:
            job_id: Job identifier

        Returns:
            Result dictionary or None if not found
        """
        await self._connect()

        try:
            result_json: str | None = await self.redis.get(
                f"{self.job_result_prefix}{job_id}"
            )
            if not result_json:
                return None

            result: dict[str, Any] = json.loads(result_json)
            return result
        except json.JSONDecodeError as e:
            logger.error(f"Failed to decode job result: {e}", exc_info=True)
            return None
        except OSError as e:
            logger.error(f"Failed to get job result: {e}", exc_info=True)
            return None

    async def get_queue_depth(self) -> int:
        """Get number of pending jobs in queue.

        Returns:
            Number of pending jobs
        """
        await self._connect()

        try:
            depth: int = await self.redis.llen(self.pending_queue)
            return depth
        except OSError as e:
            logger.error(f"Failed to get queue depth: {e}", exc_info=True)
            return 0

    async def get_processing_count(self) -> int:
        """Get number of jobs currently being processed.

        Returns:
            Number of processing jobs
        """
        await self._connect()

        try:
            count: int = await self.redis.scard(self.processing_set)
            return count
        except OSError as e:
            logger.error(f"Failed to get processing count: {e}", exc_info=True)
            return 0

    async def close(self) -> None:
        """Close Redis connection."""
        if self.redis:
            await self.redis.aclose()
            self.redis = None
            logger.debug("Closed Redis connection for job queue")
