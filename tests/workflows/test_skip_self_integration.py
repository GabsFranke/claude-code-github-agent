"""Integration tests for skip_self with event actor logic."""

import pytest

from workflows.engine import WorkflowEngine


class TestSkipSelfWithEventActor:
    """Test skip_self behavior with different event actors."""

    @pytest.fixture
    def engine(self):
        """Load the real workflows.yaml."""
        from pathlib import Path

        workflow_path = Path(__file__).parent.parent.parent / "workflows.yaml"
        if not workflow_path.exists():
            pytest.skip("workflows.yaml not found")
        return WorkflowEngine(workflow_path)

    def test_bot_opens_pr_should_skip(self, engine):
        """Bot opens PR - should skip automatic review."""
        workflow_name = engine.get_workflow_for_event("pull_request", "opened")
        if not workflow_name:
            pytest.skip("No workflow for pull_request.opened")

        # Bot is the actor (sender) - should skip
        assert engine.should_skip_self(workflow_name, "bot-user", "bot-user") is True

    def test_human_opens_pr_should_not_skip(self, engine):
        """Human opens PR - should process review."""
        workflow_name = engine.get_workflow_for_event("pull_request", "opened")
        if not workflow_name:
            pytest.skip("No workflow for pull_request.opened")

        # Human is the actor (sender) - should not skip
        assert engine.should_skip_self(workflow_name, "human-user", "bot-user") is False

    def test_human_comments_on_bot_pr_should_not_skip(self, engine):
        """Human comments /review on bot's PR - should process."""
        workflow_name = engine.get_workflow_for_command("/review")
        if not workflow_name:
            pytest.skip("No workflow for /review command")

        # Human is the comment author (sender) - should not skip
        # Even though the PR owner is the bot
        assert engine.should_skip_self(workflow_name, "human-user", "bot-user") is False

    def test_bot_comments_on_own_pr_should_skip(self, engine):
        """Bot comments /review on its own PR - should skip."""
        workflow_name = engine.get_workflow_for_command("/review")
        if not workflow_name:
            pytest.skip("No workflow for /review command")

        # Bot is the comment author (sender) - should skip
        assert engine.should_skip_self(workflow_name, "bot-user", "bot-user") is True

    def test_bot_comments_on_human_pr_should_skip(self, engine):
        """Bot comments /review on human's PR - should skip."""
        workflow_name = engine.get_workflow_for_command("/review")
        if not workflow_name:
            pytest.skip("No workflow for /review command")

        # Bot is the comment author (sender) - should skip
        # Even though the PR owner is human
        assert engine.should_skip_self(workflow_name, "bot-user", "bot-user") is True

    def test_generic_workflow_with_skip_self_false(self, engine):
        """Generic workflow with skip_self=false should never skip."""
        workflow_name = engine.get_workflow_for_command("/agent")
        if not workflow_name:
            pytest.skip("No workflow for /agent command")

        # Check if skip_self is false for generic workflow
        if engine.workflows[workflow_name].skip_self is False:
            # Should not skip even if bot is the actor
            assert (
                engine.should_skip_self(workflow_name, "bot-user", "bot-user") is False
            )
            assert (
                engine.should_skip_self(workflow_name, "human-user", "bot-user")
                is False
            )

    def test_different_bot_usernames(self, engine):
        """Test with different bot username formats."""
        workflow_name = engine.get_workflow_for_event("pull_request", "opened")
        if not workflow_name:
            pytest.skip("No workflow for pull_request.opened")

        # Test various bot username formats
        bot_usernames = [
            "claude-code-agent[bot]",
            "my-bot",
            "github-actions[bot]",
        ]

        for bot_username in bot_usernames:
            # Bot triggers - should skip
            assert (
                engine.should_skip_self(workflow_name, bot_username, bot_username)
                is True
            )
            # Human triggers - should not skip
            assert (
                engine.should_skip_self(workflow_name, "human-user", bot_username)
                is False
            )

    def test_empty_event_actor(self, engine):
        """Test with empty event actor (edge case)."""
        workflow_name = engine.get_workflow_for_event("pull_request", "opened")
        if not workflow_name:
            pytest.skip("No workflow for pull_request.opened")

        # Empty actor should not match bot username
        assert (
            engine.should_skip_self(workflow_name, "", "claude-code-agent[bot]")
            is False
        )

    def test_case_sensitive_username_matching(self, engine):
        """Test that username matching is case-sensitive."""
        workflow_name = engine.get_workflow_for_event("pull_request", "opened")
        if not workflow_name:
            pytest.skip("No workflow for pull_request.opened")

        # Different case should not match
        assert engine.should_skip_self(workflow_name, "Bot-User", "bot-user") is False
        assert engine.should_skip_self(workflow_name, "bot-user", "Bot-User") is False

        # Exact match should skip
        assert engine.should_skip_self(workflow_name, "bot-user", "bot-user") is True
