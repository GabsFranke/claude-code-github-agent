"""Session Service — FastAPI application.

Provides REST endpoints for unified session management via ``SessionStore``.
This service is the successor to ``session_proxy`` and will replace it in Wave 6.

Endpoints:
    GET /health                       — health check with Redis connectivity probe
    POST /api/sessions                — create persistent session
    GET  /api/sessions/{token}        — get session details by token
    DELETE /api/sessions/{token}     — close session
    GET  /api/sessions?repo=X        — list sessions for a repository
    PUT  /api/sessions/{token}/expire — set session TTL
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from collections.abc import Awaitable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal, cast

import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.websockets import WebSocketState

from shared.constants import (
    CTL_CHANNEL,
    DEFAULT_SESSION_TTL_HOURS,
    DEFAULT_SESSION_TTL_SECONDS,
    MSG_CHANNEL,
    _now_iso,
    history_key,
    sanitize_repo_key,
)
from shared.logging_utils import setup_logging
from shared.session_store import UnifiedSessionInfo
from shared.utils import url_segment_to_thread_type

from .config import SessionServiceConfig
from .resume import handle_resume
from .store import get_store, init_store
from .transcript import load_transcript_history

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

config = SessionServiceConfig()
setup_logging(level=config.log_level)

_redis: aioredis.Redis | None = None


# ---------------------------------------------------------------------------
# Redis helpers
# ---------------------------------------------------------------------------


def get_redis() -> aioredis.Redis:
    """Return the global Redis client singleton."""
    if _redis is None:
        raise RuntimeError("Redis not initialised — call init_store() first")
    return _redis


# ---------------------------------------------------------------------------
# Lifespan — connect Redis on start, disconnect on shutdown
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan: connect Redis on startup, close on shutdown."""
    global _redis

    _redis = aioredis.from_url(
        config.redis_url,
        password=config.redis_password,
        decode_responses=True,
        socket_timeout=10,
        socket_connect_timeout=5,
        retry_on_timeout=True,
    )
    # Verify connectivity
    try:
        await cast(Awaitable[bool], _redis.ping())
        logger.info("Connected to Redis at %s", config.redis_url)
    except Exception as exc:
        logger.warning(
            "Redis not reachable at %s — starting without Redis: %s",
            config.redis_url,
            exc,
        )

    # Initialise the store singleton with the connected client
    init_store(_redis)

    yield

    # Shutdown: close Redis connection
    if _redis:
        await _redis.close()
        logger.info("Redis connection closed")


# ---------------------------------------------------------------------------
# Token encode / decode
# ---------------------------------------------------------------------------


def encode_session_token(
    repo: str, thread_type: str, thread_id: str, workflow: str
) -> str:
    """Encode composite session key into a URL-safe opaque token."""
    payload = json.dumps([repo, thread_type, str(thread_id), workflow])
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def decode_session_token(token: str) -> tuple[str, str, str, str]:
    """Decode a URL-safe token back into (repo, thread_type, thread_id, workflow).

    Raises ``HTTPException(400)`` if the token is malformed.
    """
    # Add padding back if needed (base64url strips = padding)
    padding = 4 - len(token) % 4
    if padding != 4:
        token += "=" * padding
    try:
        payload = base64.urlsafe_b64decode(token.encode()).decode()
        parts: list = json.loads(payload)
        if len(parts) != 4 or not all(isinstance(p, str) for p in parts):
            raise ValueError("token must decode to exactly 4 strings")
        return parts[0], parts[1], parts[2], parts[3]
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid session token: {exc}",
        ) from exc


# ---------------------------------------------------------------------------
# Pydantic request / response models
# ---------------------------------------------------------------------------


