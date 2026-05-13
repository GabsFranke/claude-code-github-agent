"""MCP HTTP Proxy using FastAPI.

This service acts as an HTTP SSE transport wrapper around our stdio MCP servers
so they can be accessed over HTTP by the host machine and other network clients.
"""

import asyncio
import logging
import os
import re
import uuid

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

app = FastAPI(title="MCP HTTP Proxy")

# Map of session_id -> asyncio.subprocess.Process
sessions: dict[str, asyncio.subprocess.Process] = {}

MAX_MCP_SESSIONS = 50

MCP_SERVERS_DIR = os.getenv("MCP_SERVERS_DIR", "/app/mcp_servers")

# Security: allowed prefixes for query parameters injected as env vars.
# Prevents overwriting sensitive env vars like PATH, HOME, LD_LIBRARY_PATH, etc.
DEFAULT_ALLOWED_ENV_PREFIXES = (
    "REPO_",
    "GITHUB_",
    "MCP_",
    "SURREALDB_",
    "GEMINI_",
    "PYTHONPATH",
)
_ALLOWED_ENV_PREFIXES: tuple[str, ...] | None = None


def _get_allowed_env_prefixes() -> tuple[str, ...]:
    """Return the cached allowed env var prefixes, computing once on first use."""
    global _ALLOWED_ENV_PREFIXES
    if _ALLOWED_ENV_PREFIXES is None:
        custom = os.getenv("MCP_ALLOWED_ENV_PREFIXES")
        if custom:
            _ALLOWED_ENV_PREFIXES = tuple(
                p.strip() for p in custom.split(",") if p.strip()
            )
        else:
            _ALLOWED_ENV_PREFIXES = DEFAULT_ALLOWED_ENV_PREFIXES
    return _ALLOWED_ENV_PREFIXES


# Safe base environment for subprocesses
_SAFE_ENV_VARS = {"PATH", "HOME", "LANG", "TERM", "PWD", "SHELL", "USER"}


def _build_subprocess_env() -> dict[str, str]:
    """Build a minimal environment for MCP server subprocesses.

    Only includes safe system vars and whitelisted prefixes.
    Sensitive vars like ANTHROPIC_API_KEY, REDIS_PASSWORD, etc. are excluded.
    """
    env: dict[str, str] = {}
    allowed_prefixes = _get_allowed_env_prefixes()
    for key, value in os.environ.items():
        if key in _SAFE_ENV_VARS:
            env[key] = value
        elif any(key.startswith(prefix) for prefix in allowed_prefixes):
            env[key] = value
    return env


def _is_safe_server_name(name: str) -> bool:
    """Validate that a server_name is safe for filesystem path construction.

    Rejects names containing path traversal sequences, directory separators,
    null bytes, or any character outside the allowed set (alphanumeric,
    hyphens, underscores).
    """
    if not name:
        return False
    # Reject null bytes, path separators, and traversal sequences
    if "\x00" in name or "/" in name or "\\" in name or ".." in name:
        return False
    # Only allow alphanumeric, hyphens, and underscores
    return bool(re.fullmatch(r"[A-Za-z0-9_-]+", name))


def _resolve_and_validate_server_script(server_name: str) -> str:
    """Resolve the server script path and verify it stays within MCP_SERVERS_DIR.

    Returns the resolved script path if safe, raises HTTPException otherwise.
    """
    server_script = os.path.join(MCP_SERVERS_DIR, server_name, "server.py")
    resolved_script = os.path.realpath(server_script)
    resolved_base = os.path.realpath(MCP_SERVERS_DIR)

    # Verify the resolved path is still within the base directory
    if not resolved_script.startswith(resolved_base + os.sep):
        raise HTTPException(
            status_code=400, detail="Invalid server name: path escape detected"
        )

    # Verify the resolved path ends with the expected filename
    if not resolved_script.endswith(os.path.join(server_name, "server.py")):
        raise HTTPException(
            status_code=400, detail="Invalid server name: unexpected path structure"
        )

    if not os.path.isfile(resolved_script):
        raise HTTPException(status_code=404, detail="Server not found")

    return resolved_script


