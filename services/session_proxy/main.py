"""Session Proxy — WebSocket bridge between Redis pub/sub and browser clients.

Each agent session is identified by owner/repo/type/number/workflow. The URL
/session/{owner}/{repo}/{type}/{number}/{workflow} is human-readable and predictable.

This service:

  1. Validates the session exists in Redis (StreamingSessionStore)
  2. Sends full message history so the browser shows the entire conversation
  3. Opens a Redis pub/sub subscription and forwards messages over WebSocket
  4. Forwards browser WebSocket messages back to Redis
  5. When a user sends a message to a completed session, creates a resume job

GET  /health                                              — health check
GET  /api/resolve/{owner}/{repo}/{type}/{number}/{wf}     — resolve session metadata
WS   /ws/{owner}/{repo}/{type}/{number}/{wf}              — WebSocket streaming endpoint
GET  /session/{owner}/{repo}/{type}/{number}/{wf}         — serves React SPA (catch-all)
GET  /                                                     — redirect/info endpoint
"""

import asyncio
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from services.session_proxy.transcript_loader import (
    find_transcript,
    find_transcript_by_repo,
    load_transcript_history,
    load_transcript_meta,
)
from shared.constants import (
    AUTO_APPROVE_TIMEOUT,
    CTL_CHANNEL,
    DEFAULT_SESSION_TTL_HOURS,
    JOB_DATA_PREFIX,
    JOB_STATUS_PREFIX,
    JOB_TTL_SECONDS,
    MSG_CHANNEL,
    PENDING_JOB_QUEUE,
    SESSION_HISTORY_KEY,
)
from shared.logging_utils import setup_logging
from shared.streaming_session import StreamingSessionStore
from shared.utils import build_session_url, url_segment_to_thread_type