class CreateSessionRequest(BaseModel):
    """Request body for ``POST /api/sessions``."""

    repo: str = Field(..., description="GitHub repository (owner/repo)")
    thread_type: Literal["pr", "issue", "discussion"] = Field(
        ..., description="Thread type"
    )
    thread_id: str = Field(..., description="Issue/PR/discussion number")
    workflow: str = Field(..., description="Workflow name")
    session_id: str = Field(..., description="SDK session identifier")
    worktree_path: str = Field(..., description="Local worktree path")
    ref: str = Field(..., description="Git ref (branch/tag)")
    # Optional fields
    turn_count: int = Field(default=0, description="Initial turn count")
    summary: str | None = Field(default=None, description="Conversation summary")
    ttl_hours: int = Field(
        default=DEFAULT_SESSION_TTL_HOURS, description="Session TTL in hours"
    )
    streaming_token: str | None = Field(
        default=None, description="Streaming session token"
    )
    installation_id: str = Field(default="", description="GitHub App installation ID")
    initial_query: str = Field(default="", description="Triggering comment text")
    conversation_config: str = Field(
        default="", description="JSON-encoded conversation settings"
    )
    transcript_path: str = Field(default="", description="Transcript file path")
    run_count: int = Field(default=1, description="Initial run count")
    session_proxy_url: str = Field(default="", description="Public session_proxy URL")
    issue_number: str = Field(default="", description="Issue number (string)")
    user: str = Field(default="", description="GitHub username")


class ExpireSessionRequest(BaseModel):
    """Request body for ``PUT /api/sessions/{token}/expire``."""

    ttl_hours: int = Field(
        default=DEFAULT_SESSION_TTL_HOURS, description="New TTL in hours"
    )


def _session_to_response(
    session: UnifiedSessionInfo,
    repo: str,
    thread_type: str,
    thread_id: str,
    workflow: str,
) -> dict:
    """Convert a ``UnifiedSessionInfo`` to a JSON-safe dict with an added ``token`` field."""
    data = session.model_dump(mode="json")
    data["token"] = encode_session_token(repo, thread_type, thread_id, workflow)
    return data  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Session Service",
    version="0.1.0",
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# CORS — allow localhost dev servers and the SPA origin
# ---------------------------------------------------------------------------

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.origins_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# Warn if CORS is wide open
if config.allowed_origins == "*":
    logger.warning(
        "[Security] ALLOWED_ORIGINS='*' — CORS is wide open. "
        "Set ALLOWED_ORIGINS to a comma-separated list of trusted origins."
    )


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------


@app.get("/health")
async def health():
    """Health check with Redis connectivity probe.

    Returns 200 with ``{"status": "ok"}`` when healthy, 503 when Redis
    is unreachable.
    """
    try:
        redis_client = get_redis()
        await cast(Awaitable[bool], redis_client.ping())
        return {"status": "ok", "redis": "connected"}
    except RuntimeError:
        # Redis not initialised yet — service is starting
        return JSONResponse(
            status_code=503,
            content={"status": "error", "redis": "not_initialised"},
        )
    except Exception as exc:
        return JSONResponse(
            status_code=503,
            content={"status": "error", "redis": str(exc)},
        )


# ---------------------------------------------------------------------------
# Session CRUD endpoints (Task 8)
# ---------------------------------------------------------------------------


@app.post("/api/sessions", status_code=201)
async def create_session(request: CreateSessionRequest):
    """Create a new persistent session.

    Returns 201 with the full session data (all 21 ``UnifiedSessionInfo`` fields)
    plus a ``token`` field encoding the composite session key.
    """
    store = get_store().store

    await store.save_session(
        repo=request.repo,
        thread_type=request.thread_type,
        thread_id=request.thread_id,
        workflow=request.workflow,
        session_id=request.session_id,
        worktree_path=request.worktree_path,
        ref=request.ref,
        turn_count=request.turn_count,
        summary=request.summary,
        ttl_hours=request.ttl_hours,
        streaming_token=request.streaming_token,
        installation_id=request.installation_id,
        initial_query=request.initial_query,
        conversation_config=request.conversation_config,
        transcript_path=request.transcript_path,
        run_count=request.run_count,
        session_proxy_url=request.session_proxy_url,
        issue_number=request.issue_number,
        user=request.user,
    )

    session = await store.get_session(
        request.repo, request.thread_type, request.thread_id, request.workflow
    )
    if session is None:
        raise HTTPException(
            status_code=500, detail="Session was saved but could not be retrieved"
        )

    return _session_to_response(
        session, request.repo, request.thread_type, request.thread_id, request.workflow
    )


