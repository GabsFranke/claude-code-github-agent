"""MCP server configuration builder."""

import logging
from typing import TYPE_CHECKING

from claude_agent_sdk import ClaudeAgentOptions

from subagents import AGENTS

if TYPE_CHECKING:
    from ..auth import GitHubTokenManager

logger = logging.getLogger(__name__)


class MCPConfigurationBuilder:
    """Builds MCP server configuration for Claude Agent SDK."""

    def __init__(self, token_manager: "GitHubTokenManager"):
        self.token_manager = token_manager

    async def create_mcp_config(self) -> dict:
        """Create MCP server configuration."""
        github_token = await self.token_manager.get_token()
        return {
            "github": {
                "type": "http",
                "url": "https://api.githubcopilot.com/mcp",
                "headers": {"Authorization": f"Bearer {github_token}"},
            }
        }

    def create_agent_options(
        self, mcp_servers: dict, hooks: dict
    ) -> ClaudeAgentOptions:
        """Create Claude Agent SDK options."""
        return ClaudeAgentOptions(
            allowed_tools=["Task", "mcp__github__*"],
            permission_mode="acceptEdits",
            mcp_servers=mcp_servers,
            agents=AGENTS,
            plugins=[{"type": "local", "path": "/app/plugins/pr-review-toolkit"}],
            hooks=hooks,
            max_turns=50,
        )
