#!/usr/bin/env python3
"""Integration test for MCP configuration.

This script can be run manually or in CI to verify MCP servers are configured correctly.
It requires GITHUB_TOKEN environment variable to be set.

Usage:
    python tests/integration/test_mcp_config.py

In Docker:
    docker-compose exec sandbox_worker python /app/tests/integration/test_mcp_config.py
"""

import os
import sys
from pathlib import Path

# Add parent directory to path for shared imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from shared.sdk_factory import SDKOptionsBuilder  # noqa: E402


def main():
    """Test MCP configuration."""
    token = os.getenv("GITHUB_TOKEN")

    if not token:
        print("❌ GITHUB_TOKEN environment variable not set")
        print("   This test requires a GitHub token to validate MCP configuration")
        sys.exit(1)

    print("Testing MCP Configuration...\n")

    builder = SDKOptionsBuilder(cwd="/tmp")
    builder.with_github_mcp(token).with_github_actions_mcp(token)
    options = builder.build()

    print("✅ MCP Servers configured:")
    for name, config in options.mcp_servers.items():
        print(f'  - {name}: {config.get("type")}')
        if config.get("type") == "stdio":
            args = config.get("args", [])
            cmd_path = args[0] if args else ""
            print(f'    Command: {config.get("command")} {cmd_path}')
            print(f'    PYTHONPATH: {config.get("env", {}).get("PYTHONPATH", "N/A")}')

            # Verify file exists
            if cmd_path and os.path.exists(cmd_path):
                print("    ✅ Script file exists")
            elif cmd_path:
                print(f"    ❌ Script file not found: {cmd_path}")
                sys.exit(1)

    print("\n✅ All MCP servers configured correctly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