@app.get("/api/sessions/{token}")
async def get_session_by_token(token: str):
    """Retrieve a session by its composite token.

    Returns 200 with full session data, or 404 with an error JSON body.
    """
    repo, thread_type, thread_id, workflow = decode_session_token(token)

    store = get_store().store
    session = await store.get_session(repo, thread_type, thread_id, workflow)

    if session is None:
        return JSONResponse(
            status_code=404,
            content={
                "error": "Session not found",
                "detail": f"No session for {repo}/{thread_type}/{thread_id}/{workflow}",
            },
        )

    return _session_to_response(session, repo, thread_type, thread_id, workflow)


@app.delete("/api/sessions/{token}")
async def delete_session_by_token(token: str):
    """Delete a session and all associated keys.

    Returns 200 on success with a confirmation status.
    """
    repo, thread_type, thread_id, workflow = decode_session_token(token)

    store = get_store().store
    await store.close_session(repo, thread_type, thread_id, workflow)

    return JSONResponse(
        status_code=200,
        content={
            "status": "deleted",
            "detail": f"Session {repo}/{thread_type}/{thread_id}/{workflow} closed",
        },
    )


@app.get("/api/sessions")
async def list_sessions(
    repo: str = Query(..., description="Repository (owner/repo)"),  # noqa: B008
):
    """List all active sessions for a repository.

    Returns 200 with a JSON array of session objects (empty array if none found).
    """
    store = get_store().store
    sessions = await store.list_sessions(repo)

    return [
        _session_to_response(s, s.repo, s.thread_type, s.thread_id, s.workflow_name)
        for s in sessions
    ]


@app.put("/api/sessions/{token}/expire")
async def expire_session_by_token(
    token: str, request: ExpireSessionRequest | None = None
):
    """Set a new TTL on a session (shortens or extends expiry).

    The ``ExpireSessionRequest`` body is optional — a default TTL is used
    when no body is provided.  Returns 200 with the applied TTL.
    """
    repo, thread_type, thread_id, workflow = decode_session_token(token)

    store = get_store().store
    ttl_hours = request.ttl_hours if request else DEFAULT_SESSION_TTL_HOURS
    await store.expire_session(
        repo, thread_type, thread_id, workflow, ttl_hours=ttl_hours
    )

    return {
        "status": "expired",
        "ttl_hours": ttl_hours,
        "detail": f"TTL set to {ttl_hours}h for {repo}/{thread_type}/{thread_id}/{workflow}",
    }


# ---------------------------------------------------------------------------
# Token resolution endpoint (Task 9)
# ---------------------------------------------------------------------------


def _find_transcript_token(repo: str, issue_number: int, workflow: str) -> str | None:
    """Scan ~/.claude/projects/ for transcripts matching repo/issue/workflow.

    Returns pseudo-token ``transcript:{stem}`` or ``None`` if no matching
    transcript file is found on disk.
    """
    claude_home = Path(os.getenv("CLAUDE_HOME", str(Path.home() / ".claude")))
    projects_dir = claude_home / "projects"

    if not projects_dir.exists():
        return None

    repo_segment = sanitize_repo_key(repo)
    candidates: list[tuple[float, Path]] = []

    for project_dir in projects_dir.iterdir():
        if not project_dir.is_dir():
            continue
        if repo_segment not in project_dir.name:
            continue
        for jsonl_file in project_dir.glob("*.jsonl"):
            candidates.append((jsonl_file.stat().st_mtime, jsonl_file))

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0], reverse=True)
    return f"transcript:{candidates[0][1].stem}"


