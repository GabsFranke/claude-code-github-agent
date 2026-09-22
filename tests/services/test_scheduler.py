"""Unit tests for the Workflow Scheduler service."""

import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.scheduler.config import get_scheduler_config
from services.scheduler.scheduler import (
    INSTALLATION_REPOS_TTL,
    MISFIRE_GRACE_SECONDS,
    TRIGGER_DEDUP_TTL_SECONDS,
    WorkflowScheduler,
)


class TestSchedulerConfig:
    """Test scheduler configuration loading."""

    def test_default_config(self):
        config = get_scheduler_config()
        assert config.port in (8082, 8000)
        assert config.log_level == "INFO"
        assert config.github.github_app_id == "123456"
        assert config.github.github_installation_id == "789012"
        assert config.queue.redis_url == "redis://localhost:6379"


class TestWorkflowScheduler:
    """Test core WorkflowScheduler class and capabilities."""

    @pytest.fixture
    def mock_scheduler_deps(self):
        """Mock external dependencies for WorkflowScheduler."""
        with (
            patch("services.scheduler.scheduler.redis") as mock_redis,
            patch("services.scheduler.scheduler.get_queue") as mock_get_queue,
            patch("services.scheduler.scheduler.GitHubAuthService") as mock_auth,
            patch("services.scheduler.scheduler.WorkflowEngine") as mock_engine_cls,
        ):
            # Mock Redis client
            mock_redis_client = AsyncMock()
            mock_redis_client.ping = AsyncMock(return_value=True)
            mock_redis_client.set = AsyncMock(return_value=True)
            mock_redis.from_url.return_value = mock_redis_client

            # Mock Queue
            mock_queue = AsyncMock()
            mock_get_queue.return_value = mock_queue

            # Mock WorkflowEngine
            mock_engine = MagicMock()
            mock_engine_cls.return_value = mock_engine

            yield {
                "redis": mock_redis,
                "redis_client": mock_redis_client,
                "queue": mock_queue,
                "engine": mock_engine,
                "auth": mock_auth,
            }

    @pytest.mark.asyncio
    async def test_scheduler_start_stop(self, mock_scheduler_deps):
        """Test starting and stopping the scheduler gracefully."""
        ws = WorkflowScheduler()

        # Mock load_schedules to prevent real yaml loading
        ws.load_schedules = AsyncMock()

        await ws.start()
        assert ws._running is True
        mock_scheduler_deps["redis"].from_url.assert_called_once()
        mock_scheduler_deps["redis_client"].ping.assert_called_once()
        ws.load_schedules.assert_called_once()

        await ws.stop()
        assert ws._running is False
        mock_scheduler_deps["redis_client"].close.assert_called_once()

    @pytest.mark.asyncio
    async def test_load_schedules(self, mock_scheduler_deps):
        """Test loading schedules from the workflow engine."""
        ws = WorkflowScheduler()

        # Create mock workflow trigger configurations
        mock_wf_1 = MagicMock()
        mock_wf_1.triggers.schedule.cron = "0 9 * * 1-5"
        mock_wf_1.triggers.schedule.timezone = "UTC"
        mock_wf_1.triggers.schedule.enabled = True

        mock_scheduler_deps["engine"].get_scheduled_workflows.return_value = {
            "stale-pr-checker": mock_wf_1
        }

        # Mock scheduler add_job method
        ws.scheduler = MagicMock()

        await ws.load_schedules()

        mock_scheduler_deps["engine"].get_scheduled_workflows.assert_called_once()
        ws.scheduler.add_job.assert_called_once()
        # Verify job was registered with correct function and trigger details
        args = ws.scheduler.add_job.call_args[1]
        assert args["id"] == "stale-pr-checker"
        assert args["args"] == ["stale-pr-checker"]

    @pytest.mark.asyncio
    async def test_resolve_repositories_explicit(self, mock_scheduler_deps):
        """Test resolving explicit repositories config."""
        ws = WorkflowScheduler()
        repos = await ws._resolve_repositories(["owner/repo1", "owner/repo2"])
        assert repos == ["owner/repo1", "owner/repo2"]

    @pytest.mark.asyncio
    async def test_resolve_repositories_dynamic(self, mock_scheduler_deps):
        """Test dynamically resolving repositories using GitHubAuthService."""
        ws = WorkflowScheduler()

        mock_auth_instance = AsyncMock()
        mock_auth_instance.get_installation_repositories = AsyncMock(
            return_value=["owner/repo-a", "owner/repo-b"]
        )
        mock_auth_instance.__aenter__ = AsyncMock(return_value=mock_auth_instance)
        mock_auth_instance.__aexit__ = AsyncMock()
        mock_scheduler_deps["auth"].return_value = mock_auth_instance

        # Test wildcard resolution
        repos = await ws._resolve_repositories(["*"])
        assert repos == ["owner/repo-a", "owner/repo-b"]
        mock_auth_instance.get_installation_repositories.assert_called_once()

    @pytest.mark.asyncio
    async def test_trigger_workflow_publishes_jobs(self, mock_scheduler_deps):
        """Test triggering a workflow acquires locks and enqueues jobs to Redis."""
        ws = WorkflowScheduler()
        ws.redis_client = mock_scheduler_deps["redis_client"]
        ws._running = True

        # Mock workflow configs
        mock_wf = MagicMock()
        mock_wf.triggers.schedule.repos = ["owner/repo-test"]
        mock_wf.triggers.schedule.enabled = True
        ws.workflow_engine.workflows = {"stale-pr-checker": mock_wf}

        # Mock dynamic repo resolution to return a list
        ws._resolve_repositories = AsyncMock(return_value=["owner/repo-test"])

        await ws.trigger_workflow("stale-pr-checker")

        # 1. Distributed lock set in Redis
        assert ws.redis_client.set.call_count == 2
        lock_call = ws.redis_client.set.call_args_list[0]
        lock_key = lock_call[0][0]
        assert "scheduler:lock:stale-pr-checker:owner/repo-test:" in lock_key

        # 2. Redis status and history metrics recorded
        ws.redis_client.lpush.assert_called_once()
        ws.redis_client.ltrim.assert_called_once()

        # 3. Queue publication matching webhook payload format
        assert ws.queue.publish.called
        # Check at least one publish call contains the expected payload
        job_payload = ws.queue.publish.call_args[0][0]
        assert job_payload["repository"] == "owner/repo-test"
        assert job_payload["issue_number"] is None
        assert job_payload["user"] == "scheduler"
        assert job_payload["ref"] == "main"
        assert job_payload["workflow_name"] == "stale-pr-checker"
        assert job_payload["event_data"]["event_type"] == "schedule"
        assert job_payload["event_data"]["trigger_type"] == "schedule"
        assert job_payload["event_data"]["installation_id"] == "789012"