# Configure logging
setup_logging(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Redis connection
# ---------------------------------------------------------------------------

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379")
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD") or None

_redis: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    if _redis is None:
        raise RuntimeError("Redis not initialized")
    return _redis


# ---------------------------------------------------------------------------
# URL parsing helpers
# ---------------------------------------------------------------------------


def _parse_session_path(
    owner: str, repo: str, thread_type_segment: str, number: str, workflow: str
) -> dict:
    """Parse human-readable URL segments into session lookup keys.

    Returns a dict with repo (owner/repo format), issue_number (int),
    workflow, and thread_type (internal format: "pr", "issue", "discussion").
    """
    return {
        "repo": f"{owner}/{repo}",
        "issue_number": int(number),
        "workflow": workflow,
        "thread_type": url_segment_to_thread_type(thread_type_segment),
    }


# ---------------------------------------------------------------------------
# Job queue helpers (direct Redis — avoids importing the full JobQueue class)
# ---------------------------------------------------------------------------


async def _create_job(job_data: dict) -> str:
    """Create a job directly in Redis without importing JobQueue."""
    r = get_redis()
    job_id = str(uuid.uuid4())
    await r.setex(f"{JOB_DATA_PREFIX}{job_id}", JOB_TTL_SECONDS, json.dumps(job_data))
    await r.setex(f"{JOB_STATUS_PREFIX}{job_id}", JOB_TTL_SECONDS, "pending")
    await r.rpush(PENDING_JOB_QUEUE, job_id)  # type: ignore[misc]
    logger.info(f"[Job] Created resume job {job_id}")
    return job_id


# ---------------------------------------------------------------------------
# Resolve endpoint — returns session metadata for human-readable URLs
# ---------------------------------------------------------------------------


async def _resolve_session(
    owner: str, repo: str, thread_type_segment: str, number: str, workflow: str
) -> tuple[str | None, dict | None]:
    """Look up a streaming session by human-readable path.

    Returns (token, session) or (None, None) if not found.
    """
    store = StreamingSessionStore(get_redis())
    parsed = _parse_session_path(owner, repo, thread_type_segment, number, workflow)
    token = await store.find_session(
        repo=parsed["repo"],
        issue_number=parsed["issue_number"],
        workflow=parsed["workflow"],
        thread_type=parsed["thread_type"],
    )
    if not token:
        return None, None
    session = await store.get_session(token)
    if not session:
        return None, None
    return token, session


# ---------------------------------------------------------------------------
# Resume handler — creates a job when user messages a completed session
# ---------------------------------------------------------------------------


async def _handle_resume_message(token: str, content: str, session: dict) -> None:
    """Create a new job to resume a completed session.

    Called when a user sends a message to a session that is not running.
    The session_proxy publishes the user message to the session channel,
    sets the session back to running, and creates a resume job.
    """
    store = StreamingSessionStore(get_redis())
    r = get_redis()

    # Parse conversation_config to get the session's TTL
    conversation_config_json = session.get("conversation_config", "")
    conversation_config: dict = {}
    if conversation_config_json:
        try:
            conversation_config = json.loads(conversation_config_json)
        except Exception:
            conversation_config = {
                "persist": True,
                "ttl_hours": DEFAULT_SESSION_TTL_HOURS,
            }

    # Ensure persist is True for resumed sessions
    conversation_config.setdefault("persist", True)

    # Derive TTL from conversation_config (matches the workflow config chain)
    ttl_hours = conversation_config.get("ttl_hours", DEFAULT_SESSION_TTL_HOURS)
    ttl_seconds = ttl_hours * 3600

    # Set session back to running with the correct TTL
    await store.set_running(token, ttl_seconds=ttl_seconds)

    # Increment run count
    await store.increment_run_count(token)

    # Publish user message to the session channel
    channel_name = MSG_CHANNEL.format(token)
    now = _now_iso()

    # User message
    user_msg = json.dumps(
        {"type": "user_message", "data": {"content": content}, "ts": now}
    )
    await r.publish(channel_name, user_msg)
    # Also push to Redis history (short-lived fallback)
    await r.rpush(SESSION_HISTORY_KEY.format(token), user_msg)  # type: ignore[misc]

    # Note: We do NOT publish run_start here. The sandbox_worker publishes
    # run_start when it starts processing the job, which avoids a duplicate
    # event if both session_proxy and sandbox_worker emit it.

    # Build job data from session metadata
    repo = session.get("repo", "")
    issue_number = int(session.get("issue_number", "0"))
    workflow = session.get("workflow", "generic")
    session_id = session.get("session_id", "")
    thread_type = session.get("thread_type", "issue")
    ref = session.get("ref", "main")
    user = session.get("user", "remote-control")
    installation_id = session.get("installation_id", "")

    job_data = {
        "repo": repo,
        "issue_number": issue_number,
        "prompt": content,
        "user": user,
        "workflow_name": workflow,
        "ref": ref,
        "session_mode": "resume" if session_id else "new",
        "session_id": session_id or None,
        "session_token": token,
        "streaming_enabled": True,
        "tool_approval_enabled": False,
        "auto_approve_timeout": AUTO_APPROVE_TIMEOUT,
        "installation_id": installation_id,
        "thread_type": thread_type,
        "thread_id": str(issue_number),
        "conversation_config": conversation_config,
        "user_query": content,
        "event_data": {"event_type": "remote_control"},
    }

    job_id = await _create_job(job_data)
    logger.info(
        f"[Resume] Created job {job_id} for session {token[:8]}... "
        f"(mode={job_data['session_mode']})"
    )


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _redis
    _redis = aioredis.from_url(
        REDIS_URL,
        password=REDIS_PASSWORD,
        decode_responses=False,  # Keep raw bytes for pub/sub
    )
    logger.info(f"Connected to Redis at {REDIS_URL}")
    yield
    if _redis:
        await _redis.close()


app = FastAPI(title="Session Proxy", lifespan=lifespan)

# ---------------------------------------------------------------------------
# Static files (React build)
# ---------------------------------------------------------------------------

CLIENT_DIST = Path(__file__).parent / "client" / "dist"

# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------


@app.get("/health")
async def health():
    try:
        result = get_redis().ping()
        # Handle both sync and async ping (driver version dependent)
        if hasattr(result, "__await__"):
            await result
        return {"status": "ok", "redis": "connected"}
    except Exception as e:
        return JSONResponse(
            status_code=503, content={"status": "error", "redis": str(e)}
        )


@app.get("/api/resolve/{owner}/{repo}/{thread_type_segment}/{number}/{workflow}")
async def resolve_session(
    owner: str, repo: str, thread_type_segment: str, number: str, workflow: str
):
    """Resolve a human-readable session path to session metadata.

    Returns the session token and metadata if a session exists,
    or 404 if no session has been created yet (the SPA will poll).

    Falls back to transcript-based lookup when the Redis session has
    expired but the transcript file still exists on disk.
    """
    try:
        issue_number = int(number)
    except ValueError:
        raise HTTPException(status_code=400, detail="Issue number must be an integer")

    full_repo = f"{owner}/{repo}"
    thread_type = url_segment_to_thread_type(thread_type_segment)

    # Try Redis first (active or recent sessions)
    token, session = await _resolve_session(
        owner, repo, thread_type_segment, number, workflow
    )
    if token is not None:
        return {
            "status": "found",
            "token": token,
            "session": session,
        }

    # Fallback: search transcript files for a matching session
    transcript_path = find_transcript_by_repo(full_repo, issue_number, workflow)
    if transcript_path is not None:
        # Merge sidecar metadata (installation_id, ref, etc.) for re-invoke
        meta = load_transcript_meta(transcript_path)
        session_proxy_url = os.getenv("SESSION_PROXY_URL", "").strip()
        full_url = build_session_url(
            session_proxy_url, owner, repo, thread_type, issue_number, workflow
        )
        return {
            "status": "found",
            "token": f"transcript:{transcript_path.stem}",
            "session": {
                "token": f"transcript:{transcript_path.stem}",
                "repo": full_repo,
                "issue_number": str(issue_number),
                "workflow": workflow,
                "thread_type": thread_type,
                "status": "completed",
                "session_proxy_url": full_url,
                **meta,
            },
        }

    return JSONResponse(
        status_code=404,
        content={
            "status": "pending",
            "message": "No active session for this issue/workflow yet.",
        },
    )


# ---------------------------------------------------------------------------
# History loading (transcript-first, Redis fallback)
# ---------------------------------------------------------------------------


async def _load_history_async(
    session: dict, store: StreamingSessionStore, token: str
) -> list[dict]:
    """Load conversation history — transcript file first, Redis fallback.

    Transcript files (SDK JSONL on the shared ~/.claude volume) are the
    primary history source. They persist across container restarts and
    contain the full conversation. Redis history is the fallback for
    sessions that are still running (transcript may not exist yet).
    """
    # Try transcript-based loading first (sync — reads from disk)
    transcript_path = session.get("transcript_path", "")
    if transcript_path:
        p = Path(transcript_path)
        if p.exists():
            messages = load_transcript_history(p)
            if messages:
                logger.info(
                    f"[WS] Loaded {len(messages)} messages from transcript {p.name}"
                )
                return messages

    # Try finding transcript by session_id
    session_id = session.get("session_id", "")
    if session_id:
        found_path = find_transcript(session_id)
        if found_path:
            messages = load_transcript_history(found_path)
            if messages:
                await store.update_transcript_path(token, str(found_path))
                logger.info(
                    f"[WS] Loaded {len(messages)} messages from discovered transcript {found_path.name}"
                )
                return messages

    # Fall back to Redis history
    redis_history = await store.get_history(token)
    logger.debug(
        f"[WS] Using Redis history for session {token[:8]}... "
        f"({len(redis_history)} messages)"
    )
    return redis_history


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------


async def _resolve_token_from_path(
    owner: str, repo: str, thread_type_segment: str, number: str, workflow: str
) -> tuple[str | None, dict | None]:
    """Resolve a human-readable path to a session token and metadata.

    Checks Redis first, then falls back to transcript-based lookup.
    Returns (token, session) or (None, None) if not found.
    """
    token, session = await _resolve_session(
        owner, repo, thread_type_segment, number, workflow
    )
    if token is not None:
        return token, session

    # Fallback: search transcript files
    full_repo = f"{owner}/{repo}"
    thread_type = url_segment_to_thread_type(thread_type_segment)
    try:
        issue_number = int(number)
    except ValueError:
        return None, None

    transcript_path = find_transcript_by_repo(full_repo, issue_number, workflow)
    if transcript_path is not None:
        t_token = f"transcript:{transcript_path.stem}"
        meta = load_transcript_meta(transcript_path)
        session_proxy_url = os.getenv("SESSION_PROXY_URL", "").strip()
        full_url = build_session_url(
            session_proxy_url, owner, repo, thread_type, issue_number, workflow
        )
        return t_token, {
            "token": t_token,
            "repo": full_repo,
            "issue_number": str(issue_number),
            "workflow": workflow,
            "thread_type": thread_type,
            "status": "completed",
            "session_proxy_url": full_url,
            **meta,
        }

    return None, None


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
              or resume job (when session is completed)
    Redis msg channel → Browser (SDK messages in real-time)

    The WebSocket stays open until the browser disconnects — across agent
    runs and session completions — so the user can always send a follow-up
    message to re-invoke the agent.
    """
    # Accept first — Starlette requires accept() before any send/close,
    # otherwise it returns 403 Forbidden to the client.
    await websocket.accept()

    # Validate issue number
    try:
        int(number)
    except ValueError:
        await websocket.close(code=4400, reason="Issue number must be an integer")
        return

    # Resolve path to token (Redis first, then transcript fallback)
    token, session = await _resolve_token_from_path(
        owner, repo, thread_type_segment, number, workflow
    )

    if token is None or session is None:
        await websocket.close(code=4404, reason="Session not found")
        return

    store = StreamingSessionStore(get_redis())
    history_sent = False

    # ── Transcript path: send history from file, then rehydrate on message ──
    if token.startswith("transcript:"):
        session_id = token[len("transcript:") :]
        transcript_path = find_transcript(session_id)
        if transcript_path is None:
            await websocket.close(code=4404, reason="Transcript file not found")
            return

        # Send transcript history
        history = load_transcript_history(transcript_path)
        if history:
            for msg in history:
                try:
                    await websocket.send_text(json.dumps(msg))
                except Exception:
                    break
            logger.info(
                f"[WS] Sent {len(history)} transcript messages for "
                f"completed session {session_id[:8]}..."
            )

        # Send session metadata + completion signals
        await websocket.send_text(
            json.dumps({"type": "session_meta", "data": session, "ts": _now_iso()})
        )
        await websocket.send_text(
            json.dumps(
                {
                    "type": "result",
                    "data": {
                        "num_turns": 0,
                        "duration_ms": 0,
                        "is_error": False,
                        "session_id": session_id,
                        "subtype": None,
                    },
                    "ts": _now_iso(),
                }
            )
        )
        await websocket.send_text(
            json.dumps({"type": "session_closed", "data": {}, "ts": _now_iso()})
        )
        logger.info(
            f"[WS] Transcript replay complete for {session_id[:8]}..., "
            f"waiting for user message to resume"
        )

        # Wait for the first user message, then rehydrate into a live session
        new_token = await _rehydrate_transcript_session(websocket, token, session)
        if new_token is None:
            return  # Browser disconnected

        # Transition to live mode with the rehydrated session
        token = new_token
        session = await store.get_session(token)
        if session is None:
            logger.error(f"[WS] Session {new_token[:8]}... not found after rehydration")
            await websocket.close(code=4500, reason="Session creation failed")
            return

        # Send updated session_meta so browser knows we're now live
        await websocket.send_text(
            json.dumps({"type": "session_meta", "data": session, "ts": _now_iso()})
        )
        history_sent = True

    # ── Shared live-mode setup (both paths converge here) ──
    await store.increment_subscribers(token)

    if not history_sent:
        # Normal path: load history from Redis/transcript
        history = await _load_history_async(session, store, token)
        if history:
            for msg in history:
                try:
                    await websocket.send_text(json.dumps(msg))
                except Exception:
                    break
            logger.info(f"[WS] Sent {len(history)} history messages to new client")

        await websocket.send_text(
            json.dumps({"type": "session_meta", "data": session, "ts": _now_iso()})
        )

    logger.info(
        f"[WS] Client connected to session "
        f"{owner}/{repo}/{thread_type_segment}/{number}/{workflow} (token={token[:8]}...)"
    )

    # ── Shared live mode: bidirectional streaming until browser disconnects ──
    session_status = session.get("status", "unknown") if session else "unknown"

    try:
        await asyncio.gather(
            _redis_to_ws(websocket, token),
            _ws_to_redis(websocket, token, session_status),
        )
    except WebSocketDisconnect:
        logger.info(f"[WS] Client disconnected from session {token[:8]}...")
    except Exception as e:
        logger.error(f"[WS] Error in session {token[:8]}...: {e}")
    finally:
        await store.decrement_subscribers(token)
        logger.info(f"[WS] Subscriber count decremented for {token[:8]}...")


async def _redis_to_ws(websocket: WebSocket, token: str) -> None:
    """Forward Redis pub/sub messages to the WebSocket client.

    The connection stays open across agent runs — when a session
    completes, the status changes to "completed" but the WebSocket
    persists so the user can send a message to re-invoke.
    """
    channel = MSG_CHANNEL.format(token)

    pubsub = get_redis().pubsub()
    await pubsub.subscribe(channel)
    logger.info(f"[WS] Subscribed to Redis channel {channel}")

    try:
        async for raw in pubsub.listen():
            if raw["type"] != "message":
                continue
            data = raw["data"]
            text = data.decode() if isinstance(data, bytes) else data
            await websocket.send_text(text)
            # Don't close the WebSocket on session completion —
            # the connection persists for future agent runs.
    finally:
        await pubsub.unsubscribe(channel)
        await pubsub.close()


async def _rehydrate_transcript_session(
    websocket: WebSocket, token: str, session: dict
) -> str | None:
    """Wait for the first user message on a transcript-only session and rehydrate.

    Creates a new Redis session and resume job, then returns the new token
    so the caller can transition to live mode. Returns None if the browser
    disconnected before sending a message.
    """
    session_id = token[len("transcript:") :] if token.startswith("transcript:") else ""
    repo = session.get("repo", "")
    issue_number_str = session.get("issue_number", "0")
    issue_number = int(issue_number_str) if issue_number_str.isdigit() else 0
    workflow = session.get("workflow", "generic")
    thread_type = session.get("thread_type", "issue")

    async for text in _receive_text(websocket):
        try:
            msg = json.loads(text)

            if msg.get("type") != "inject_message":
                # Ignore non-message types during rehydration (no agent to receive them)
                continue

            content = msg.get("content", "").strip()
            if not content:
                continue

            r = get_redis()
            store = StreamingSessionStore(r)

            # Build full session URL for the rehydrated session
            session_proxy_url = os.getenv("SESSION_PROXY_URL", "").strip()
            owner, _, repo_name = repo.partition("/")
            rehydrated_session_url = build_session_url(
                session_proxy_url, owner, repo_name, thread_type, issue_number, workflow
            )

            # Create a new Redis session so the sandbox_worker can find it
            new_token = str(uuid.uuid4())
            await store.create_session(
                token=new_token,
                repo=repo,
                issue_number=issue_number,
                workflow=workflow,
                session_proxy_url=rehydrated_session_url,
                session_id=session_id,
                installation_id=session.get("installation_id", ""),
                ref=session.get("ref", "main"),
                user=session.get("user", "remote-control"),
                thread_type=thread_type,
            )

            # Build resume job data
            conversation_config: dict = {"persist": True}
            try:
                raw = session.get("conversation_config", "")
                if raw:
                    conversation_config = json.loads(raw)
            except Exception:
                pass
            conversation_config.setdefault("persist", True)

            job_data = {
                "repo": repo,
                "issue_number": issue_number,
                "prompt": content,
                "user": session.get("user", "remote-control"),
                "workflow_name": workflow,
                "ref": session.get("ref", "main"),
                "session_mode": "resume" if session_id else "new",
                "session_id": session_id or None,
                "session_token": new_token,
                "streaming_enabled": True,
                "tool_approval_enabled": False,
                "auto_approve_timeout": AUTO_APPROVE_TIMEOUT,
                "installation_id": session.get("installation_id", ""),
                "thread_type": session.get("thread_type", "issue"),
                "thread_id": str(issue_number),
                "conversation_config": conversation_config,
                "user_query": content,
                "event_data": {"event_type": "remote_control"},
            }

            job_id = await _create_job(job_data)
            await store.set_running(new_token)
            await store.increment_run_count(new_token)

            logger.info(
                f"[WS] Resume job {job_id} created for transcript session "
                f"{session_id[:8]}... -> new token {new_token[:8]}..."
            )

            # Show the user's message in the chat
            await websocket.send_text(
                json.dumps(
                    {
                        "type": "user_message",
                        "data": {"content": content},
                        "ts": _now_iso(),
                    }
                )
            )

            return new_token

        except json.JSONDecodeError:
            logger.warning(f"[WS] Received invalid JSON from client: {text[:100]}")
        except Exception as e:
            logger.warning(f"[WS] Failed to handle transcript message: {e}")

    return None  # WebSocket disconnected


async def _ws_to_redis(websocket: WebSocket, token: str, session_status: str) -> None:
    """Forward browser WebSocket messages to Redis control channel.

    For inject_message type: if the session is completed/error, creates
    a resume job instead of publishing to the control channel (since
    no worker is listening on a completed session).

    When the session is running, publishes the user message to the MSG
    channel so the browser sees it echoed back (the sandbox_worker's
    ControlChannel also does this, but publishing here ensures the
    message appears even if no worker is actively listening).
    """
    channel = CTL_CHANNEL.format(token)
    msg_channel = MSG_CHANNEL.format(token)
    redis = get_redis()
    store = StreamingSessionStore(redis)

    async for text in _receive_text(websocket):
        try:
            msg = json.loads(text)

            # Handle inject_message based on session status
            if msg.get("type") == "inject_message":
                content = msg.get("content", "").strip()
                if not content:
                    continue

                # Check current session status
                session = await store.get_session(token)
                current_status = (
                    session.get("status", "unknown") if session else "unknown"
                )

                if current_status in ("completed", "error") and session:
                    # Session not running — create a resume job
                    await _handle_resume_message(token, content, session)
                    continue

                # Session is running — echo the user message to the
                # MSG channel so the browser displays it, then forward
                # to the control channel for the sandbox_worker.
                user_msg = json.dumps(
                    {
                        "type": "user_message",
                        "data": {"content": content},
                        "ts": _now_iso(),
                    }
                )
                await redis.publish(msg_channel, user_msg)

            # Forward to Redis control channel
            await redis.publish(channel, text)
        except json.JSONDecodeError:
            logger.warning(f"[WS] Received invalid JSON from client: {text[:100]}")
        except Exception as e:
            logger.warning(f"[WS] Failed to forward control message: {e}")


async def _receive_text(websocket: WebSocket):
    """Async generator that yields text messages from a WebSocket."""
    try:
        while True:
            msg = await websocket.receive_text()
            yield msg
    except WebSocketDisconnect:
        return
    except Exception:
        return


def _now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------------
# SPA catch-all (must be last — after all API routes)
# ---------------------------------------------------------------------------

if CLIENT_DIST.exists():
    app.mount("/assets", StaticFiles(directory=CLIENT_DIST / "assets"), name="assets")

    @app.get("/session/{owner}/{repo}/{thread_type_segment}/{number}/{workflow}")
    async def serve_spa(
        owner: str,
        repo: str,
        thread_type_segment: str,
        number: str,
        workflow: str,
    ):
        return FileResponse(CLIENT_DIST / "index.html")

    @app.get("/")
    async def serve_root():
        return JSONResponse(
            {
                "service": "Session Proxy",
                "message": (
                    "Open /session/{owner}/{repo}/{type}/{issue_number}/{workflow} "
                    "to view a streaming session."
                ),
            }
        )

else:
    # Dev mode — frontend not built yet
    @app.get("/session/{owner}/{repo}/{thread_type_segment}/{number}/{workflow}")
    async def serve_spa_dev(
        owner: str,
        repo: str,
        thread_type_segment: str,
        number: str,
        workflow: str,
    ):
        return JSONResponse(
            {"message": "Frontend not built. Run: cd client && npm run build"}
        )

    @app.get("/")
    async def serve_root_dev():
        return JSONResponse({"message": "Session Proxy API. Frontend not built."})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)