def _thread_type_candidates(thread_type: str) -> list[str]:
    """Requested thread type first, then the remaining types.

    A PR thread can be stored under ``"issue"`` (GitHub delivers PR comments as
    ``issue_comment`` events), so a ``/pull/`` URL must still find it — and the
    reverse for links minted before the classification was fixed.
    """
    others = [t for t in ("pr", "issue", "discussion") if t != thread_type]
    return [thread_type, *others]


async def _find_session_any_thread_type(
    full_repo: str, issue_number: int, workflow: str, thread_type: str
) -> tuple[str | None, UnifiedSessionInfo | None]:
    """Resolve a session token across thread types, preferring an active one."""
    store = get_store().store
    candidates = _thread_type_candidates(thread_type)

    for finder in (store.find_active_session, store.find_session):
        for candidate in candidates:
            token = await finder(
                full_repo, issue_number, workflow, thread_type=candidate
            )
            if not token:
                continue
            session = await store.get_streaming_session(token)
            if session:
                return token, session

    return None, None


@app.get("/api/resolve/{owner}/{repo}/{thread_type_segment}/{number}/{workflow}")
async def resolve_session(
    owner: str,
    repo: str,
    thread_type_segment: str,
    number: str,
    workflow: str,
):
    """Resolve a human-readable session path to a token and metadata.

    Returns a ``ResolveResponse`` matching the SPA contract:
        ``{"status": "found", "token": ..., "session": {...}}`` on success,
        ``{"status": "pending", "message": "..."}`` when nothing is found yet.

    Resolution chain:
        1. find_active_session → running/active token (all thread types)
        2. find_session → any session token, completed/error (all thread types)
        3. transcript scan → transcript:{stem} pseudo-token
        4. not found → ``{"status": "pending"}`` (404 previously)
    """
    try:
        issue_number = int(number)
    except ValueError:
        raise HTTPException(
            status_code=400, detail="Issue number must be an integer"
        ) from None

    thread_type = url_segment_to_thread_type(thread_type_segment)
    full_repo = f"{owner}/{repo}"

    def _make_response(token: str, info: UnifiedSessionInfo) -> dict:
        """Build a ResolveResponse with ``status: "found"``."""
        return {
            "status": "found",
            "token": token,
            "session": {
                "token": token,
                "repo": info.repo,
                "issue_number": info.issue_number,
                "workflow": info.workflow_name,
                "thread_type": info.thread_type,
                "status": info.status.value,
                "session_proxy_url": info.session_proxy_url,
            },
        }

    # 1-2. Active session first, then any session (completed, error),
    #      each tried across thread types.
    token, session = await _find_session_any_thread_type(
        full_repo, issue_number, workflow, thread_type
    )
    if token and session:
        return _make_response(token, session)

    # 3. Transcript fallback — scan filesystem
    pseudo_token = _find_transcript_token(full_repo, issue_number, workflow)
    if pseudo_token:
        return {
            "status": "found",
            "token": pseudo_token,
            "session": {
                "token": pseudo_token,
                "repo": full_repo,
                "issue_number": str(issue_number),
                "workflow": workflow,
                "thread_type": thread_type,
                "status": "completed",
                "session_proxy_url": "",
            },
        }

    # 4. Nothing found — return pending to let the SPA poll
    return {"status": "pending", "message": "Waiting for session…"}


# ---------------------------------------------------------------------------
# WebSocket endpoint (Task 13)
# ---------------------------------------------------------------------------


def _validate_ws_origin(websocket: WebSocket) -> bool:
    """CSWSH prevention — reject disallowed WebSocket origins.

    Returns ``True`` if the origin is allowed, closes the WebSocket
    with code 4403 and returns ``False`` otherwise.
    """
    if config.allowed_origins == "*":
        return True

    origin = websocket.headers.get("origin", "")
    if not origin:
        return True  # no origin header → allow (e.g. native apps)

    from urllib.parse import urlparse

    origin_list = config.origins_list
    if origin in origin_list:
        return True

    # Fallback to localhost when no explicit origins are configured
    if not origin_list:
        hostname = urlparse(origin).hostname or ""
        if hostname in ("localhost", "127.0.0.1", "::1"):
            return True

    logger.warning("[WS] Rejected WebSocket from disallowed origin: %s", origin)
    return False


