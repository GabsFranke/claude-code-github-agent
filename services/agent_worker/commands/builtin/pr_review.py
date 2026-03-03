"""PR review command."""

from ..base import Command, CommandContext, CommandResult


class PRReviewCommand(Command):
    """Command for reviewing pull requests."""

    def __init__(self):
        super().__init__(
            name="review-pr",
            description="Review a pull request",
            aliases=["review", "pr-review"],
        )

    async def build_prompt(self, context: CommandContext) -> CommandResult:
        """Build PR review prompt."""
        # Use plugin command for comprehensive review
        prompt = (
            f"/pr-review-toolkit:review-pr {context.repo} {context.issue_number} all"
        )

        return CommandResult(
            prompt=prompt,
            metadata={
                "command_type": "pr_review",
                "uses_plugin": True,
            },
        )
