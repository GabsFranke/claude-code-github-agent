"""Tests for command base classes."""

import pytest

from services.agent_worker.commands.base import Command, CommandContext, CommandResult


class TestCommandContext:
    """Test CommandContext dataclass."""

    def test_init(self):
        """Test context initialization."""
        context = CommandContext(
            repo="owner/repo",
            issue_number=123,
            command_text="review",
            user="testuser",
            event_type="manual",
            raw_data={"key": "value"},
        )

        assert context.repo == "owner/repo"
        assert context.issue_number == 123
        assert context.command_text == "review"
        assert context.user == "testuser"
        assert context.event_type == "manual"
        assert context.raw_data == {"key": "value"}


class TestCommandResult:
    """Test CommandResult dataclass."""

    def test_init_with_metadata(self):
        """Test result initialization with metadata."""
        result = CommandResult(prompt="Test prompt", metadata={"key": "value"})

        assert result.prompt == "Test prompt"
        assert result.metadata == {"key": "value"}

    def test_init_without_metadata(self):
        """Test result initialization without metadata."""
        result = CommandResult(prompt="Test prompt")

        assert result.prompt == "Test prompt"
        assert result.metadata is None


class MockCommand(Command):
    """Mock command for testing."""

    async def build_prompt(self, context: CommandContext) -> CommandResult:
        """Build test prompt."""
        return CommandResult(prompt=f"Mock prompt for {context.command_text}")


class TestCommand:
    """Test Command base class."""

    def test_init(self):
        """Test command initialization."""
        cmd = MockCommand(
            name="test",
            description="Test command",
            aliases=["t", "tst"],
        )

        assert cmd.name == "test"
        assert cmd.description == "Test command"
        assert cmd.aliases == ["t", "tst"]

    def test_init_no_aliases(self):
        """Test command initialization without aliases."""
        cmd = MockCommand(name="test", description="Test command")

        assert cmd.name == "test"
        assert cmd.aliases == []

    def test_matches_name(self):
        """Test command matches by name."""
        cmd = MockCommand(name="test", description="Test")

        assert cmd.matches("test") is True
        assert cmd.matches("TEST") is True
        assert cmd.matches("  test  ") is True

    def test_matches_alias(self):
        """Test command matches by alias."""
        cmd = MockCommand(name="test", description="Test", aliases=["t"])

        assert cmd.matches("t") is True
        assert cmd.matches("T") is True

    def test_matches_no_match(self):
        """Test command doesn't match wrong text."""
        cmd = MockCommand(name="test", description="Test", aliases=["t"])

        assert cmd.matches("review") is False
        assert cmd.matches("testing") is False

    @pytest.mark.asyncio
    async def test_build_prompt(self):
        """Test build_prompt implementation."""
        cmd = MockCommand(name="test", description="Test")
        context = CommandContext(
            repo="owner/repo",
            issue_number=1,
            command_text="test",
            user="user",
            event_type="manual",
            raw_data={},
        )

        result = await cmd.build_prompt(context)

        assert isinstance(result, CommandResult)
        assert "Mock prompt" in result.prompt
        assert "test" in result.prompt

    def test_abstract_build_prompt(self):
        """Test that Command is abstract."""

        class IncompleteCommand(Command):
            pass

        # Should not be able to instantiate without implementing build_prompt
        with pytest.raises(TypeError):
            IncompleteCommand(name="test", description="Test")
