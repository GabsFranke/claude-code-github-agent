"""Tests for retrospector worker module.

These tests focus on the utility functions and core logic that can be
tested in isolation without requiring extensive mocking of external services.
"""

# Import the module under test
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


class TestValidateGitConfigValue:
    """Tests for _validate_git_config_value function."""

    def test_valid_value(self):
        """Test valid git config value."""
        from services.retrospector_worker.retrospector_worker import (
            _validate_git_config_value,
        )

        result = _validate_git_config_value("Claude Code Agent", "user.name")
        assert result == "Claude Code Agent"

    def test_newline_raises_error(self):
        """Test newline in value raises error."""
        from services.retrospector_worker.retrospector_worker import (
            _validate_git_config_value,
        )

        with pytest.raises(ValueError, match="newline"):
            _validate_git_config_value("bad\nvalue", "user.name")

    def test_carriage_return_raises_error(self):
        """Test carriage return in value raises error."""
        from services.retrospector_worker.retrospector_worker import (
            _validate_git_config_value,
        )

        with pytest.raises(ValueError, match="newline"):
            _validate_git_config_value("bad\rvalue", "user.email")

    def test_valid_email(self):
        """Test valid email value."""
        from services.retrospector_worker.retrospector_worker import (
            _validate_git_config_value,
        )

        result = _validate_git_config_value(
            "claude-code-agent[bot]@users.noreply.github.com", "user.email"
        )
        assert "claude-code-agent" in result


class TestProcessRetrospectorJob:
    """Tests for process_retrospector_job function."""

    @pytest.fixture
    def mock_redis_client(self):
        """Create a mock Redis client."""
        return AsyncMock()

    @pytest.fixture
    def valid_message(self, tmp_path):
        """Create a valid job message."""
        transcript = tmp_path / "test.jsonl"
        transcript.write_text(
            '{"type": "user", "message": {"role": "user", "content": "test"}}'
        )

        return {
            "repo": "test/repo",
            "transcript_path": str(transcript),
            "workflow_name": "test-workflow",
            "hook_event": "Stop",
            "session_meta": {
                "num_turns": 5,
                "duration_ms": 1000,
                "is_error": False,
            },
        }

    @pytest.mark.asyncio
    async def test_skips_if_transcript_not_found(self, mock_redis_client):
        """Test that job is skipped if transcript doesn't exist."""
        from services.retrospector_worker.retrospector_worker import (
            process_retrospector_job,
        )

        message = {
            "repo": "test/repo",
            "transcript_path": "/nonexistent/path.jsonl",
            "workflow_name": "test-workflow",
        }

        # Should return early without error
        await process_retrospector_job(message, mock_redis_client)
