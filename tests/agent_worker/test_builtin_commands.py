"""Unit tests for builtin commands."""

import pytest

from services.agent_worker.commands.base import CommandContext
from services.agent_worker.commands.builtin import (
    GenericCommand,
    IssueTriageCommand,
    PRReviewCommand,
)


class TestGenericCommand:
    """Test GenericCommand class."""

    def test_initialization(self):
        """Test GenericCommand initialization."""
        cmd = GenericCommand()
        assert cmd.name == "generic"
        assert cmd.description == "Handle any generic request"
        assert cmd.aliases == []

    @pytest.mark.asyncio
    async def test_build_prompt(self):
        """Test building generic command prompt."""
        cmd = GenericCommand()
        context = CommandContext(
            repo="owner/repo",
            issue_number=123,
            command_text="help me fix this bug",
            event_type="issue_comment",
            user="testuser",
            raw_data={},
        )

        result = await cmd.build_prompt(context)

        assert result.prompt is not None
        assert "owner/repo" in result.prompt
        assert "Issue #123" in result.prompt
        assert "help me fix this bug" in result.prompt
        assert "helpful coding assistant" in result.prompt
        assert result.metadata["command_type"] == "generic"

    @pytest.mark.asyncio
    async def test_build_prompt_with_different_context(self):
        """Test generic command with different context."""
        cmd = GenericCommand()
        context = CommandContext(
            repo="test/project",
            issue_number=456,
            command_text="explain the authentication flow",
            event_type="issue_comment",
            user="developer",
            raw_data={},
        )

        result = await cmd.build_prompt(context)

        assert "test/project" in result.prompt
        assert "Issue #456" in result.prompt
        assert "explain the authentication flow" in result.prompt
        assert result.metadata["command_type"] == "generic"


class TestIssueTriageCommand:
    """Test IssueTriageCommand class."""

    def test_initialization(self):
        """Test IssueTriageCommand initialization."""
        cmd = IssueTriageCommand()
        assert cmd.name == "triage"
        assert cmd.description == "Triage an issue"
        assert "triage-issue" in cmd.aliases

    @pytest.mark.asyncio
    async def test_build_prompt(self):
        """Test building triage command prompt."""
        cmd = IssueTriageCommand()
        context = CommandContext(
            repo="owner/repo",
            issue_number=789,
            command_text="triage",
            event_type="issue_comment",
            user="maintainer",
            raw_data={},
        )

        result = await cmd.build_prompt(context)

        assert result.prompt is not None
        assert "triaging issue #789" in result.prompt
        assert "owner/repo" in result.prompt
        assert "labels" in result.prompt
        assert "priority" in result.prompt
        assert "complexity" in result.prompt
        assert result.metadata["command_type"] == "triage"

    @pytest.mark.asyncio
    async def test_build_prompt_includes_instructions(self):
        """Test triage prompt includes all necessary instructions."""
        cmd = IssueTriageCommand()
        context = CommandContext(
            repo="test/repo",
            issue_number=1,
            command_text="triage-issue",
            event_type="issue_comment",
            user="user",
            raw_data={},
        )

        result = await cmd.build_prompt(context)

        # Check for key triage instructions
        assert "Add appropriate labels" in result.prompt
        assert "bug" in result.prompt
        assert "enhancement" in result.prompt
        assert "GitHub MCP tools" in result.prompt


class TestPRReviewCommand:
    """Test PRReviewCommand class."""

    def test_initialization(self):
        """Test PRReviewCommand initialization."""
        cmd = PRReviewCommand()
        assert cmd.name == "review-pr"
        assert cmd.description == "Review a pull request"
        assert "review" in cmd.aliases
        assert "pr-review" in cmd.aliases

    @pytest.mark.asyncio
    async def test_build_prompt(self):
        """Test building PR review command prompt."""
        cmd = PRReviewCommand()
        context = CommandContext(
            repo="owner/repo",
            issue_number=42,
            command_text="review-pr",
            event_type="pull_request",
            user="contributor",
            raw_data={},
        )

        result = await cmd.build_prompt(context)

        assert result.prompt is not None
        assert "/pr-review-toolkit:review-pr" in result.prompt
        assert "owner/repo" in result.prompt
        assert "42" in result.prompt
        assert "all" in result.prompt
        assert result.metadata["command_type"] == "pr_review"
        assert result.metadata["uses_plugin"] is True

    @pytest.mark.asyncio
    async def test_build_prompt_uses_plugin(self):
        """Test PR review uses plugin command."""
        cmd = PRReviewCommand()
        context = CommandContext(
            repo="test/project",
            issue_number=100,
            command_text="review",
            event_type="pull_request",
            user="reviewer",
            raw_data={},
        )

        result = await cmd.build_prompt(context)

        # Verify it's using the plugin toolkit
        assert result.prompt.startswith("/pr-review-toolkit:review-pr")
        assert result.metadata.get("uses_plugin") is True

    @pytest.mark.asyncio
    async def test_build_prompt_format(self):
        """Test PR review prompt has correct format."""
        cmd = PRReviewCommand()
        context = CommandContext(
            repo="org/repository",
            issue_number=999,
            command_text="pr-review",
            event_type="pull_request",
            user="bot",
            raw_data={},
        )

        result = await cmd.build_prompt(context)

        # Check exact format: /pr-review-toolkit:review-pr {repo} {pr_number} all
        expected_parts = [
            "/pr-review-toolkit:review-pr",
            "org/repository",
            "999",
            "all",
        ]
        for part in expected_parts:
            assert part in result.prompt


class TestBuiltinCommandsModule:
    """Test builtin commands module exports."""

    def test_module_exports(self):
        """Test that all commands are exported from __init__.py."""
        from services.agent_worker.commands.builtin import __all__

        assert "GenericCommand" in __all__
        assert "IssueTriageCommand" in __all__
        assert "PRReviewCommand" in __all__
        assert len(__all__) == 3

    def test_all_commands_instantiable(self):
        """Test that all exported commands can be instantiated."""
        from services.agent_worker.commands.builtin import (
            GenericCommand,
            IssueTriageCommand,
            PRReviewCommand,
        )

        # Should not raise any exceptions
        generic = GenericCommand()
        triage = IssueTriageCommand()
        pr_review = PRReviewCommand()

        assert generic is not None
        assert triage is not None
        assert pr_review is not None