async def _resolve_ws_token(
    owner: str, repo: str, thread_type_segment: str, number: str, workflow: str
) -> tuple[str | None, UnifiedSessionInfo | None]:
    """Resolve human-readable path to a session token and metadata.

    Uses SessionStore for lookup, with transcript fallback.
    Returns (token, session) or (None, None).
    """
    full_repo = f"{owner}/{repo}"
    issue_number = int(number)
    thread_type = url_segment_to_thread_type(thread_type_segment)

    token, session = await _find_session_any_thread_type(
        full_repo, issue_number, workflow, thread_type
    )
    if token and session:
        return token, session

    pseudo_token = _find_transcript_token(full_repo, issue_number, workflow)
    if pseudo_token:
        return pseudo_token, None

    return None, None


async def _receive_text(websocket: WebSocket):
    """Async generator yielding text messages from a WebSocket."""
    try:
        while True:
            msg = await websocket.receive_text()
            yield msg
    except WebSocketDisconnect:
        return
    except Exception as e:
        logger.warning("[WS] Error reading from WebSocket: %s", e)
        return


async def _redis_to_ws(websocket: WebSocket, token: str) -> None:
    """Forward Redis pub/sub messages to the WebSocket client.

    Reconnects with exponential backoff on pub/sub connection drops.
    """
    channel = MSG_CHANNEL.format(token)
    max_retries = 5
    backoff = [1, 2, 4, 8, 16]

    for attempt in range(max_retries + 1):
        if websocket.client_state != WebSocketState.CONNECTED:
            return
        pubsub = get_redis().pubsub()
        try:
            await pubsub.subscribe(channel)
            if attempt == 0:
                logger.info("[WS] Subscribed to Redis channel %s", channel)
            else:
                logger.info(
                    "[WS] Re-subscribed to %s (attempt %d/%d)",
                    channel,
                    attempt,
                    max_retries,
                )

            async for raw in pubsub.listen():
                if raw["type"] != "message":
                    continue
                data = raw["data"]
                text = data.decode() if isinstance(data, bytes) else data
                if websocket.client_state == WebSocketState.CONNECTED:
                    await websocket.send_text(text)

            if attempt >= max_retries:
                logger.error("[WS] Redis pub/sub closed, max retries exceeded")
                raise ConnectionError("Redis pub/sub connection lost")
            delay = backoff[attempt]
            logger.warning(
                "[WS] Redis pub/sub closed (attempt %d/%d), reconnecting in %ds...",
                attempt + 1,
                max_retries,
                delay,
            )
            await asyncio.sleep(delay)

        except Exception as e:
            if attempt >= max_retries:
                logger.error(
                    "[WS] Redis pub/sub failed after %d retries: %s", max_retries, e
                )
                raise
            delay = backoff[attempt]
            logger.warning(
                "[WS] Redis pub/sub error (attempt %d/%d): %s. Reconnecting in %ds...",
                attempt + 1,
                max_retries,
                e,
                delay,
            )
            await asyncio.sleep(delay)
        finally:
            try:
                await pubsub.unsubscribe(channel)
                await pubsub.close()
            except Exception:
                pass


async def _ws_to_redis(websocket: WebSocket, token: str) -> None:
    """Forward browser WebSocket messages to Redis control channel.

    For inject_message: publishes user message to the msg channel (so the
    browser sees it echoed), persists to history, and forwards to the ctl
    channel for the sandbox_worker.
    """
    ctl_channel = CTL_CHANNEL.format(token)
    msg_channel = MSG_CHANNEL.format(token)
    redis_client = get_redis()

    async for text in _receive_text(websocket):
        try:
            msg = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("[WS] Invalid JSON from client: %s", text[:100])
            continue

        try:
            if msg.get("type") == "inject_message":
                content = (msg.get("content") or "").strip()
                if not content:
                    continue

                store = get_store().store
                session = await store.get_streaming_session(token)
                if session and str(session.status) in ("completed", "error"):
                    await handle_resume(token, content, store, redis_client)
                    continue

                user_msg = json.dumps(
                    {
                        "type": "user_message",
                        "data": {"content": content},
                        "ts": _now_iso(),
                    }
                )
                await redis_client.publish(msg_channel, user_msg)

                hist_key = history_key(token)
                await cast(Awaitable[int], redis_client.rpush(hist_key, user_msg))
                await redis_client.expire(hist_key, DEFAULT_SESSION_TTL_SECONDS)

            await redis_client.publish(ctl_channel, text)
        except Exception as e:
            logger.warning("[WS] Failed to forward control message: %s", e)


