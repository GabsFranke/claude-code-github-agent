"""Tests for shared/worktree_manager.py — deterministic worktree paths and orphan detection."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.worktree_manager import (
    WORKTREE_BASE,
    detect_orphan_worktrees,
    get_worktree_path,
)


class TestGetWorktreePath:
    def test_basic_path(self):
        result = get_worktree_path("owner/repo", "pr", "42", "review-pr")
        assert result == WORKTREE_BASE / "owner--repo" / "pr-42" / "review-pr"

    def test_issue_path(self):
        result = get_worktree_path("org/proj", "issue", "100", "triage")
        assert result == WORKTREE_BASE / "org--proj" / "issue-100" / "triage"

    def test_discussion_path(self):
        result = get_worktree_path("user/repo", "discussion", "7", "summarize")
        assert result == WORKTREE_BASE / "user--repo" / "discussion-7" / "summarize"


class TestDetectOrphanWorktrees:
    @pytest.mark.asyncio
    async def test_finds_orphans(self, tmp_path):
        """Worktrees on disk with no matching session should be listed."""
        # Create fake worktree directories
        owner_dir = tmp_path / "owner--repo"
        pr_dir = owner_dir / "pr-42" / "review-pr"
        pr_dir.mkdir(parents=True)

        # Mock session store that returns an empty list (no active sessions)
        mock_store = MagicMock()
        mock_store.list_sessions = AsyncMock(return_value=[])

        with patch("shared.worktree_manager.WORKTREE_BASE", tmp_path):
            orphans = await detect_orphan_worktrees(mock_store)

        assert len(orphans) == 1
        assert orphans[0] == pr_dir

    @pytest.mark.asyncio
    async def test_no_orphans_when_sessions_exist(self, tmp_path):
        """Worktrees that have matching sessions should not be listed."""
        owner_dir = tmp_path / "owner--repo"
        pr_dir = owner_dir / "pr-42" / "review-pr"
        pr_dir.mkdir(parents=True)

        # Mock session with matching worktree_path
        mock_session = MagicMock()
        mock_session.worktree_path = str(pr_dir)
        mock_store = MagicMock()
        mock_store.list_sessions = AsyncMock(return_value=[mock_session])

        with patch("shared.worktree_manager.WORKTREE_BASE", tmp_path):
            orphans = await detect_orphan_worktrees(mock_store)

        assert len(orphans) == 0

    @pytest.mark.asyncio
    async def test_empty_when_base_dir_missing(self):
        """If the worktree base doesn't exist, return empty list."""
        mock_store = MagicMock()
        mock_store.list_sessions = AsyncMock(return_value=[])

        with patch(
            "shared.worktree_manager.WORKTREE_BASE",
            Path("/nonexistent/path/that/does/not/exist"),
        ):
            orphans = await detect_orphan_worktrees(mock_store)

        assert len(orphans) == 0

    @pytest.mark.asyncio
    async def test_partial_orphans(self, tmp_path):
        """Only orphaned worktrees are listed, active ones are excluded."""
        owner_dir = tmp_path / "owner--repo"
        orphan_dir = owner_dir / "pr-99" / "review-pr"
        active_dir = owner_dir / "pr-42" / "review-pr"
        orphan_dir.mkdir(parents=True)
        active_dir.mkdir(parents=True)

        mock_session = MagicMock()
        mock_session.worktree_path = str(active_dir)
        mock_store = MagicMock()
        mock_store.list_sessions = AsyncMock(return_value=[mock_session])

        with patch("shared.worktree_manager.WORKTREE_BASE", tmp_path):
            orphans = await detect_orphan_worktrees(mock_store)

        assert len(orphans) == 1
        assert orphans[0] == orphan_dir
