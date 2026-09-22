"""Tests for orphaned streaming-session recovery in the agent worker.

Recovery synthesises an event to re-drive ``RequestProcessor.process``. That
synthetic event carries no webhook payload, so whatever it says about the
thread must be honoured downstream — otherwise a recovered PR session is
re-keyed as an issue session and takes a *different* worktree lock than real
PR events, letting a concurrent PR webhook run in parallel on the same repo.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.session_store import UnifiedSessionInfo, resolve_thread_type


def _pr_session(**overrides) -> UnifiedSessionInfo:
    data = {
        "session_id": "",
        "repo": "owner/repo",
        "thread_type": "pr",
        "thread_id": "42",
        "workflow_name": "review-pr",
        "ref": "feature-branch",
        "worktree_path": "",
        "created_at": "2026-01-01T00:00:00+00:00",
        "last_run": "2026-01-01T00:00:00+00:00",
        "status": "running",
        "installation_id": "1",
        "initial_query": "please review",
        "issue_number": "42",
        "user": "someone",
        "streaming_token": "tok-orphan",
        "run_count": 1,
    }
    data.update(overrides)
    return UnifiedSessionInfo.model_validate(data)


async def _run_recovery(info: UnifiedSessionInfo) -> dict:
    """Drive ``_recover_session`` and return the event_data it produced."""
    from services.agent_worker.worker import _recover_session

    store = MagicMock()
    store.get_job_id = AsyncMock(return_value=None)
    store.set_completed = AsyncMock()

    job_queue = MagicMock()
    job_queue.redis = MagicMock()
    job_queue.get_job_status = AsyncMock(return_value=None)

    request_processor = MagicMock()
    request_processor.process = AsyncMock()

    lock = MagicMock()
    lock.get_lock_info = AsyncMock(return_value=None)

    with patch("services.agent_worker.worker.WorktreeLock", return_value=lock):
        await _recover_session(info, store, job_queue, request_processor)

    request_processor.process.assert_awaited_once()
    return request_processor.process.await_args.kwargs["event_data"]


class TestRecoveryForwardsThreadType:
    @pytest.mark.asyncio
    async def test_pr_session_forwards_thread_type(self):
        event_data = await _run_recovery(_pr_session())

        assert event_data["thread_type"] == "pr"

    @pytest.mark.asyncio
    async def test_recovered_pr_session_resolves_back_to_pr(self):
        """The round trip is what matters, not the hint in isolation.

        ``resolve_thread_type`` only consults ``is_pr`` inside the
        ``issue_comment`` branch, so a recovery event carrying only that hint
        fell straight through to "issue".
        """
        event_data = await _run_recovery(_pr_session())

        assert resolve_thread_type(event_data) == "pr"

    @pytest.mark.asyncio
    async def test_recovered_issue_session_resolves_to_issue(self):
        event_data = await _run_recovery(
            _pr_session(thread_type="issue", workflow_name="triage")
        )

        assert resolve_thread_type(event_data) == "issue"

    @pytest.mark.asyncio
    async def test_recovered_discussion_session_resolves_to_discussion(self):
        event_data = await _run_recovery(
            _pr_session(thread_type="discussion", workflow_name="answer")
        )

        assert resolve_thread_type(event_data) == "discussion"