# pylint: disable=too-many-nested-blocks
def _transcript_to_spa_messages(entries: list[dict]) -> list[dict]:
    """Convert raw transcript JSONL entries to SPA-compatible messages.

    Pairs ``assistant(tool_use)`` entries with their subsequent
    ``user(tool_result)`` entries by matching ``id`` ↔ ``tool_use_id``
    so the SPA can render tool calls with their results inline.
    """
    tool_results: dict[str, dict] = {}
    for entry in entries:
        if entry.get("type") != "user":
            continue
        content = entry.get("message", {}).get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if block.get("type") != "tool_result":
                continue
            tuid = block.get("tool_use_id", "")
            if tuid:
                result_text = block.get("content", "")
                if isinstance(result_text, list):
                    result_text = "\n".join(
                        b.get("text", "")
                        for b in result_text
                        if isinstance(b, dict) and b.get("text")
                    )
                tool_results[tuid] = {
                    "result": str(result_text) if result_text else "",
                    "is_error": bool(block.get("is_error", False)),
                }

    messages: list[dict] = []
    for entry in entries:
        t = entry.get("type", "")
        ts = entry.get("timestamp", _now_iso())
        if t == "user":
            content = entry.get("message", {}).get("content", "")
            has_tool_result = isinstance(content, list) and any(
                b.get("type") == "tool_result" for b in content
            )
            if has_tool_result:
                continue
            text = _extract_user_text(content)
            if text:
                messages.append(
                    {"type": "user_message", "data": {"content": text}, "ts": ts}
                )
        elif t == "assistant":
            content = entry.get("message", {}).get("content", [])
            if isinstance(content, list):
                blocks: list[dict] = []
                for block in content:
                    bt = block.get("type", "")
                    if bt == "text" and block.get("text"):
                        blocks.append({"type": "text", "text": block["text"]})
                    elif bt == "tool_use":
                        tu_block: dict = {
                            "type": "tool_use",
                            "name": block.get("name", ""),
                            "input": block.get("input", {}),
                        }
                        tuid = block.get("id", "")
                        if tuid and tuid in tool_results:
                            tu_block["result"] = tool_results[tuid]["result"]
                            tu_block["isError"] = tool_results[tuid]["is_error"]
                        blocks.append(tu_block)
                if blocks:
                    messages.append(
                        {
                            "type": "assistant_message",
                            "data": {"content": blocks},
                            "ts": ts,
                        }
                    )
        elif t == "result":
            messages.append(
                {
                    "type": "result",
                    "data": {
                        "num_turns": 0,
                        "duration_ms": 0,
                        "is_error": entry.get("is_error", False),
                        "session_id": entry.get("session_id"),
                        "subtype": entry.get("subtype"),
                    },
                    "ts": ts,
                }
            )
    return messages


