"""Tests for GitHub Actions tools."""

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

# Add plugin tools directory to path
sys.path.insert(
    0, str(Path(__file__).parent.parent.parent / "plugins" / "ci-failure-toolkit")
)

from tools.github_actions import (  # noqa: E402
    get_failed_steps,
    get_job_logs_raw,
    get_workflow_run_summary,
    search_job_logs,
)

# Add shared to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from shared.exceptions import AuthenticationError  # noqa: E402


@pytest.fixture
def mock_github_token():
    """Mock GitHub token in environment."""
    with patch.dict(os.environ, {"GITHUB_TOKEN": "test_token"}):
        yield "test_token"


@pytest.fixture
def mock_no_github_token():
    """Mock missing GitHub token."""
    with patch.dict(os.environ, {}, clear=True):
        yield


class TestGetWorkflowRunSummary:
    """Tests for get_workflow_run_summary function."""

    @pytest.mark.asyncio
    async def test_missing_github_token(self, mock_no_github_token):
        """Test that missing GITHUB_TOKEN raises AuthenticationError."""
        with pytest.raises(AuthenticationError, match="GITHUB_TOKEN not available"):
            await get_workflow_run_summary("owner", "repo", "123")

    @pytest.mark.asyncio
    async def test_successful_summary(self, mock_github_token):
        """Test successful workflow run summary retrieval."""
        mock_run_data = {
            "id": 123,
            "name": "CI",
            "status": "completed",
            "conclusion": "failure",
            "event": "push",
            "created_at": "2024-01-01T00:00:00Z",
            "updated_at": "2024-01-01T00:05:00Z",
            "html_url": "https://github.com/owner/repo/actions/runs/123",
        }

        mock_jobs_data = {
            "jobs": [
                {
                    "id": 456,
                    "name": "test",
                    "status": "completed",
                    "conclusion": "failure",
                    "started_at": "2024-01-01T00:00:00Z",
                    "completed_at": "2024-01-01T00:05:00Z",
                },
                {
                    "id": 789,
                    "name": "lint",
                    "status": "completed",
                    "conclusion": "success",
                    "started_at": "2024-01-01T00:00:00Z",
                    "completed_at": "2024-01-01T00:02:00Z",
                },
            ]
        }

        with patch("httpx.AsyncClient") as mock_client:
            mock_instance = AsyncMock()
            mock_client.return_value.__aenter__.return_value = mock_instance

            # Mock run response
            mock_run_resp = AsyncMock()
            mock_run_resp.json.return_value = mock_run_data
            mock_run_resp.raise_for_status = AsyncMock()

            # Mock jobs response
            mock_jobs_resp = AsyncMock()
            mock_jobs_resp.json.return_value = mock_jobs_data
            mock_jobs_resp.raise_for_status = AsyncMock()

            mock_instance.get.side_effect = [mock_run_resp, mock_jobs_resp]

            result = await get_workflow_run_summary("owner", "repo", "123")

            assert result["run_id"] == 123
            assert result["name"] == "CI"
            assert result["status"] == "completed"
            assert result["conclusion"] == "failure"
            assert len(result["jobs"]) == 2
            assert result["jobs"][0]["id"] == 456
            assert result["jobs"][1]["conclusion"] == "success"

    @pytest.mark.asyncio
    async def test_api_error(self, mock_github_token):
        """Test handling of GitHub API errors."""
        with patch("httpx.AsyncClient") as mock_client:
            mock_instance = AsyncMock()
            mock_client.return_value.__aenter__.return_value = mock_instance

            mock_resp = AsyncMock()
            mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
                "404 Not Found",
                request=AsyncMock(),
                response=AsyncMock(status_code=404),
            )
            mock_instance.get.return_value = mock_resp

            with pytest.raises(httpx.HTTPStatusError):
                await get_workflow_run_summary("owner", "repo", "999")


