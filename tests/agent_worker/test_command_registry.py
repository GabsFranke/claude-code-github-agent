"""Tests for command registry."""

from unittest.mock import AsyncMock, patch

import pytest

from services.agent_worker.commands.base import Command, CommandContext, CommandResult
from services.agent_worker.commands.registry import (
    CommandRegistry,
    get_command_registry,
)


class MockCommand(Command):
    """Mock command for testing."""

    def __init__(self, name: str, aliases: list[str] | None = None):
        super().__init__(name=name, description=f"{name} description", aliases=aliases)
        self._mock_build_prompt = AsyncMock(
            return_value=CommandResult(prompt=f"Prompt for {name}")
        )

    async def build_prompt(self, context: CommandContext) -> CommandResult:
        """Build prompt using mock."""
        return await self._mock_build_prompt(context)


class TestCommandRegistry:
    """Test CommandRegistry class."""

    def test_init(self):
        """Test registry initialization."""
        registry = CommandRegistry()

        assert registry._commands == {}
        assert registry._auto_triggers == {}

    def test_register_command(self):
        """Test registering a command."""
        registry = CommandRegistry()
        cmd = MockCommand("test")

        registry.register(cmd)

        assert registry._commands["test"] == cmd

    def test_register_command_with_aliases(self):
        """Test registering command with aliases."""
        registry = CommandRegistry()
        cmd = MockCommand("test", aliases=["t", "tst"])

        registry.register(cmd)

        assert registry._commands["test"] == cmd
        assert registry._commands["t"] == cmd
        assert registry._commands["tst"] == cmd

    def test_register_command_with_auto_trigger(self):
        """Test registering command with auto-trigger."""
        registry = CommandRegistry()
        cmd = MockCommand("review")

        registry.register(cmd, auto_trigger="auto_review")

        assert registry._commands["review"] == cmd
        assert registry._auto_triggers["auto_review"] == cmd

    def test_get_command_by_name(self):
        """Test getting command by name."""
        registry = CommandRegistry()
        cmd = MockCommand("test")
        registry.register(cmd)

        result = registry.get_command("test")

        assert result == cmd

    def test_get_command_by_alias(self):
        """Test getting command by alias."""
        registry = CommandRegistry()
        cmd = MockCommand("test", aliases=["t"])
        registry.register(cmd)

        result = registry.get_command("t")

        assert result == cmd

    def test_get_command_case_insensitive(self):
        """Test getting command is case-insensitive."""
        registry = CommandRegistry()
        cmd = MockCommand("test")
        registry.register(cmd)

        assert registry.get_command("TEST") == cmd
        assert registry.get_command("Test") == cmd

    def test_get_command_strips_whitespace(self):
        """Test getting command strips whitespace."""
        registry = CommandRegistry()
        cmd = MockCommand("test")
        registry.register(cmd)

        result = registry.get_command("  test  ")

        assert result == cmd

    def test_get_command_not_found(self):
        """Test getting non-existent command."""
        registry = CommandRegistry()

        result = registry.get_command("nonexistent")

        assert result is None

    def test_get_auto_trigger(self):
        """Test getting auto-trigger command."""
        registry = CommandRegistry()
        cmd = MockCommand("review")
        registry.register(cmd, auto_trigger="auto_review")

        result = registry.get_auto_trigger("auto_review")

        assert result == cmd

    def test_get_auto_trigger_not_found(self):
        """Test getting non-existent auto-trigger."""
        registry = CommandRegistry()

        result = registry.get_auto_trigger("auto_nonexistent")

        assert result is None

    @pytest.mark.asyncio
    async def test_execute_manual_command(self):
        """Test executing manual command."""
        registry = CommandRegistry()
        cmd = MockCommand("test")
        registry.register(cmd)

        context = CommandContext(
            repo="owner/repo",
            issue_number=1,
            command_text="test",
            user="user",
            event_type="manual",
            raw_data={},
        )

        result = await registry.execute(context)

        assert isinstance(result, CommandResult)
        assert "test" in result.prompt
        cmd._mock_build_prompt.assert_called_once_with(context)

    @pytest.mark.asyncio
    async def test_execute_auto_trigger(self):
        """Test executing auto-trigger command."""
        registry = CommandRegistry()
        cmd = MockCommand("review")
        registry.register(cmd, auto_trigger="auto_review")

        context = CommandContext(
            repo="owner/repo",
            issue_number=1,
            command_text="",
            user="user",
            event_type="auto_review",
            raw_data={},
        )

        result = await registry.execute(context)

        assert isinstance(result, CommandResult)
        cmd._mock_build_prompt.assert_called_once_with(context)

    @pytest.mark.asyncio
    async def test_execute_fallback_to_generic(self):
        """Test executing unknown command falls back to generic."""
        registry = CommandRegistry()
        generic_cmd = MockCommand("generic")
        registry.register(generic_cmd)

        context = CommandContext(
            repo="owner/repo",
            issue_number=1,
            command_text="unknown",
            user="user",
            event_type="manual",
            raw_data={},
        )

        result = await registry.execute(context)

        assert isinstance(result, CommandResult)
        generic_cmd._mock_build_prompt.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_no_fallback_raises(self):
        """Test executing unknown command without fallback raises."""
        registry = CommandRegistry()

        context = CommandContext(
            repo="owner/repo",
            issue_number=1,
            command_text="unknown",
            user="user",
            event_type="manual",
            raw_data={},
        )

        with pytest.raises(ValueError, match="Unknown command"):
            await registry.execute(context)

    def test_list_commands(self):
        """Test listing all commands."""
        registry = CommandRegistry()
        cmd1 = MockCommand("test1")
        cmd2 = MockCommand("test2", aliases=["t2"])
        registry.register(cmd1)
        registry.register(cmd2)

        commands = registry.list_commands()

        assert len(commands) == 2
        assert cmd1 in commands
        assert cmd2 in commands

    def test_list_commands_no_duplicates(self):
        """Test listing commands doesn't include alias duplicates."""
        registry = CommandRegistry()
        cmd = MockCommand("test", aliases=["t", "tst"])
        registry.register(cmd)

        commands = registry.list_commands()

        # Should only appear once despite having aliases
        assert len(commands) == 1
        assert commands[0] == cmd


class TestGetCommandRegistry:
    """Test get_command_registry function."""

    def test_get_command_registry_singleton(self):
        """Test registry is a singleton."""
        with patch(
            "services.agent_worker.commands.registry._register_builtin_commands"
        ):
            # Reset global registry
            import services.agent_worker.commands.registry as registry_module

            registry_module._registry = None

            registry1 = get_command_registry()
            registry2 = get_command_registry()

            assert registry1 is registry2

    def test_get_command_registry_registers_builtins(self):
        """Test registry registers builtin commands."""
        # Reset global registry
        import services.agent_worker.commands.registry as registry_module

        registry_module._registry = None

        with patch(
            "services.agent_worker.commands.registry._register_builtin_commands"
        ) as mock_register:
            registry = get_command_registry()

            mock_register.assert_called_once_with(registry)
