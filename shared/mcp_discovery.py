"""Auto-discovery for MCP servers in mcp_servers/ directory.

Scans mcp_servers/ for subdirectories containing server.py files,
reads optional mcp.json metadata, and returns structured configs.
"""

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel

logger = logging.getLogger(__name__)

_SKIP_ENTRIES = {"__pycache__", "__init__.py", "base.py", "http.json"}


class MCPManifest(BaseModel):
    """Optional per-server metadata from mcp.json."""

    env: dict[str, str] = {}


class MCPServerConfig(BaseModel):
    """Discovered stdio MCP server configuration."""

    name: str
    server_path: str
    env: dict[str, str] = {}


def _resolve_base_path() -> str:
    """Resolve the mcp_servers/ base path.

    Checks Docker path first, then falls back to project-local path.

    Returns:
        Absolute path to mcp_servers/ directory.
    """
    docker_path = "/app/mcp_servers"
    if os.path.exists(docker_path):
        return docker_path

    project_path = os.path.join(Path(__file__).parent.parent, "mcp_servers")
    return project_path


def _read_manifest(server_dir: str) -> MCPManifest:
    """Read optional mcp.json from a server directory.

    Args:
        server_dir: Path to the server directory.

    Returns:
        MCPManifest (empty defaults if file missing).
    """
    manifest_path = os.path.join(server_dir, "mcp.json")
    if os.path.exists(manifest_path):
        try:
            with open(manifest_path, encoding="utf-8") as f:
                data = json.load(f)
            return MCPManifest.model_validate(data)  # type: ignore[no-any-return]
        except Exception as e:
            logger.warning(f"Failed to read mcp.json from {server_dir}: {e}")
    return MCPManifest()


def discover_stdio_servers() -> list[MCPServerConfig]:
    """Discover all stdio MCP servers in mcp_servers/ directory.

    Scans for subdirectories containing server.py, reads optional
    mcp.json for custom env vars.

    Returns:
        List of discovered server configs.
    """
    base_path = _resolve_base_path()

    if not os.path.exists(base_path):
        logger.warning(f"MCP servers directory not found: {base_path}")
        return []

    servers: list[MCPServerConfig] = []

    for entry in sorted(os.listdir(base_path)):
        if entry.startswith(".") or entry in _SKIP_ENTRIES:
            continue

        server_dir = os.path.join(base_path, entry)
        if not os.path.isdir(server_dir):
            continue

        server_script = os.path.join(server_dir, "server.py")
        if not os.path.exists(server_script):
            continue

        manifest = _read_manifest(server_dir)

        servers.append(
            MCPServerConfig(
                name=entry,
                server_path=server_script,
                env=manifest.env,
            )
        )
        logger.debug(f"Discovered MCP server: {entry}")

    logger.info(f"Discovered {len(servers)} MCP servers from {base_path}")
    return servers


def _interpolate_env_value(value: str) -> str | None:
    """Interpolate ${VAR} patterns in an env value.

    Args:
        value: String potentially containing ${VAR} patterns.

    Returns:
        Interpolated string, or None if any variable is unset.
    """
    unresolved: list[str] = []

    def replacer(match: re.Match) -> str:
        var_name = match.group(1)
        env_val = os.getenv(var_name)
        if env_val is None:
            unresolved.append(var_name)
            return str(match.group(0))
        return env_val

    result = re.sub(r"\$\{(\w+)\}", replacer, value)

    if unresolved:
        return None

    return result


def build_stdio_server_entry(
    config: MCPServerConfig,
    *,
    app_root: str | None = None,
    repo: str | None = None,
    worktree_path: str | None = None,
) -> dict[str, Any] | None:
    """Build a stdio MCP server entry for ClaudeAgentOptions.

    Merges default env vars with mcp.json env vars, interpolates
    ${VAR} patterns from the environment, and skips the server if
    any required variables are unresolved.

    Args:
        config: Discovered server config.
        app_root: Application root path (default: /app or project root).
        repo: Repository identifier for GITHUB_REPOSITORY.
        worktree_path: Worktree path for REPO_PATH.

    Returns:
        Server entry dict, or None if env vars are unresolved.
    """
    if not app_root:
        app_root = _resolve_base_path().rsplit(os.sep, 1)[0] or "/app"

    env: dict[str, str] = {
        "PYTHONPATH": app_root,
    }

    if repo:
        env["GITHUB_REPOSITORY"] = repo

    if worktree_path:
        env["REPO_PATH"] = worktree_path

    for key, value in config.env.items():
        interpolated = _interpolate_env_value(value)
        if interpolated is None:
            logger.info(
                f"Skipping MCP server {config.name}: " f"unresolved env var in {key}"
            )
            return None
        env[key] = interpolated

    return {
        "type": "stdio",
        "command": "python3",
        "args": [config.server_path],
        "env": env,
    }


def discover_http_servers() -> list[tuple[str, dict[str, Any]]]:
    """Discover HTTP MCP servers from mcp_servers/http.json.

    Reads the config file, interpolates ${VAR} patterns, and
    skips servers with unresolved variables.

    Returns:
        List of (name, server_entry) tuples.
    """
    base_path = _resolve_base_path()
    http_config_path = os.path.join(base_path, "http.json")

    if not os.path.exists(http_config_path):
        return []

    try:
        with open(http_config_path, encoding="utf-8") as f:
            config = json.load(f)
    except Exception as e:
        logger.warning(f"Failed to read http.json: {e}")
        return []

    servers: list[tuple[str, dict[str, Any]]] = []

    for name, server_config in config.items():
        entry = _build_http_entry(name, server_config)
        if entry is not None:
            servers.append((name, entry))

    return servers


def _build_http_entry(name: str, config: dict[str, Any]) -> dict[str, Any] | None:
    """Build an HTTP MCP server entry, interpolating env vars.

    Args:
        name: Server name.
        config: Server config dict (url, headers, etc.).

    Returns:
        Server entry dict, or None if env vars are unresolved.
    """
    entry: dict[str, Any] = {"type": "http"}

    if "url" in config:
        url = _interpolate_env_value(config["url"])
        if url is None:
            logger.info(f"Skipping HTTP MCP server {name}: unresolved env in url")
            return None
        entry["url"] = url

    if "headers" in config:
        headers: dict[str, str] = {}
        for key, value in config["headers"].items():
            interpolated = _interpolate_env_value(value)
            if interpolated is None:
                logger.info(
                    f"Skipping HTTP MCP server {name}: "
                    f"unresolved env in header {key}"
                )
                return None
            headers[key] = interpolated
        entry["headers"] = headers

    return entry