def _extract_user_text(content: str | list) -> str:
    """Extract displayable text from a user message's content field."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text" and block.get("text"):
                    parts.append(block["text"])
                elif block.get("type") == "tool_result":
                    result = block.get("content", "")
                    if isinstance(result, str) and len(result) > 500:
                        parts.append(result[:500] + "...")
                    elif isinstance(result, str):
                        parts.append(result)
        return "\n".join(parts)
    return ""  # type: ignore[unreachable]


@app.websocket("/ws/{owner}/{repo}/{thread_type_segment}/{number}/{workflow}")
async def websocket_session(
    websocket: WebSocket,
    owner: str,
    repo: str,
    thread_type_segment: str,
    number: str,
    workflow: str,
):
    """Bidirectional WebSocket bridge using human-readable paths.

    Browser → Redis ctl channel (tool approvals, inject messages)
    Redis msg channel → Browser (SDK messages in real-time)

    The WebSocket stays open until the browser disconnects — across agent
    runs and session completions.
    """
    await websocket.accept()

    if not _validate_ws_origin(websocket):
        await websocket.close(code=4403, reason="Origin not allowed")
        return

    try:
        int(number)
    except ValueError:
        await websocket.close(code=4400, reason="Issue number must be an integer")
        return

    token, session = await _resolve_ws_token(
        owner, repo, thread_type_segment, number, workflow
    )

    if token is None:
        await websocket.close(code=4404, reason="Session not found")
        return

    store = get_store().store

    # Send transcript history (full conversation from disk)
    if session is not None and session.transcript_path:
        transcript_path = Path(session.transcript_path)
        if transcript_path.exists():
            entries = load_transcript_history(transcript_path)
            for msg in _transcript_to_spa_messages(entries):
                try:
                    await websocket.send_text(json.dumps(msg))
                except Exception:
                    break

    # Send Redis history (recent inject_messages not yet in transcript)
    history = await store.get_history(token)
    if history:
        for msg in history:
            try:
                await websocket.send_text(json.dumps(msg))
            except Exception:
                break

    # Send session_meta if available
    if session is not None:
        sensitive = {"installation_id"}
        safe_meta = {
            k: v
            for k, v in session.model_dump(mode="json").items()
            if k not in sensitive
        }
        safe_meta["workflow"] = safe_meta.get("workflow_name", "")
        await websocket.send_text(
            json.dumps({"type": "session_meta", "data": safe_meta, "ts": _now_iso()})
        )

    await store.increment_subscribers(token)

    logger.info(
        "[WS] Client connected to session %s/%s/%s/%s/%s (token=%s...)",
        owner,
        repo,
        thread_type_segment,
        number,
        workflow,
        token[:8],
    )

    try:
        results = await asyncio.gather(
            _redis_to_ws(websocket, token),
            _ws_to_redis(websocket, token),
            return_exceptions=True,
        )
        for r in results:
            if isinstance(r, BaseException) and not isinstance(
                r, (WebSocketDisconnect, asyncio.CancelledError)
            ):
                logger.error("[WS] Stream direction failed for %s...: %s", token[:8], r)
    except (WebSocketDisconnect, asyncio.CancelledError, RuntimeError):
        logger.info("[WS] Client disconnected from session %s...", token[:8])
    except Exception as e:
        logger.error("[WS] Error in session %s...: %s", token[:8], e)
    finally:
        await store.decrement_subscribers(token)
        logger.info("[WS] Subscriber count decremented for %s...", token[:8])


# ---------------------------------------------------------------------------
# SPA static file serving (must be last — after all API/WS routes)
# ---------------------------------------------------------------------------

CLIENT_DIST = Path(__file__).parent / "client" / "dist"

if CLIENT_DIST.exists():
    app.mount(
        "/assets",
        StaticFiles(directory=CLIENT_DIST / "assets"),
        name="assets",
    )

    @app.get("/{path:path}")
    async def serve_spa(path: str):
        # Exclude API, WebSocket, and health paths — let registered
        # handlers respond instead of catching everything with index.html.
        if path.startswith(("api/", "ws/")) or path == "health":
            raise HTTPException(status_code=404)
        return FileResponse(CLIENT_DIST / "index.html")

    @app.get("/")
    async def serve_root():
        return FileResponse(CLIENT_DIST / "index.html")

else:

    @app.get("/")
    async def serve_root_dev():
        return JSONResponse({"message": "Session Service — React frontend not built."})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=config.port,
        log_level=config.log_level.lower(),
    )
