"""Tests for the ``--model <tier>`` slash-command override."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.agent_worker.processors import RequestProcessor
from services.agent_worker.processors.request_processor import (
    _parse_model_flag,
    _parse_session_flag,
)


@pytest.mark.unit
class TestParseModelFlag:
    def test_no_flag(self):
        assert _parse_model_flag("/review please") == (None, "/review please")

    @pytest.mark.parametrize(
        "query",
        ["/review --model opus", "/review --model=opus", "/review --model Opus"],
    )
    def test_extracts_tier_and_strips_flag(self, query):
        assert _parse_model_flag(query) == ("opus", "/review")

    def test_flag_anywhere_in_query(self):
        tier, rest = _parse_model_flag("/review focus on auth --model haiku thanks")
        assert tier == "haiku"
        assert rest == "/review focus on auth thanks"

    def test_composes_with_session_flag(self):
        tier, rest = _parse_model_flag("/review --model sonnet --new redo it")
        assert tier == "sonnet"
        assert _parse_session_flag(rest) == ("new", "redo it")

    def test_unknown_tier_is_dropped(self):
        tier, rest = _parse_model_flag("/review --model claude-opus-5 x")
        assert tier is None
        assert rest == "/review x"


def _processor(workflow_model):
    """Build a RequestProcessor whose engine has one workflow with `model`."""
    job_queue = AsyncMock()
    job_queue.create_job = AsyncMock(return_value="job-1")
    token_manager = AsyncMock()
    token_manager.get_token = AsyncMock(return_value="tok")

    engine = MagicMock()
    engine.build_prompt = MagicMock(return_value=("prompt", None))
    wf = MagicMock()
    wf.model = workflow_model
    wf.streaming.enabled = False
    wf.conversation.persist = False
    engine.workflows = {"review-pr": wf}
    engine.get_conversation_config = MagicMock(return_value=wf.conversation)

    with patch(
        "services.agent_worker.processors.request_processor.WorkflowEngine",
        return_value=engine,
    ):
        processor = RequestProcessor(token_manager, AsyncMock(), job_queue)
    processor.context_loader.fetch_claude_md = AsyncMock(return_value="")
    processor.context_loader.fetch_memory_index = AsyncMock(return_value="")
    return processor, engine, job_queue


async def _run(processor, user_query):
    with patch("shared.get_queue", return_value=AsyncMock()):
        return await processor._execute(
            repo="o/r",
            issue_number=1,
            event_data={"event_type": "issue_comment", "action": "created"},
            user_query=user_query,
            user="u",
            ref="main",
            workflow_name="review-pr",
        )


@pytest.mark.unit
@pytest.mark.asyncio
class TestModelTierInJob:
    async def test_flag_overrides_workflow_model(self):
        processor, engine, job_queue = _processor(workflow_model="sonnet")
        await _run(processor, "/review --model opus check auth")

        job = job_queue.create_job.call_args.args[0]
        assert job["model"] == "opus"
        # Flag is stripped before the prompt is built
        assert engine.build_prompt.call_args.kwargs["user_query"] == (
            "/review check auth"
        )

    async def test_workflow_model_used_without_flag(self):
        processor, _, job_queue = _processor(workflow_model="opus")
        await _run(processor, "/review")

        assert job_queue.create_job.call_args.args[0]["model"] == "opus"

    async def test_unset_everywhere_leaves_it_to_the_cli(self):
        processor, _, job_queue = _processor(workflow_model=None)
        await _run(processor, "/review")

        assert job_queue.create_job.call_args.args[0]["model"] is None
