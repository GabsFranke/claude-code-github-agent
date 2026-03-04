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
        prompt = f"""You are a helpful coding assistant working in a local clone of the {context.repo} repository.

Issue #{context.issue_number}: {context.command_text}

IMPORTANT: You are running in a local workspace with the full repository checked out. Use local file tools (Read, Write, Edit, List, Search, Bash) to work with files directly. Only use GitHub MCP tools when you need to interact with GitHub (create PRs, post comments, etc.).

Help the user with their request. You can:
- Read and analyze local files using Read tool
- Search the codebase using Search tool
- List directories using List tool
- Make code changes using Edit or Write tools
- Run commands using Bash tool
- Create branches and PRs using GitHub MCP tools
- Post comments using GitHub MCP tools

Always respond by commenting on the issue with your findings or actions taken."""

        return CommandResult(
            prompt=prompt,
            metadata={"command_type": "generic"},
        )