@app.get("/health")
async def health():
    return {"status": "ok", "active_sessions": len(sessions)}


@app.get("/mcp/{server_name}/sse")
async def mcp_sse(server_name: str, request: Request):
    """Establish an SSE connection to an MCP server.

    Spawns the underlying stdio Python process and routes stdin/stdout.
    Query parameters are passed to the subprocess as environment variables
    (only those matching the allowed prefix whitelist).
    """
    if len(sessions) >= MAX_MCP_SESSIONS:
        raise HTTPException(status_code=429, detail="Too many MCP sessions active")

    if not _is_safe_server_name(server_name):
        raise HTTPException(
            status_code=400,
            detail="Invalid server name: must be alphanumeric with hyphens/underscores only",
        )

    server_script = _resolve_and_validate_server_script(server_name)

    # Read query parameters to pass as environment variables.
    # Security: only inject params whose uppercased key matches an allowed prefix.
    env = _build_subprocess_env()
    allowed_prefixes = _get_allowed_env_prefixes()
    for k, v in request.query_params.items():
        key_upper = k.upper()
        if any(key_upper.startswith(prefix) for prefix in allowed_prefixes):
            env[key_upper] = v
        else:
            logger.warning(
                f"Rejected query parameter '{k}': prefix not in allowed list "
                f"{allowed_prefixes}"
            )

    session_id = str(uuid.uuid4())

    try:
        process = await asyncio.create_subprocess_exec(
            "python3",
            server_script,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            limit=16 * 1024 * 1024,  # 16 MB — MCP responses can be large
        )
        sessions[session_id] = process
        logger.info(f"Started {server_name} session {session_id} (PID {process.pid})")
    except Exception as e:
        logger.error(f"Failed to start {server_name}: {e}")
        raise HTTPException(status_code=500, detail="Failed to start server process")

    # Start a background task to log stderr from the subprocess
    async def log_stderr():
        if not process.stderr:
            return
        while True:
            if process.stderr.at_eof():
                break
            line = await process.stderr.readline()
            if not line:
                break
            logger.warning(f"[{server_name} stderr]: {line.decode('utf-8').rstrip()}")

    asyncio.create_task(log_stderr())

    async def event_generator():
        try:
            # MCP Spec: First event must be "endpoint" with the POST URI
            endpoint_url = f"/mcp/message?session_id={session_id}"
            yield f"event: endpoint\ndata: {endpoint_url}\n\n"

            if not process.stdout:
                return

            # Read stdout line by line (JSON-RPC) and yield as message events.
            while True:
                if process.stdout.at_eof():
                    break
                try:
                    line = await process.stdout.readline()
                except ValueError:
                    logger.error(
                        "readline exceeded buffer for session %s, closing session",
                        session_id,
                    )
                    return
                if not line:
                    break

                decoded = line.decode("utf-8").strip()
                if decoded:
                    yield f"event: message\ndata: {decoded}\n\n"

        except asyncio.CancelledError:
            logger.info(f"Client disconnected from session {session_id}")
        finally:
            if process.returncode is None:
                try:
                    process.terminate()
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except Exception:
                    process.kill()
            sessions.pop(session_id, None)
            logger.info(f"Cleaned up session {session_id}")

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.post("/mcp/message")
async def mcp_message(session_id: str, request: Request):
    """Receive JSON-RPC messages and pipe them to the MCP server's stdin."""
    if session_id not in sessions:
        raise HTTPException(status_code=404, detail="Session not found")

    process = sessions[session_id]
    if process.returncode is not None:
        sessions.pop(session_id, None)
        raise HTTPException(status_code=400, detail="Session terminated")

    body = await request.body()
    try:
        if not process.stdin:
            raise Exception("Process stdin is not available")

        process.stdin.write(body + b"\n")
        await process.stdin.drain()
        return Response(status_code=202)
    except Exception as e:
        logger.error(f"Failed to write to session {session_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to write to process")
