"""Generate .mcp.json for worktrees so the Claude Code CLI loads MCP servers.

The Claude Code CLI reads .mcp.json from the project root when
``setting_sources`` includes ``"project"``.  By writing this file into
each worktree at creation time, we eliminate the need for programmatic
``mcp_servers={}`` registration in the SDK builder.

Environment variables use ``${VAR}`` / ``${VAR:-default}`` syntax which
the CLI interpolates at load time — no Python-side resolution needed.
"""

import json
import logging
import os
import re
from typing import Any
from urllib.parse import quote

logger = logging.getLogger(__name__)

_PARAM_KEY_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

# Default application root inside the Docker container
_APP_ROOT = "/app"


def generate_mcp_json(
    repo: str,
    worktree_path: str,
    app_root: str | None = None,
) -> dict[str, Any]:
    """Generate ``.mcp.json`` content declaring all app MCP servers.

    Args:
        repo: Repository identifier (e.g., ``"owner/repo"``).
        worktree_path: Absolute path to the git worktree.
        app_root: Application root path (default: ``/app``).

    Returns:
        Dict matching the ``.mcp.json`` schema
        (``{"mcpServers": {…}}``).
    """
    if not app_root:
        app_root = _APP_ROOT

    servers: dict[str, Any] = {}

    # ── External Servers ──────────────────────────────────────────

    # GitHub HTTP MCP — SDK resolves ${GITHUB_TOKEN} at load time;
    # if the var is unset the server simply won't authenticate.
    servers["github"] = {
        "type": "http",
        "url": "https://api.githubcopilot.com/mcp",
        "headers": {
            "Authorization": "Bearer ${GITHUB_TOKEN}",
        },
    }

    # ── Auto-Discovered Local Servers ─────────────────────────────

    mcp_servers_dir = os.path.join(app_root, "mcp_servers")
    if os.path.isdir(mcp_servers_dir):
        for server_name in os.listdir(mcp_servers_dir):
            server_dir = os.path.join(mcp_servers_dir, server_name)
            server_script = os.path.join(server_dir, "server.py")

            if not os.path.isdir(server_dir) or not os.path.isfile(server_script):
                continue

            # Default proxy URL format with standard variables
            # Use localhost:18000 — socat in the container bridges this to mcp_proxy:18000,
            # and host mapping bridges it to the mcp_proxy container.
            proxy_host = os.getenv("MCP_PROXY_HOST", "localhost")
            base_url = f"http://{proxy_host}:18000/mcp/{server_name}/sse"
            params = [
                f"repo_path={worktree_path}",
                f"github_repository={repo}",
            ]
            # SECURITY: github_token is passed via environment variable
            # (ALLOWED_ENV_PREFIXES whitelists GITHUB_*), NOT in the URL
            # query string where it could be logged.

            # Check for mcp.json to handle conditions and extra params
            mcp_json_path = os.path.join(server_dir, "mcp.json")
            skip_server = False

            if os.path.isfile(mcp_json_path):
                try:
                    with open(mcp_json_path, encoding="utf-8") as f:
                        config = json.load(f)

                    # Check condition (e.g. "INDEXING_ENABLED=true")
                    condition = config.get("condition")
                    if condition and "=" in condition:
                        env_var, expected_val = condition.split("=", 1)
                        if os.getenv(env_var, "false").lower() != expected_val.lower():
                            skip_server = True

                    # Add extra params
                    extra_params = config.get("extra_params", {})
                    for k, v in extra_params.items():
                        if not _PARAM_KEY_RE.match(k):
                            logger.warning(f"Skipping invalid extra_param key: {k!r}")  # type: ignore[unreachable]
                            continue
                        params.append(f"{k}={quote(str(v))}")

                except Exception as e:
                    logger.warning(f"Failed to parse {mcp_json_path}: {e}")

            if skip_server:
                continue

            url = f"{base_url}?{'&'.join(params)}"
            # Use "sse" type — our proxy implements the MCP SSE (2024-11) protocol
            # with a separate endpoint event + POST endpoint, not Streamable HTTP.
            servers[server_name] = {"type": "sse", "url": url}

    return {"mcpServers": servers}


def write_mcp_json(worktree_path: str, repo: str) -> str:
    """Write ``.mcp.json`` to a worktree root.

    The Claude Code CLI reads this file when ``setting_sources``
    includes ``"project"``.

    Args:
        worktree_path: Absolute path to the git worktree.
        repo: Repository identifier (e.g., ``"owner/repo"``).

    Returns:
        Absolute path to the written file.
    """
    content = generate_mcp_json(
        repo=repo, worktree_path=worktree_path, app_root=_APP_ROOT
    )
    mcp_path = os.path.join(worktree_path, ".mcp.json")

    with open(mcp_path, "w", encoding="utf-8") as f:
        json.dump(content, f, indent=2)
        f.write("\n")

    server_names = list(content["mcpServers"].keys())
    logger.info(
        f"Wrote .mcp.json to {mcp_path} with {len(server_names)} servers: "
        f"{', '.join(server_names)}"
    )
    return mcp_path
