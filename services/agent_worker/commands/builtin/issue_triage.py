"""Issue triage command."""

from ..base import Command, CommandContext, CommandResult


class IssueTriageCommand(Command):
    """Command for triaging issues."""

    def __init__(self):
        super().__init__(
            name="triage",
            description="Triage an issue",
            aliases=["triage-issue"],
        )

    async def build_prompt(self, context: CommandContext) -> CommandResult:
        """Build issue triage prompt."""
        prompt = f"""You are triaging issue #{context.issue_number} in {context.repo}.

Analyze the issue and:
1. Add appropriate labels (bug, enhancement, documentation, question, etc.)
2. Assess priority and complexity
3. Suggest next steps or ask clarifying questions if needed
4. Post a comment with your analysis

Use the GitHub MCP tools to read the issue details and add labels."""

        return CommandResult(
            prompt=prompt,
            metadata={"command_type": "triage"},
        )
