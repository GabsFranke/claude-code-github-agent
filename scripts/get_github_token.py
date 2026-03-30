#!/usr/bin/env python3
"""Generate a GitHub installation token for testing."""

import asyncio
import os
import sys
from pathlib import Path

from shared.github_auth import GitHubAuthService

# Load .env file
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    # If python-dotenv not installed, try to read .env manually
    env_file = Path(__file__).parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ[key] = value.strip('"').strip("'")


async def main():
    """Generate and print GitHub installation token."""
    try:
        async with GitHubAuthService() as auth_service:
            token = await auth_service.get_token()
            print(token)
        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
