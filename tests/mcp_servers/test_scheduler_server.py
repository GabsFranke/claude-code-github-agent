"""Unit tests for the Scheduler MCP server (mcp_servers/scheduler/server.py)."""

from unittest.mock import patch

import pytest

from mcp_servers.scheduler.server import handle_request


@pytest.fixture
def mock_env(monkeypatch):
    """Setup default environment variables for testing."""
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/test-repo")


class TestSchedulerMCPServer:
    """Test Scheduler MCP server tools and handler implementation."""

    @pytest.mark.asyncio
    async def test_initialize(self):
        """Test the initialize protocol method."""
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05"},
        }
        response = await handle_request(request)
        assert response["protocolVersion"] == "2024-11-05"
        assert "tools" in response["capabilities"]

    @pytest.mark.asyncio
    async def test_tools_list(self):
        """Test listing the schedule tools."""
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        response = await handle_request(request)
        assert "tools" in response
        assert len(response["tools"]) == 1
        assert response["tools"][0]["name"] == "schedule_one_shot_task"

    @pytest.mark.asyncio
    async def test_call_missing_repository(self, monkeypatch):
        """Rejects calls when GITHUB_REPOSITORY is missing from the environment."""
        monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "schedule_one_shot_task",
                "arguments": {
                    "workflow_name": "review-pr",
                    "delay_seconds": 60,
                },
            },
        }
        response = await handle_request(request)
        assert "error" in response
        assert "GITHUB_REPOSITORY" in response["error"]["message"]

    @pytest.mark.asyncio
    async def test_call_missing_trigger_arguments(self, mock_env):
        """Rejects calls when both run_at and delay_seconds are missing."""
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "schedule_one_shot_task",
                "arguments": {
                    "workflow_name": "review-pr",
                },
            },
        }
        response = await handle_request(request)
        assert "error" in response
        assert (
            "Either 'run_at' or 'delay_seconds' must be provided"
            in response["error"]["message"]
        )

    @pytest.mark.asyncio
    async def test_call_invalid_date_format(self, mock_env):
        """Rejects calls with malformed run_at date strings."""
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "schedule_one_shot_task",
                "arguments": {
                    "workflow_name": "review-pr",
                    "run_at": "not-a-date",
                },
            },
        }
        response = await handle_request(request)
        assert "error" in response
        assert "Invalid ISO-8601 date format" in response["error"]["message"]

    @pytest.mark.asyncio
    async def test_call_negative_delay(self, mock_env):
        """Rejects calls with negative delay_seconds."""
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "schedule_one_shot_task",
                "arguments": {
                    "workflow_name": "review-pr",
                    "delay_seconds": -10,
                },
            },
        }
        response = await handle_request(request)
        assert "error" in response
        assert "cannot be negative" in response["error"]["message"]

    @pytest.mark.asyncio
    @patch("mcp_servers.scheduler.server._call_scheduler_api")
    async def test_call_success_with_delay(self, mock_call_api, mock_env):
        """Schedules job successfully when delay_seconds is supplied."""
        mock_call_api.return_value = {
            "status": "success",
            "job_id": "test-job-123",
            "run_at": "2026-05-30T12:00:00Z",
        }
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "schedule_one_shot_task",
                "arguments": {
                    "workflow_name": "stale-pr-checker",
                    "delay_seconds": 3600,
                },
            },
        }
        response = await handle_request(request)
        assert "content" in response
        text = response["content"][0]["text"]
        assert "successfully scheduled" in text
        assert "test-job-123" in text
        assert "stale-pr-checker" in text

        # Verify api call payload contents
        mock_call_api.assert_called_once()
        payload = mock_call_api.call_args[0][0]
        assert payload["workflow_name"] == "stale-pr-checker"
        assert payload["repo"] == "owner/test-repo"
        assert payload["ref"] == "main"