class TestSchedulerJobDefaults:
    """APScheduler's stock defaults make scheduling quietly unreliable."""

    @pytest.fixture
    def mock_scheduler_deps(self):
        with (
            patch("services.scheduler.scheduler.redis"),
            patch("services.scheduler.scheduler.get_queue"),
            patch("services.scheduler.scheduler.GitHubAuthService"),
            patch("services.scheduler.scheduler.WorkflowEngine"),
        ):
            yield

    def test_misfire_grace_time_is_not_the_one_second_default(
        self, mock_scheduler_deps
    ):
        """A tick landing while the loop is busy must not be silently dropped.

        BaseScheduler ships misfire_grace_time=1, so any cron firing while the
        loop is busy for over a second is skipped with only a library log line.
        trigger_workflow with repos ["*"] does a paginated installation walk
        plus a publish per repo, which routinely exceeds that — meaning a daily
        cron could simply not run that day.
        """
        ws = WorkflowScheduler()
        defaults = ws.scheduler._job_defaults

        assert defaults["misfire_grace_time"] == MISFIRE_GRACE_SECONDS
        assert defaults["misfire_grace_time"] > 1

    def test_misfire_grace_matches_the_dedup_lock_ttl(self, mock_scheduler_deps):
        """A recovered misfire must still be inside its dedup lock.

        If the grace window outlived the lock, a late tick would re-fire a
        workflow the dedup lock was meant to suppress.
        """
        assert MISFIRE_GRACE_SECONDS == TRIGGER_DEDUP_TTL_SECONDS

    def test_coalesce_and_max_instances_are_pinned(self, mock_scheduler_deps):
        ws = WorkflowScheduler()
        defaults = ws.scheduler._job_defaults

        assert defaults["coalesce"] is True
        assert defaults["max_instances"] == 1

    def test_timezone_is_utc(self, mock_scheduler_deps):
        """Cron expressions must not drift with the container's local zone."""
        ws = WorkflowScheduler()

        assert ws.scheduler.timezone == datetime.UTC


class TestInstallationRepoCache:
    """The admission check must not spend App API quota per request.

    ``_resolve_repositories(["*"])`` does a paginated GitHub App installation
    walk. The cron path calls it once per tick, but the one-shot endpoint
    would call it on every request, adding a remote round trip to each
    request's latency and drawing down the shared installation rate limit.
    """

    @pytest.fixture
    def scheduler(self):
        with (
            patch("services.scheduler.scheduler.redis"),
            patch("services.scheduler.scheduler.get_queue"),
            patch("services.scheduler.scheduler.GitHubAuthService"),
            patch("services.scheduler.scheduler.WorkflowEngine"),
        ):
            yield WorkflowScheduler()

    @pytest.mark.asyncio
    async def test_second_call_does_not_hit_github(self, scheduler):
        resolve = AsyncMock(return_value=["owner/repo"])
        with patch.object(scheduler, "_resolve_repositories", resolve):
            first = await scheduler.installation_repositories()
            second = await scheduler.installation_repositories()

        assert first == second == ["owner/repo"]
        assert resolve.await_count == 1

    @pytest.mark.asyncio
    async def test_failure_is_not_cached(self, scheduler):
        """`[]` means "cannot prove scope" and callers deny on it.

        Caching that would keep denying for the whole TTL after a transient
        GitHub blip had passed.
        """
        resolve = AsyncMock(side_effect=[[], ["owner/repo"]])
        with patch.object(scheduler, "_resolve_repositories", resolve):
            assert await scheduler.installation_repositories() == []
            assert await scheduler.installation_repositories() == ["owner/repo"]

        assert resolve.await_count == 2

    @pytest.mark.asyncio
    async def test_cache_expires(self, scheduler):
        resolve = AsyncMock(return_value=["owner/repo"])
        with patch.object(scheduler, "_resolve_repositories", resolve):
            await scheduler.installation_repositories()
            scheduler._installation_repos_cached_at -= INSTALLATION_REPOS_TTL + 1
            await scheduler.installation_repositories()

        assert resolve.await_count == 2
