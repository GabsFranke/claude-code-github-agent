"""Claude Code settings configuration."""

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def setup_claude_settings():
    """Configure Claude Code settings from environment variables."""
    settings_file = Path.home() / ".claude" / "settings.json"
    settings_file.parent.mkdir(parents=True, exist_ok=True)

    settings = {}
    if settings_file.exists():
        try:
            with open(settings_file, encoding="utf-8") as f:
                settings = json.load(f)
        except Exception as e:
            logger.warning(f"Could not read existing settings: {e}")

    # Configure permissions
    settings["permissions"] = {
        "allow": ["Task", "mcp__github"],
        "deny": [],
        "ask": [],
    }
    settings["enableAllProjectMcpServers"] = True

    # Custom env vars
    custom_env = {}
    env_vars = [
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_VERTEX_PROJECT_ID",
        "ANTHROPIC_VERTEX_REGION",
    ]

    for var in env_vars:
        if os.getenv(var):
            custom_env[var] = os.getenv(var)

    # Langfuse env vars — only enabled when LANGFUSE_ENABLED is truthy
    # (set LANGFUSE_ENABLED=true in .env and include docker-compose.langfuse.yml)
    if (
        os.getenv("LANGFUSE_ENABLED", "false").lower() in ("true", "1", "yes")
        and os.getenv("LANGFUSE_PUBLIC_KEY")
        and os.getenv("LANGFUSE_SECRET_KEY")
    ):
        custom_env.update(
            {
                "TRACE_TO_LANGFUSE": "true",
                "LANGFUSE_PUBLIC_KEY": os.getenv("LANGFUSE_PUBLIC_KEY"),
                "LANGFUSE_SECRET_KEY": os.getenv("LANGFUSE_SECRET_KEY"),
                "LANGFUSE_HOST": os.getenv("LANGFUSE_HOST", "http://langfuse:3000"),
                "LANGFUSE_BASE_URL": os.getenv("LANGFUSE_HOST", "http://langfuse:3000"),
                "CC_LANGFUSE_DEBUG": "true",
            }
        )

    if custom_env:
        settings.setdefault("env", {}).update(custom_env)

    with open(settings_file, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)

    logger.info("Claude Code settings configured")
