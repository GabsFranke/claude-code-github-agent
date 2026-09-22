"""Tests for the scheduler service's HTTP surface.

Covers the two ways the API could quietly misbehave: reporting a dead
scheduler as healthy, and accepting job creation from anything on the
compose network.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from services.scheduler import main as scheduler_main


@pytest.fixture(autouse=True)
def _clear_installation_repo_cache():
    """Reset the admission-check cache around each test.

    ``scheduler_main.workflow_scheduler`` is a module-level singleton, so a
    repo list cached by one test would otherwise decide the next one.
    """
    sched = scheduler_main.workflow_scheduler
    sched._installation_repos_cache = None
    sched._installation_repos_cached_at = 0.0
    yield
    sched._installation_repos_cache = None
    sched._installation_repos_cached_at = 0.0


@pytest.fixture
def client():
    """A TestClient that does not run the real lifespan (no scheduler start)."""
    return TestClient(scheduler_main.app)


class TestHealthStatusCode:
    """``/health`` must fail the compose healthcheck when it says unhealthy.

    The compose test is ``urllib.request.urlopen(...)``, which only raises on
    a non-2xx status. Returning a plain dict meant the container reported
    healthy while the scheduler was dead and firing nothing, and
    ``restart: unless-stopped`` never triggered because the process was alive.
    """

    def test_running_scheduler_returns_200(self, client):
        with patch.object(scheduler_main.workflow_scheduler, "_running", True):
            with patch.object(
                scheduler_main.workflow_scheduler, "scheduler", MagicMock()
            ) as sched:
                sched.get_jobs.return_value = []
                response = client.get("/health")

        assert response.status_code == 200
        assert response.json()["status"] == "healthy"

    def test_stopped_scheduler_returns_503(self, client):
        with patch.object(scheduler_main.workflow_scheduler, "_running", False):
            response = client.get("/health")

        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "unhealthy"
        assert body["scheduler_running"] is False


class TestOneShotAuth:
    """``POST /schedule/one-shot`` must not be open to the compose network.

    Sandbox agents reach this endpoint through the scheduler MCP server while
    running on attacker-authored issue and PR text, so an injected
    "schedule workflow X on repo Y" instruction would otherwise become a real
    job on a repo the commenter has no access to, stamped with the App's own
    installation id.
    """

    @staticmethod
    def _payload(**overrides):
        body = {
            "workflow_name": "review-pr",
            "repo": "owner/repo",
            "run_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        }
        body.update(overrides)
        return body

    def test_missing_token_is_rejected(self, client):
        with patch.object(scheduler_main.config, "scheduler_internal_token", "s3cret"):
            response = client.post("/schedule/one-shot", json=self._payload())

        assert response.status_code == 401

    def test_wrong_token_is_rejected(self, client):
        with patch.object(scheduler_main.config, "scheduler_internal_token", "s3cret"):
            response = client.post(
                "/schedule/one-shot",
                json=self._payload(),
                headers={"X-Internal-Token": "guess"},
            )

        assert response.status_code == 401

    def test_unconfigured_secret_fails_closed(self, client):
        """An unset secret must deny, not leave the endpoint open."""
        with patch.object(scheduler_main.config, "scheduler_internal_token", ""):
            response = client.post(
                "/schedule/one-shot",
                json=self._payload(),
                headers={"X-Internal-Token": ""},
            )

        assert response.status_code == 503

    def test_unknown_workflow_is_rejected(self, client):
        with (
            patch.object(scheduler_main.config, "scheduler_internal_token", "s3cret"),
            patch.object(
                scheduler_main.workflow_scheduler.workflow_engine,
                "workflows",
                {"review-pr": object()},
            ),
        ):
            response = client.post(
                "/schedule/one-shot",
                json=self._payload(workflow_name="not-a-workflow"),
                headers={"X-Internal-Token": "s3cret"},
            )

        assert response.status_code == 400

    def test_repo_outside_installation_is_rejected(self, client):
        with (
            patch.object(scheduler_main.config, "scheduler_internal_token", "s3cret"),
            patch.object(
                scheduler_main.workflow_scheduler.workflow_engine,
                "workflows",
                {"review-pr": object()},
            ),
            patch.object(
                scheduler_main.workflow_scheduler,
                "installation_repositories",
                AsyncMock(return_value=["someone/else"]),
            ),
        ):
            response = client.post(
                "/schedule/one-shot",
                json=self._payload(),
                headers={"X-Internal-Token": "s3cret"},
            )

        assert response.status_code == 403

    def test_unresolvable_installation_is_rejected(self, client):
        """If we cannot prove the repo is in scope, we do not enqueue."""
        with (
            patch.object(scheduler_main.config, "scheduler_internal_token", "s3cret"),
            patch.object(
                scheduler_main.workflow_scheduler.workflow_engine,
                "workflows",
                {"review-pr": object()},
            ),
            patch.object(
                scheduler_main.workflow_scheduler,
                "installation_repositories",
                AsyncMock(return_value=[]),
            ),
        ):
            response = client.post(
                "/schedule/one-shot",
                json=self._payload(),
                headers={"X-Internal-Token": "s3cret"},
            )

        assert response.status_code == 503

    def test_valid_request_is_scheduled(self, client):
        with (
            patch.object(scheduler_main.config, "scheduler_internal_token", "s3cret"),
            patch.object(
                scheduler_main.workflow_scheduler.workflow_engine,
                "workflows",
                {"review-pr": object()},
            ),
            patch.object(
                scheduler_main.workflow_scheduler,
                "installation_repositories",
                AsyncMock(return_value=["owner/repo"]),
            ),
            patch.object(
                scheduler_main.workflow_scheduler,
                "schedule_one_shot",
                AsyncMock(return_value="job-1"),
            ),
        ):
            response = client.post(
                "/schedule/one-shot",
                json=self._payload(),
                headers={"X-Internal-Token": "s3cret"},
            )

        assert response.status_code == 200
        assert response.json()["job_id"] == "job-1"
