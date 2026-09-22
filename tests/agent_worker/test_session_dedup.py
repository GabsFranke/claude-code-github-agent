"""TDD tests for session-aware job deduplication (Task 4).

Tests the atomic SETNX-based dedup lock in request_processor.py that
prevents duplicate jobs when multiple requests arrive for the same
session within a short window.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Stateful mock Redis for SETNX-aware dedup testing
# ---------------------------------------------------------------------------


class FakeRedis:
    """Stateful fake Redis that tracks SETNX semantics for dedup testing.

    Supports: set(nx=True), get, hgetall, hset, delete, publish, pipeline.
    SETNX (set with nx=True) returns True on first write, None on conflict.
    """

    def __init__(self):
        self._data: dict[str, bytes | str] = {}
        self._hashes: dict[str, dict[str, str]] = {}

    def pipeline(self):
        return _FakePipeline(self)

    async def set(self, key, value, nx=False, ex=None):
        if nx:
            if key in self._data:
                return None
        self._data[key] = value
        return True

    async def get(self, key):
        return self._data.get(key)

    async def hgetall(self, key):
        return self._hashes.get(key, {})

    async def hset(self, key, mapping=None, **kwargs):
        if mapping:
            decoded = {}
            for k, v in mapping.items():
                dk = k.decode() if isinstance(k, bytes) else k
                dv = v.decode() if isinstance(v, bytes) else v
                decoded[dk] = dv
            self._hashes[key] = decoded
        else:
            self._hashes.setdefault(key, {}).update(kwargs)
        return 1

    async def delete(self, *keys):
        for k in keys:
            self._data.pop(k, None)
            self._hashes.pop(k, None)
        return len(keys)

    async def expire(self, key, seconds):
        return True

    async def publish(self, channel, message):
        return 1


class _FakePipeline:
    """Minimal pipeline that buffers commands and replays on execute()."""

    def __init__(self, redis: FakeRedis):
        self._redis = redis
        self._commands: list = []

    def hset(self, key, mapping=None, **kwargs):
        self._commands.append(("hset", key, mapping, kwargs))
        return self

    def expire(self, key, seconds):
        self._commands.append(("expire", key, seconds))
        return self

    def setex(self, key, seconds, value):
        self._commands.append(("setex", key, seconds, value))
        return self

    async def execute(self):
        for cmd in self._commands:
            name = cmd[0]
            if name == "hset":
                await self._redis.hset(cmd[1], mapping=cmd[2])
            elif name == "expire":
                await self._redis.expire(cmd[1], cmd[2])
            elif name == "setex":
                self._redis._data[cmd[1]] = cmd[3]  # simple set, ignore TTL
        self._commands.clear()
        return [True] * len(self._commands)


@pytest.fixture
def fake_redis():
    """Return a FakeRedis instance shared across the test."""
    return FakeRedis()


@pytest.fixture
def mock_job_queue(fake_redis):
    """Mock JobQueue with a FakeRedis client."""
    jq = AsyncMock()
    jq.redis = fake_redis
    jq.create_job = AsyncMock(return_value="job-test-123")
    jq.ensure_connected = AsyncMock()
    return jq


@pytest.fixture
def mock_token_manager():
    tm = AsyncMock()
    tm.get_token = AsyncMock(return_value="test-github-token")
    return tm


def _make_streaming_workflow_engine():
    """WorkflowEngine mock with streaming enabled and proper model_dump."""
    engine = MagicMock()
    engine.build_prompt = MagicMock(return_value=("Test prompt", "system context"))
    engine.workflows = {}
    engine.get_context_profile = MagicMock(return_value={})
    engine.get_conversation_config = MagicMock()

    conv_cfg = MagicMock()
    conv_cfg.persist = True
    conv_cfg.ttl_hours = 720
    conv_cfg.auto_continue = True
    conv_cfg.summary_fallback = False
    conv_cfg.model_dump.return_value = {
        "persist": True,
        "ttl_hours": 720,
        "auto_continue": True,
        "summary_fallback": False,
    }
    engine.get_conversation_config.return_value = conv_cfg

    wf_cfg = MagicMock()
    wf_cfg.streaming = MagicMock()
    wf_cfg.streaming.enabled = True
    engine.workflows = {"test-workflow": wf_cfg}

    return engine


# ---------------------------------------------------------------------------
# Helpers for seeding streaming session state into FakeRedis
# ---------------------------------------------------------------------------


def _seed_lookup_key(fake_redis: FakeRedis, repo, thread_type, tid, workflow, token):
    """Write the streaming lookup key -> token so find_session works."""
    from shared.constants import streaming_lookup_key

    lk = streaming_lookup_key(repo, tid, workflow, thread_type=thread_type)
    fake_redis._data[lk] = token.encode()


def _seed_session_hash(fake_redis: FakeRedis, token: str, fields: dict):
    """Write session hash data so get_session / find_active_session works."""
    from shared.constants import streaming_session_key

    sk = streaming_session_key(token)
    fake_redis._hashes[sk] = fields


# ---------------------------------------------------------------------------
# Test: Two rapid requests → exactly 1 job, second in inbox
# ---------------------------------------------------------------------------


class TestDedupTwoRapidRequests:

    @pytest.mark.asyncio
    async def test_two_rapid_requests_one_job(
        self, fake_redis, mock_job_queue, mock_token_manager
    ):
        """First request acquires dedup lock → creates job.
        Second request sees lock → injects instead of creating job."""

        with patch(
            "services.agent_worker.processors.request_processor.WorkflowEngine",
            return_value=_make_streaming_workflow_engine(),
        ):
            with patch("shared.get_queue") as mock_get_queue:
                mock_sync_queue = AsyncMock()
                mock_get_queue.return_value = mock_sync_queue
                with patch(
                    "services.agent_worker.processors.request_processor."
                    "_inject_into_running_session"
                ) as mock_inject:
                    mock_inject.return_value = None

                    from services.agent_worker.processors import (
                        RequestProcessor,
                    )

                    processor = RequestProcessor(
                        mock_token_manager, AsyncMock(), mock_job_queue
                    )
                    processor.context_loader.fetch_claude_md = AsyncMock(
                        return_value=""
                    )
                    processor.context_loader.fetch_memory_index = AsyncMock(
                        return_value=""
                    )

                    event_data = {
                        "event_type": "issues",
                        "action": "opened",
                        "installation_id": "12345",
                    }

                    with patch.dict(
                        "os.environ", {"SESSION_PROXY_URL": "http://proxy:8000"}
                    ):
                        # First request → lock acquired, job created
                        result1 = await processor._execute(
                            repo="owner/repo",
                            issue_number=1,
                            event_data=event_data,
                            user_query="/agent review this",
                            user="testuser",
                            ref="main",
                            workflow_name="test-workflow",
                        )

                        # Second request → lock exists → should inject
                        result2 = await processor._execute(
                            repo="owner/repo",
                            issue_number=1,
                            event_data=event_data,
                            user_query="/agent also check this",
                            user="testuser",
                            ref="main",
                            workflow_name="test-workflow",
                        )

                    assert (
                        result1 == "job-test-123"
                    ), f"First request should create job, got: {result1}"
                    assert (
                        result2 == "injected"
                    ), f"Second request should be injected, got: {result2}"
                    assert (
                        mock_job_queue.create_job.call_count == 1
                    ), f"Expected 1 job, got {mock_job_queue.create_job.call_count}"

    @pytest.mark.asyncio
    async def test_existing_running_session_injects(
        self, fake_redis, mock_job_queue, mock_token_manager
    ):
        """When dedup lock exists AND running session is found,
        message should be injected with cancel signal."""

        # Pre-seed the dedup lock so SETNX fails
        from shared.constants import session_dedup_key

        dk = session_dedup_key("owner/repo", "issue", "1", "test-workflow")
        fake_redis._data[dk] = "1"

        # Pre-seed a running session
        _seed_lookup_key(
            fake_redis, "owner/repo", "issue", 1, "test-workflow", "running-token-abc"
        )
        _seed_session_hash(
            fake_redis, "running-token-abc", {"status": "running", "run_count": "1"}
        )

        event_data = {
            "event_type": "issues",
            "action": "opened",
            "installation_id": "12345",
        }

        with patch(
            "services.agent_worker.processors.request_processor.WorkflowEngine",
            return_value=_make_streaming_workflow_engine(),
        ):
            with patch("shared.get_queue") as mock_get_queue:
                mock_sync_queue = AsyncMock()
                mock_get_queue.return_value = mock_sync_queue
                with patch(
                    "services.agent_worker.processors.request_processor."
                    "_inject_into_running_session"
                ) as mock_inject:
                    mock_inject.return_value = None

                    from services.agent_worker.processors import (
                        RequestProcessor,
                    )

                    processor = RequestProcessor(
                        mock_token_manager, AsyncMock(), mock_job_queue
                    )
                    processor.context_loader.fetch_claude_md = AsyncMock(
                        return_value=""
                    )
                    processor.context_loader.fetch_memory_index = AsyncMock(
                        return_value=""
                    )

                    with patch.dict(
                        "os.environ", {"SESSION_PROXY_URL": "http://proxy:8000"}
                    ):
                        result = await processor._execute(
                            repo="owner/repo",
                            issue_number=1,
                            event_data=event_data,
                            user_query="follow-up question",
                            user="testuser",
                            ref="main",
                            workflow_name="test-workflow",
                        )

                    assert result == "injected", f"Expected 'injected', got: {result}"
                    mock_inject.assert_called_once()
                    call_kwargs = mock_inject.call_args.kwargs
                    assert call_kwargs["token"] == "running-token-abc"
                    assert call_kwargs["user_query"] == "follow-up question"
                    mock_job_queue.create_job.assert_not_called()

    @pytest.mark.asyncio
    async def test_completed_session_creates_resume(
        self, fake_redis, mock_job_queue, mock_token_manager
    ):
        """When no dedup lock exists but a completed session is found,
        the session should be reused and a resume job created."""

        # Pre-seed a completed session (but NO dedup lock)
        _seed_lookup_key(
            fake_redis, "owner/repo", "issue", 1, "test-workflow", "completed-token-xyz"
        )
        _seed_session_hash(
            fake_redis, "completed-token-xyz", {"status": "completed", "run_count": "3"}
        )

        event_data = {
            "event_type": "issues",
            "action": "opened",
            "installation_id": "12345",
        }

        with patch(
            "services.agent_worker.processors.request_processor.WorkflowEngine",
            return_value=_make_streaming_workflow_engine(),
        ):
            with patch("shared.get_queue") as mock_get_queue:
                mock_sync_queue = AsyncMock()
                mock_get_queue.return_value = mock_sync_queue

                from services.agent_worker.processors import RequestProcessor

                processor = RequestProcessor(
                    mock_token_manager, AsyncMock(), mock_job_queue
                )
                processor.context_loader.fetch_claude_md = AsyncMock(return_value="")
                processor.context_loader.fetch_memory_index = AsyncMock(return_value="")

                with patch.dict(
                    "os.environ", {"SESSION_PROXY_URL": "http://proxy:8000"}
                ):
                    result = await processor._execute(
                        repo="owner/repo",
                        issue_number=1,
                        event_data=event_data,
                        user_query="continue working",
                        user="testuser",
                        ref="main",
                        workflow_name="test-workflow",
                    )

                assert result == "job-test-123", f"Expected job ID, got: {result}"
                mock_job_queue.create_job.assert_called_once()
                call_args = mock_job_queue.create_job.call_args[0][0]
                assert (
                    call_args["session_token"] is not None
                ), "Expected session_token to be set for resumed session"

    @pytest.mark.asyncio
    async def test_cross_session_both_jobs(
        self, fake_redis, mock_job_queue, mock_token_manager
    ):
        """Different repos should not conflict — both create independent jobs."""

        event_data = {
            "event_type": "issues",
            "action": "opened",
            "installation_id": "12345",
        }

        with patch(
            "services.agent_worker.processors.request_processor.WorkflowEngine",
            return_value=_make_streaming_workflow_engine(),
        ):
            with patch("shared.get_queue") as mock_get_queue:
                mock_sync_queue = AsyncMock()
                mock_get_queue.return_value = mock_sync_queue

                from services.agent_worker.processors import RequestProcessor

                processor = RequestProcessor(
                    mock_token_manager, AsyncMock(), mock_job_queue
                )
                processor.context_loader.fetch_claude_md = AsyncMock(return_value="")
                processor.context_loader.fetch_memory_index = AsyncMock(return_value="")

                with patch.dict(
                    "os.environ", {"SESSION_PROXY_URL": "http://proxy:8000"}
                ):
                    mock_job_queue.create_job = AsyncMock(return_value="job-A")
                    result_a = await processor._execute(
                        repo="owner/repo-a",
                        issue_number=1,
                        event_data=event_data,
                        user_query="task for repo A",
                        user="testuser",
                        ref="main",
                        workflow_name="test-workflow",
                    )

                    mock_job_queue.create_job = AsyncMock(return_value="job-B")
                    result_b = await processor._execute(
                        repo="owner/repo-b",
                        issue_number=1,
                        event_data=event_data,
                        user_query="task for repo B",
                        user="testuser",
                        ref="main",
                        workflow_name="test-workflow",
                    )

                assert result_a == "job-A", f"Repo A should create job, got: {result_a}"
                assert result_b == "job-B", f"Repo B should create job, got: {result_b}"

    @pytest.mark.asyncio
    async def test_lock_ttl_expires(
        self, fake_redis, mock_job_queue, mock_token_manager
    ):
        """When dedup lock exists but no session is found (stale lock),
        the lock should be cleared and a new job created."""

        # Pre-seed stale dedup lock (no corresponding session)
        from shared.constants import session_dedup_key

        dk = session_dedup_key("owner/repo", "issue", "1", "test-workflow")
        fake_redis._data[dk] = "1"

        event_data = {
            "event_type": "issues",
            "action": "opened",
            "installation_id": "12345",
        }

        with patch(
            "services.agent_worker.processors.request_processor.WorkflowEngine",
            return_value=_make_streaming_workflow_engine(),
        ):
            with patch("shared.get_queue") as mock_get_queue:
                mock_sync_queue = AsyncMock()
                mock_get_queue.return_value = mock_sync_queue

                from services.agent_worker.processors import RequestProcessor

                processor = RequestProcessor(
                    mock_token_manager, AsyncMock(), mock_job_queue
                )
                processor.context_loader.fetch_claude_md = AsyncMock(return_value="")
                processor.context_loader.fetch_memory_index = AsyncMock(return_value="")

                with patch.dict(
                    "os.environ", {"SESSION_PROXY_URL": "http://proxy:8000"}
                ):
                    result = await processor._execute(
                        repo="owner/repo",
                        issue_number=1,
                        event_data=event_data,
                        user_query="resume after stale lock",
                        user="testuser",
                        ref="main",
                        workflow_name="test-workflow",
                    )

                assert (
                    result == "job-test-123"
                ), f"Expected job after clearing stale lock, got: {result}"
                mock_job_queue.create_job.assert_called_once()


# ---------------------------------------------------------------------------
# Test: Dedup lock cleanup in processor.py
# ---------------------------------------------------------------------------


class TestDedupLockCleanup:

    @pytest.mark.asyncio
    async def test_cleanup_releases_dedup_lock(self, fake_redis):
        """Verify that processor._cleanup() deletes the dedup lock key."""

        from shared.constants import session_dedup_key

        expected_key = session_dedup_key("owner/repo", "issue", "1", "test-workflow")

        # Pre-populate the dedup key so we can verify it gets deleted
        fake_redis._data[expected_key] = "1"

        from services.sandbox_executor.processor import JobProcessor

        job_data = {
            "repo": "owner/repo",
            "issue_number": 1,
            "ref": "main",
            "thread_type": "issue",
            "thread_id": "1",
            "workflow_name": "test-workflow",
            "conversation_config": {"persist": True, "ttl_hours": 720},
            "github_token": "test-token",
            "prompt": "test",
            "user": "test",
            "session_token": "tok-123",
            "streaming_enabled": False,
            "session_mode": "new",
        }

        job_queue = AsyncMock()
        job_queue.redis = fake_redis

        processor = JobProcessor(job_queue, "job-123", job_data)
        processor.worktree_lock = None  # skip worktree cleanup
        processor.persist_session = True
        processor.workspace = "/tmp/test-workspace"
        processor.repo_dir = "/tmp/bare-repo"

        with patch(
            "services.sandbox_executor.processor.execute_git_command",
            new_callable=AsyncMock,
        ) as mock_git:
            mock_git.return_value = (0, "", "")
            await processor._cleanup()

        # Dedup key should be gone
        assert (
            expected_key not in fake_redis._data
        ), f"Dedup key {expected_key} was not cleaned up"


# ---------------------------------------------------------------------------
# Test: session_dedup_key builder
# ---------------------------------------------------------------------------


class TestSessionDedupKey:

    def test_dedup_key_format(self):
        from shared.constants import session_dedup_key

        key = session_dedup_key("owner/repo", "issue", "42", "review-pr")
        assert "agent:session:lock:" in key
        assert "owner--repo" in key
        assert "issue" in key
        assert "42" in key
        assert "review-pr" in key

    def test_dedup_key_different_workflows(self):
        from shared.constants import session_dedup_key

        key1 = session_dedup_key("o/r", "issue", "1", "review-pr")
        key2 = session_dedup_key("o/r", "issue", "1", "triage")
        assert key1 != key2

    def test_dedup_key_different_repos(self):
        from shared.constants import session_dedup_key

        key1 = session_dedup_key("o/r1", "issue", "1", "test")
        key2 = session_dedup_key("o/r2", "issue", "1", "test")
        assert key1 != key2
