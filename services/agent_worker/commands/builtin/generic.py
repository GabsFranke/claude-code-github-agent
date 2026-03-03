"""Generic command handler."""

from ..base import Command, CommandContext, CommandResult


class GenericCommand(Command):
    """Generic command for any user request."""

    def __init__(self):
        super().__init__(
            name="generic",
            description="Handle any generic request",
            aliases=[],
        )

    async def build_prompt(self, context: CommandContext) -> CommandResult:
        """Build generic prompt."""
        prompt = f"""You are a helpful coding assistant with access to the {context.repo} repository via GitHub MCP tools.

Issue #{context.issue_number}: {context.command_text}

Help the user with their request. You can:
- Answer questions about the code
- Review and analyze code
- Create branches and PRs
- Make code changes
- Provide explanations

Always respond by commenting on the issue with your findings or actions taken."""

        return CommandResult(
            prompt=prompt,
            metadata={"command_type": "generic"},
        )
