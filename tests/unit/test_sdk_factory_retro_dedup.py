"""Tests for SDKOptionsBuilder buffer-and-flush post-processing logic."""

from unittest.mock import AsyncMock, patch

import pytest

from shared.sdk_factory import SDKOptionsBuilder


class TestFlushPendingPostJobs:
    """Tests for the buffer-and-flush mechanism in SDKOptionsBuilder."""

    @pytest.mark.asyncio
    async def test_no_jobs_buffered_is_noop(self):
        """Flushing with no buffered jobs is a safe no-op."""
        builder = SDKOptionsBuilder(cwd="/tmp")
        # Should not raise
        await builder.flush_pending_post_jobs()

    @pytest.mark.asyncio
    async def test_dedup_keeps_last_job_per_key(self):
        """Multiple buffered jobs for the same (path, event, type) are deduped,
        keeping only the last entry."""
        builder = SDKOptionsBuilder(cwd="/tmp")
        staged_path = "/home/bot/transcripts/test/repo/session1.jsonl"

        # Simulate SDK firing Stop 5 times for the same transcript
        for i in range(5):
            builder._pending_post_jobs.append(
                {
                    "type": "retrospector",
                    "repo": "test/repo",
                    "staged_path": staged_path,
                    "event": "Stop",
                    "workflow_name": "review-pr",
                    "session_meta": {
                        "num_turns": (i + 1) * 2,
                        "is_error": False,
                        "duration_ms": (i + 1) * 1000,
                    },
                }
            )

        with patch("shared.sdk_factory._enqueue_retrospector_job") as mock_retro:
            mock_retro.return_value = AsyncMock()
            await builder.flush_pending_post_jobs()

        # Only 1 job should be enqueued (the last one)
        assert mock_retro.call_count == 1
        call_meta = (
            mock_retro.call_args[1].get("session_meta") or mock_retro.call_args[0][4]
        )
        assert call_meta["num_turns"] == 10  # Last entry had num_turns=10

    @pytest.mark.asyncio
    async def test_different_events_not_deduped(self):
        """Stop and SubagentStop for the same transcript are NOT deduped."""
        builder = SDKOptionsBuilder(cwd="/tmp")
        staged_path = "/home/bot/transcripts/test/repo/session1.jsonl"

        builder._pending_post_jobs.append(
            {
                "type": "retrospector",
                "repo": "test/repo",
                "staged_path": staged_path,
                "event": "Stop",
                "workflow_name": "review-pr",
                "session_meta": {"num_turns": 10},
            }
        )
        builder._pending_post_jobs.append(
            {
                "type": "retrospector",
                "repo": "test/repo",
                "staged_path": staged_path,
                "event": "SubagentStop",
                "workflow_name": "review-pr",
                "session_meta": {"num_turns": 3, "agent_id": "comment-analyzer"},
            }
        )

        with patch("shared.sdk_factory._enqueue_retrospector_job") as mock_retro:
            mock_retro.return_value = AsyncMock()
            await builder.flush_pending_post_jobs()

        assert mock_retro.call_count == 2

    @pytest.mark.asyncio
    async def test_different_types_not_deduped(self):
        """Retrospector and memory jobs for the same (path, event) are NOT deduped."""
        builder = SDKOptionsBuilder(cwd="/tmp")
        staged_path = "/home/bot/transcripts/test/repo/session1.jsonl"

        builder._pending_post_jobs.append(
            {
                "type": "retrospector",
                "repo": "test/repo",
                "staged_path": staged_path,
                "event": "Stop",
                "workflow_name": "review-pr",
                "session_meta": {"num_turns": 10},
            }
        )
        builder._pending_post_jobs.append(
            {
                "type": "memory",
                "repo": "test/repo",
                "staged_path": staged_path,
                "event": "Stop",
                "claude_md": "# Test",
                "memory_index": None,
            }
        )

        with (
            patch("shared.sdk_factory._enqueue_retrospector_job") as mock_retro,
            patch("shared.sdk_factory._enqueue_memory_job") as mock_mem,
        ):
            mock_retro.return_value = AsyncMock()
            mock_mem.return_value = AsyncMock()
            await builder.flush_pending_post_jobs()

        assert mock_retro.call_count == 1
        assert mock_mem.call_count == 1

    @pytest.mark.asyncio
    async def test_buffer_is_cleared_after_flush(self):
        """After flush, the internal buffer is empty (safe to reuse builder)."""
        builder = SDKOptionsBuilder(cwd="/tmp")

        builder._pending_post_jobs.append(
            {
                "type": "retrospector",
                "repo": "test/repo",
                "staged_path": "/path/transcript.jsonl",
                "event": "Stop",
                "workflow_name": "test",
                "session_meta": {},
            }
        )

        with patch("shared.sdk_factory._enqueue_retrospector_job") as mock_retro:
            mock_retro.return_value = AsyncMock()
            await builder.flush_pending_post_jobs()

        assert builder._pending_post_jobs == []

        # Second flush is a no-op
        with patch("shared.sdk_factory._enqueue_retrospector_job") as mock_retro:
            mock_retro.return_value = AsyncMock()
            await builder.flush_pending_post_jobs()

        assert mock_retro.call_count == 0

    @pytest.mark.asyncio
    async def test_subagent_transcripts_not_deduped_with_main(self):
        """Subagent transcripts (different staged_path) are not deduped with main."""
        builder = SDKOptionsBuilder(cwd="/tmp")

        builder._pending_post_jobs.append(
            {
                "type": "retrospector",
                "repo": "test/repo",
                "staged_path": "/home/bot/transcripts/test/repo/main.jsonl",
                "event": "Stop",
                "workflow_name": "review-pr",
                "session_meta": {"num_turns": 10},
            }
        )
        builder._pending_post_jobs.append(
            {
                "type": "retrospector",
                "repo": "test/repo",
                "staged_path": "/home/bot/transcripts/test/repo/subagent.jsonl",
                "event": "SubagentStop",
                "workflow_name": "review-pr",
                "session_meta": {"num_turns": 3, "agent_id": "comment-analyzer"},
            }
        )

        with patch("shared.sdk_factory._enqueue_retrospector_job") as mock_retro:
            mock_retro.return_value = AsyncMock()
            await builder.flush_pending_post_jobs()

        assert mock_retro.call_count == 2