class TestGetJobLogsRaw:
    """Tests for get_job_logs_raw function."""

    @pytest.mark.asyncio
    async def test_missing_github_token(self, mock_no_github_token):
        """Test that missing GITHUB_TOKEN raises AuthenticationError."""
        with pytest.raises(AuthenticationError, match="GITHUB_TOKEN not available"):
            await get_job_logs_raw("owner", "repo", "123")

    @pytest.mark.asyncio
    async def test_paginated_logs(self, mock_github_token):
        """Test paginated log retrieval."""
        mock_logs = "\n".join(
            [f"2024-01-01T00:00:00.000Z Line {i}" for i in range(1000)]
        )

        with patch("httpx.AsyncClient") as mock_client:
            mock_instance = AsyncMock()
            mock_client.return_value.__aenter__.return_value = mock_instance

            mock_resp = AsyncMock()
            mock_resp.text = mock_logs
            mock_resp.raise_for_status = AsyncMock()
            mock_instance.get.return_value = mock_resp

            # Get first 500 lines
            result = await get_job_logs_raw(
                "owner", "repo", "123", start_line=0, num_lines=500
            )

            assert result["total_lines"] == 1000
            assert result["start_line"] == 0
            assert result["end_line"] == 500
            assert result["num_lines_returned"] == 500
            assert "Line 0" in result["lines"]
            assert "Line 499" in result["lines"]
            assert "Line 500" not in result["lines"]

            # Get next 500 lines
            result = await get_job_logs_raw(
                "owner", "repo", "123", start_line=500, num_lines=500
            )

            assert result["start_line"] == 500
            assert result["end_line"] == 1000
            assert "Line 500" in result["lines"]
            assert "Line 999" in result["lines"]

    @pytest.mark.asyncio
    async def test_timestamp_stripping(self, mock_github_token):
        """Test that GitHub Actions timestamps are stripped."""
        mock_logs = "2024-01-01T12:34:56.789Z This is a log line\n2024-01-01T12:34:57.000Z Another line"

        with patch("httpx.AsyncClient") as mock_client:
            mock_instance = AsyncMock()
            mock_client.return_value.__aenter__.return_value = mock_instance

            mock_resp = AsyncMock()
            mock_resp.text = mock_logs
            mock_resp.raise_for_status = AsyncMock()
            mock_instance.get.return_value = mock_resp

            result = await get_job_logs_raw("owner", "repo", "123")

            assert "2024-01-01T" not in result["lines"]
            assert "This is a log line" in result["lines"]
            assert "Another line" in result["lines"]


class TestSearchJobLogs:
    """Tests for search_job_logs function."""

    @pytest.mark.asyncio
    async def test_missing_github_token(self, mock_no_github_token):
        """Test that missing GITHUB_TOKEN raises AuthenticationError."""
        with pytest.raises(AuthenticationError, match="GITHUB_TOKEN not available"):
            await search_job_logs("owner", "repo", "123", "error")

    @pytest.mark.asyncio
    async def test_case_insensitive_search(self, mock_github_token):
        """Test case-insensitive pattern search."""
        mock_logs = "Line 1: No match\nLine 2: ERROR occurred\nLine 3: Another error\nLine 4: No match"

        with patch("httpx.AsyncClient") as mock_client:
            mock_instance = AsyncMock()
            mock_client.return_value.__aenter__.return_value = mock_instance

            mock_resp = AsyncMock()
            mock_resp.text = mock_logs
            mock_resp.raise_for_status = AsyncMock()
            mock_instance.get.return_value = mock_resp

            result = await search_job_logs(
                "owner", "repo", "123", "error", case_sensitive=False
            )

            assert result["total_matches"] == 2
            assert len(result["matches"]) == 2
            assert result["matches"][0]["line_number"] == 2
            assert "ERROR occurred" in result["matches"][0]["matched_line"]
            assert result["matches"][1]["line_number"] == 3

    @pytest.mark.asyncio
    async def test_case_sensitive_search(self, mock_github_token):
        """Test case-sensitive pattern search."""
        mock_logs = "Line 1: ERROR\nLine 2: error\nLine 3: Error"

        with patch("httpx.AsyncClient") as mock_client:
            mock_instance = AsyncMock()
            mock_client.return_value.__aenter__.return_value = mock_instance

            mock_resp = AsyncMock()
            mock_resp.text = mock_logs
            mock_resp.raise_for_status = AsyncMock()
            mock_instance.get.return_value = mock_resp

            result = await search_job_logs(
                "owner", "repo", "123", "ERROR", case_sensitive=True
            )

            assert result["total_matches"] == 1
            assert result["matches"][0]["matched_line"] == "Line 1: ERROR"

    @pytest.mark.asyncio
    async def test_context_lines(self, mock_github_token):
        """Test that context lines are included."""
        mock_logs = "\n".join([f"Line {i}" for i in range(1, 11)])

        with patch("httpx.AsyncClient") as mock_client:
            mock_instance = AsyncMock()
            mock_client.return_value.__aenter__.return_value = mock_instance

            mock_resp = AsyncMock()
            mock_resp.text = mock_logs
            mock_resp.raise_for_status = AsyncMock()
            mock_instance.get.return_value = mock_resp

            result = await search_job_logs(
                "owner", "repo", "123", "Line 5", context_lines=2
            )

            assert result["total_matches"] == 1
            context = result["matches"][0]["context"]
            assert "Line 3" in context  # 2 lines before
            assert "Line 4" in context
            assert "Line 5" in context  # matched line
            assert "Line 6" in context  # 2 lines after
            assert "Line 7" in context

    @pytest.mark.asyncio
    async def test_truncation(self, mock_github_token):
        """Test that results are truncated to 50 matches."""
        mock_logs = "\n".join([f"ERROR on line {i}" for i in range(100)])

        with patch("httpx.AsyncClient") as mock_client:
            mock_instance = AsyncMock()
            mock_client.return_value.__aenter__.return_value = mock_instance

            mock_resp = AsyncMock()
            mock_resp.text = mock_logs
            mock_resp.raise_for_status = AsyncMock()
            mock_instance.get.return_value = mock_resp

            result = await search_job_logs("owner", "repo", "123", "ERROR")

            assert result["total_matches"] == 100
            assert len(result["matches"]) == 50
            assert result["truncated"] is True


