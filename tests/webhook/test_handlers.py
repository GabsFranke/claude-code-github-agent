"""Unit tests for webhook handlers."""

from unittest.mock import AsyncMock

import pytest

from services.webhook.handlers.comment_handler import handle_comment_created
from services.webhook.handlers.issue_handler import handle_issue_opened
from services.webhook.handlers.pr_handler import (
    handle_pr_opened,
    handle_pr_other_action,
)


class TestCommentHandler:
    """Test comment event handler."""

    @pytest.mark.asyncio
    async def test_handle_comment_with_agent_command(self):
        """Test handling comment with /agent command."""
        mock_queue = AsyncMock()
        mock_sync_queue = AsyncMock()
        data = {
            "comment": {
                "body": "/agent review this code",
                "user": {"login": "testuser"},
            },
            "issue": {"number": 123},
            "repository": {"full_name": "owner/repo"},
        }

        result = await handle_comment_created(data, mock_queue, mock_sync_queue)

        assert result == {
            "status": "accepted",
            "message": "Agent is processing your request",
        }
        mock_queue.publish.assert_called_once_with(
            {
                "repository": "owner/repo",
                "issue_number": 123,
                "command": "review this code",
                "user": "testuser",
                "ref": "main",
            }
        )
        mock_sync_queue.publish.assert_called_once()

    @pytest.mark.asyncio
    async def test_handle_comment_without_command(self):
        """Test handling comment without /agent command."""
        mock_queue = AsyncMock()
        mock_sync_queue = AsyncMock()
        data = {
            "comment": {
                "body": "Just a regular comment",
                "user": {"login": "testuser"},
            },
            "issue": {"number": 123},
            "repository": {"full_name": "owner/repo"},
        }

        result = await handle_comment_created(data, mock_queue, mock_sync_queue)

        assert result is None
        mock_queue.publish.assert_not_called()
        mock_sync_queue.publish.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_comment_with_registered_command(self):
        """Test handling comment with registered command like /review."""
        mock_queue = AsyncMock()
        mock_sync_queue = AsyncMock()
        data = {
            "comment": {"body": "/review", "user": {"login": "testuser"}},
            "issue": {"number": 456},
            "repository": {"full_name": "owner/repo"},
        }

        result = await handle_comment_created(data, mock_queue, mock_sync_queue)

        assert result == {
            "status": "accepted",
            "message": "Agent is processing your request",
        }
        mock_queue.publish.assert_called_once_with(
            {
                "repository": "owner/repo",
                "issue_number": 456,
                "command": "review",
                "user": "testuser",
                "ref": "main",
            }
        )
        mock_sync_queue.publish.assert_called_once()


class TestIssueHandler:
    """Test issue event handler."""

    @pytest.mark.asyncio
    async def test_handle_issue_opened_with_agent_command(self):
        """Test handling issue opened with /agent command in body."""
        mock_queue = AsyncMock()
        mock_sync_queue = AsyncMock()
        data = {
            "issue": {
                "number": 789,
                "title": "Bug report",
                "body": "/agent investigate this bug",
                "user": {"login": "testuser"},
            },
            "repository": {"full_name": "owner/repo"},
        }

        result = await handle_issue_opened(data, mock_queue, mock_sync_queue)

        assert result == {
            "status": "accepted",
            "message": "Agent is processing your request",
        }
        mock_queue.publish.assert_called_once_with(
            {
                "repository": "owner/repo",
                "issue_number": 789,
                "command": "investigate this bug",
                "user": "testuser",
            }
        )
        mock_sync_queue.publish.assert_called_once()

    @pytest.mark.asyncio
    async def test_handle_issue_opened_without_command_auto_triage(self):
        """Test handling issue opened without command triggers auto-triage."""
        mock_queue = AsyncMock()
        mock_sync_queue = AsyncMock()
        data = {
            "issue": {
                "number": 101,
                "title": "Feature request",
                "body": "Please add this feature",
                "user": {"login": "testuser"},
            },
            "repository": {"full_name": "owner/repo"},
        }

        result = await handle_issue_opened(data, mock_queue, mock_sync_queue)

        assert result == {
            "status": "accepted",
            "message": "Agent is processing your request",
        }
        mock_queue.publish.assert_called_once()
        call_args = mock_queue.publish.call_args[0][0]
        assert call_args["repository"] == "owner/repo"
        assert call_args["issue_number"] == 101
        assert call_args["command"] == "Triage this issue: Feature request"
        assert call_args["user"] == "testuser"
        assert call_args["auto_triage"] is True
        mock_sync_queue.publish.assert_called_once()

    @pytest.mark.asyncio
    async def test_handle_issue_opened_with_none_body(self):
        """Test handling issue opened with None body triggers auto-triage."""
        mock_queue = AsyncMock()
        mock_sync_queue = AsyncMock()
        data = {
            "issue": {
                "number": 202,
                "title": "Empty issue",
                "body": None,
                "user": {"login": "testuser"},
            },
            "repository": {"full_name": "owner/repo"},
        }

        result = await handle_issue_opened(data, mock_queue, mock_sync_queue)

        assert result == {
            "status": "accepted",
            "message": "Agent is processing your request",
        }
        mock_queue.publish.assert_called_once()
        call_args = mock_queue.publish.call_args[0][0]
        assert call_args["auto_triage"] is True
        mock_sync_queue.publish.assert_called_once()


class TestPRHandler:
    """Test pull request event handler."""

    @pytest.mark.asyncio
    async def test_handle_pr_opened_auto_review(self):
        """Test handling PR opened triggers auto-review."""
        mock_queue = AsyncMock()
        mock_sync_queue = AsyncMock()
        data = {
            "pull_request": {
                "number": 42,
                "title": "Add new feature",
                "user": {"login": "contributor"},
            },
            "repository": {"full_name": "owner/repo"},
        }

        result = await handle_pr_opened(data, mock_queue, mock_sync_queue)

        assert result == {"status": "accepted", "message": "Agent will review this PR"}
        mock_queue.publish.assert_called_once_with(
            {
                "repository": "owner/repo",
                "issue_number": 42,
                "command": "Review this pull request: Add new feature",
                "user": "contributor",
                "auto_review": True,
            }
        )
        mock_sync_queue.publish.assert_called_once()

    def test_handle_pr_other_action_ignored(self):
        """Test handling other PR actions are ignored."""
        result = handle_pr_other_action("synchronize", 99)

        assert result == {
            "status": "ignored",
            "message": "PR action 'synchronize' not handled",
        }

    def test_handle_pr_closed_action_ignored(self):
        """Test handling PR closed action is ignored."""
        result = handle_pr_other_action("closed", 100)

        assert result == {
            "status": "ignored",
            "message": "PR action 'closed' not handled",
        }
