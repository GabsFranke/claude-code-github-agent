"""MCP HTTP Proxy using FastAPI.

This service acts as an HTTP SSE transport wrapper around our stdio MCP servers
so they can be accessed over HTTP by the host machine and other network clients.
"""

import asyncio
import logging
import os
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

MCP_SERVERS_DIR = os.getenv("MCP_SERVERS_DIR", "/app/mcp_servers")


@app.get("/mcp/{server_name}/sse")
async def mcp_sse(server_name: str, request: Request):
    """Establish an SSE connection to an MCP server.

    Spawns the underlying stdio Python process and routes stdin/stdout.
    Query parameters are passed to the subprocess as environment variables.
    """
    server_script = os.path.join(MCP_SERVERS_DIR, server_name, "server.py")
    if not os.path.isfile(server_script):
        raise HTTPException(status_code=404, detail="Server not found")

    # Read query parameters to pass as environment variables
    env = os.environ.copy()
    for k, v in request.query_params.items():
        env[k.upper()] = v

    session_id = str(uuid.uuid4())

    try:
        process = await asyncio.create_subprocess_exec(
            "python3",
            server_script,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
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

            # Read stdout line by line (JSON-RPC) and yield as message events
            while True:
                if process.stdout.at_eof():
                    break
                line = await process.stdout.readline()
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