class TestGetFailedSteps:
    """Tests for get_failed_steps function."""

    @pytest.mark.asyncio
    async def test_missing_github_token(self, mock_no_github_token):
        """Test that missing GITHUB_TOKEN raises AuthenticationError."""
        with pytest.raises(AuthenticationError, match="GITHUB_TOKEN not available"):
            await get_failed_steps("owner", "repo", "123")

    @pytest.mark.asyncio
    async def test_extract_failed_steps(self, mock_github_token):
        """Test extraction of failed steps."""
        mock_job_data = {
            "id": 123,
            "name": "test",
            "conclusion": "failure",
            "steps": [
                {
                    "name": "Checkout",
                    "number": 1,
                    "status": "completed",
                    "conclusion": "success",
                    "started_at": "2024-01-01T00:00:00Z",
                    "completed_at": "2024-01-01T00:01:00Z",
                },
                {
                    "name": "Run tests",
                    "number": 2,
                    "status": "completed",
                    "conclusion": "failure",
                    "started_at": "2024-01-01T00:01:00Z",
                    "completed_at": "2024-01-01T00:05:00Z",
                },
                {
                    "name": "Upload coverage",
                    "number": 3,
                    "status": "completed",
                    "conclusion": "skipped",
                    "started_at": None,
                    "completed_at": None,
                },
            ],
        }

        mock_logs = "\n".join([f"Log line {i}" for i in range(200)])

        with patch("httpx.AsyncClient") as mock_client:
            mock_instance = AsyncMock()
            mock_client.return_value.__aenter__.return_value = mock_instance

            # Mock job response
            mock_job_resp = AsyncMock()
            mock_job_resp.json.return_value = mock_job_data
            mock_job_resp.raise_for_status = AsyncMock()

            # Mock logs response
            mock_logs_resp = AsyncMock()
            mock_logs_resp.text = mock_logs
            mock_logs_resp.raise_for_status = AsyncMock()

            mock_instance.get.side_effect = [mock_job_resp, mock_logs_resp]

            result = await get_failed_steps("owner", "repo", "123")

            assert result["job_id"] == 123
            assert result["job_name"] == "test"
            assert result["job_conclusion"] == "failure"
            assert result["failed_steps_count"] == 1
            assert len(result["failed_steps"]) == 1
            assert result["failed_steps"][0]["name"] == "Run tests"
            assert result["failed_steps"][0]["number"] == 2
            assert result["failed_steps"][0]["conclusion"] == "failure"

    @pytest.mark.asyncio
    async def test_log_truncation(self, mock_github_token):
        """Test that logs are truncated appropriately."""
        mock_job_data = {
            "id": 123,
            "name": "test",
            "conclusion": "failure",
            "steps": [
                {
                    "name": "Failed step",
                    "number": 1,
                    "status": "completed",
                    "conclusion": "failure",
                    "started_at": "2024-01-01T00:00:00Z",
                    "completed_at": "2024-01-01T00:01:00Z",
                }
            ],
        }

        # Create 500 lines of logs
        mock_logs = "\n".join([f"Log line {i}" for i in range(500)])

        with patch("httpx.AsyncClient") as mock_client:
            mock_instance = AsyncMock()
            mock_client.return_value.__aenter__.return_value = mock_instance

            mock_job_resp = AsyncMock()
            mock_job_resp.json.return_value = mock_job_data
            mock_job_resp.raise_for_status = AsyncMock()

            mock_logs_resp = AsyncMock()
            mock_logs_resp.text = mock_logs
            mock_logs_resp.raise_for_status = AsyncMock()

            mock_instance.get.side_effect = [mock_job_resp, mock_logs_resp]

            # Request only 100 lines
            result = await get_failed_steps(
                "owner", "repo", "123", log_lines_per_step=100
            )

            assert result["total_log_lines"] == 500
            assert result["log_excerpt_lines"] == 100
            assert "showing last 100 of 500 lines" in result["log_excerpt"]
            # Should contain last 100 lines
            assert "Log line 499" in result["log_excerpt"]
            assert "Log line 400" in result["log_excerpt"]
            assert "Log line 399" not in result["log_excerpt"]
