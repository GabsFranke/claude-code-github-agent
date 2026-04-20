# Session Streaming ("Remote Control")

## Context

The bot runs Claude Agent SDK sessions autonomously — once a job starts, there's no way to observe or interact with it. The user wants to "jump into" running sessions: watch in real-time, inject messages, and approve/deny tool calls.

Claude Code's native `--remote-control` requires claude.ai OAuth and is incompatible with API-key-based SDK usage. Instead, we build our own streaming layer using patterns from [Anthropic's official demos](https://github.com/anthropics/claude-agent-sdk-demos): Redis pub/sub bridge, WebSocket server, and a `canUseTool` round-trip via `asyncio.Future`.

This plan is independent of the persistent conversations plan (`plans/persistent-conversations.md`) but complements it — streaming handles real-time observation, persistent conversations handles cross-comment session resume.

## Architecture

```
sandbox_worker              Redis                      session_proxy            Browser
    |                         |                             |                      |
    |-- PUBLISH msg:{token} ->|                             |                      |
    |                         |---- push subscribers ------>|                      |
    |                         |                             |-- WebSocket push --->|
    |                         |                             |                      |
    |                         |<-- SUBSCRIBE ctl:{token} ---|                      |
    |                         |<---- WS message -------------|<-- user clicks ok --|
    |<-- resolve Future ------|                             |                      |
```

**New service: `session_proxy`** — lightweight FastAPI + WebSocket that bridges SDK messages to browsers.

**Two Redis pub/sub channels per session:**
- `session:msg:{token}` — SDK messages from sandbox_worker → session_proxy (broadcast)
- `session:ctl:{token}` — Control messages from session_proxy → sandbox_worker (approve/deny, inject)

**Session token** is a UUID generated per job — the URL IS the auth token (128-bit random, unguessable).

## Phases

### Phase 1: Streaming Bridge (core infrastructure)

**New file: `shared/session_stream.py`**

```python
class SessionStreamBridge:
    """Publishes SDK messages to Redis pub/sub for cross-process streaming."""

    async def publish_init(self, repo, issue_number, workflow): ...
    async def publish_assistant(self, content: list): ...
    async def publish_tool_use(self, tool_name, tool_input, tool_use_id): ...
    async def publish_tool_result(self, tool_use_id, output, is_error): ...
    async def publish_result(self, num_turns, duration_ms, is_error, session_id): ...
    async def close(self): ...


class ControlChannel:
    """Subscribes to control messages and resolves pending Futures."""

    async def start(self): ...
    async def wait_for_approval(self, tool_use_id, timeout=30.0) -> bool: ...
    async def stop(self): ...
```

Redis message format (on `session:msg:{token}`):

```json
{"type": "assistant_message", "data": {"content": [...]}, "ts": "..."}
{"type": "tool_use", "data": {"tool_name": "Bash", "tool_use_id": "...", "input": {...}}, "ts": "..."}
{"type": "tool_result", "data": {"tool_use_id": "...", "output": "...", "is_error": false}, "ts": "..."}
{"type": "result", "data": {"num_turns": 5, "session_id": "..."}, "ts": "..."}
```

Control message format (on `session:ctl:{token}`):

```json
{"type": "tool_approval", "tool_use_id": "...", "approved": true}
{"type": "inject_message", "text": "Also check the auth middleware"}
```

**New file: `shared/session_store.py`**

```python
class StreamingSessionStore:
    """Manages streaming session metadata in Redis."""

    async def create_session(self, token, repo, issue_number, workflow): ...
    async def get_session(self, token) -> dict | None: ...
    async def has_subscribers(self, token) -> bool: ...
    async def set_completed(self, token): ...
```

Redis key: `session:stream:{token}` (TTL: 24h), subscriber count: `session:subscribers:{token}`.

### Phase 2: Streaming SDK Executor

**Modified: `shared/sdk_executor.py`**

Add `execute_sdk_streaming()` — mirrors `execute_sdk()` but publishes each message as it arrives:

```python
async def execute_sdk_streaming(
    prompt: str,
    options: ClaudeAgentOptions,
    session_token: str,
    redis_url: str,
    redis_password: str | None = None,
    timeout: int | None = None,
    max_retries: int = 1,
    retry_base_delay: float = 5.0,
) -> dict:
```

Key differences from `execute_sdk()`:
- Sets `include_partial_messages=True` on options
- Creates `SessionStreamBridge` + `ControlChannel`
- In `receive_messages()` loop, calls `bridge.publish_*()` for each message type
- `can_use_tool` callback: if subscribers connected, publish question and await Future (30s timeout → auto-approve); if no subscribers, auto-approve immediately
- Returns same dict structure so post-processing works unchanged

**Modified: `shared/sdk_factory.py`**

Add to `SDKOptionsBuilder`:

```python
def with_streaming(self, session_token: str, redis_url: str, redis_password: str | None): ...
def with_can_use_tool(self, callback): ...
```

`build()` passes `can_use_tool` and `include_partial_messages=True` when streaming is configured.

### Phase 3: Session Proxy Service

**New directory: `services/session_proxy/`**

**New file: `services/session_proxy/main.py`**

FastAPI app with:
- `GET /health` — health check
- `GET /session/{token}` — serves the React SPA (fallback to index.html for client-side routing)
- `WS /ws/session/{token}` — WebSocket endpoint
- `GET /api/session/{token}` — REST endpoint for session metadata (used by React on load)

WebSocket handler:
1. Validates token exists in Redis (`StreamingSessionStore.get_session()`)
2. Increments subscriber count
3. Subscribes to `session:msg:{token}` via Redis pub/sub
4. Runs two concurrent tasks:
   - `_redis_to_ws()`: forward Redis messages → WebSocket
   - `_ws_to_redis()`: forward WebSocket messages → Redis `session:ctl:{token}`
5. On disconnect, decrements subscriber count

**New directory: `services/session_proxy/client/`**

React + Vite app. Key structure:

```
services/session_proxy/client/
  package.json
  vite.config.ts
  index.html
  src/
    main.tsx
    App.tsx
    hooks/
      useSessionSocket.ts    # WebSocket connection + auto-reconnect
    components/
      MessageLog.tsx          # Scrollable message list
      AssistantMessage.tsx    # Renders assistant text blocks (markdown)
      ToolCall.tsx            # Collapsible tool use + result
      ToolApproval.tsx        # Approve/deny buttons with countdown
      StatusBar.tsx           # Running/completed/error indicator
      SessionHeader.tsx       # Repo, issue, workflow info
    types/
      messages.ts             # WebSocket message type definitions
```

UI features:
- Dark terminal-like theme
- Auto-connects WebSocket on load, auto-reconnects on disconnect
- Renders assistant messages as markdown (using `react-markdown`)
- Tool calls shown as collapsible sections (tool name + truncated input)
- Tool results in `<pre>` blocks with syntax highlighting
- Status bar: running / completed / error with turn count and duration
- Approve/deny buttons for tool calls (appears when `tool_approval: true`, countdown timer)
- Responsive layout — works on desktop and mobile

**Build**: The React app builds to `services/session_proxy/client/dist/` via `vite build`. The FastAPI server serves the built assets via `StaticFiles`. The Dockerfile runs the Vite build as a build stage.

**New file: `services/session_proxy/Dockerfile`**

Multi-stage build:
1. Node.js stage: install deps, run `vite build`
2. Python stage: install Python deps, copy built React assets, run FastAPI

### Phase 4: Sandbox Worker Integration

**Modified: `services/sandbox_executor/sandbox_worker.py`**

In `process_job()`, branch around SDK invocation (line ~354):

```python
if job_data.get("streaming_enabled"):
    result = await execute_sdk_streaming(
        prompt=job_data["prompt"],
        options=builder.build(),
        session_token=job_data["session_token"],
        redis_url=redis_url,
        redis_password=redis_password,
        max_retries=SDK_MAX_RETRIES,
        retry_base_delay=SDK_RETRY_BASE_DELAY,
    )
else:
    result = await execute_sdk(
        prompt=job_data["prompt"],
        options=builder.build(),
        max_retries=SDK_MAX_RETRIES,
        retry_base_delay=SDK_RETRY_BASE_DELAY,
    )
```

All post-processing (flush_pending_post_jobs, job completion, cleanup) remains unchanged.

### Phase 5: GitHub Integration

**Modified: `services/agent_worker/processors/request_processor.py`**

When building a job, if workflow has `streaming.enabled: true`:
1. Generate UUID session token
2. Store in job data: `streaming_enabled`, `session_token`, `session_proxy_url`
3. Post GitHub comment immediately (before sandbox starts):

```
> Agent session started. [Watch live](http://host:10001/session/{token})
```

### Phase 6: Workflow Config

**Modified: `workflows/engine.py`**

Add `StreamingConfig` Pydantic model:

```python
class StreamingConfig(BaseModel):
    enabled: bool = False
    auto_approve_timeout: int = 30  # seconds before auto-approving tools
    tool_approval: bool = False     # require human approval for tool calls
```

Add `streaming: StreamingConfig` to `WorkflowConfig`.

**Modified: `workflows.yaml`**

```yaml
workflows:
  review-pr:
    triggers: ...
    streaming:
      enabled: true
      tool_approval: false  # observe-only to start
```

### Phase 7: Docker Compose

**Modified: `docker-compose.yml`**

Add session_proxy service:

```yaml
  session_proxy:
    build:
      context: .
      dockerfile: services/session_proxy/Dockerfile
    ports:
      - "10001:8080"
    environment:
      - REDIS_URL=redis://redis:6379
      - REDIS_PASSWORD=${REDIS_PASSWORD:-myredissecret}
    depends_on:
      redis:
        condition: service_healthy
```

Add `SESSION_PROXY_URL` to `sandbox_worker` and `worker` environments.

## Files Summary

### New files
| File | Purpose |
|------|---------|
| `shared/session_stream.py` | SessionStreamBridge + ControlChannel (Redis pub/sub bridge) |
| `shared/session_store.py` | StreamingSessionStore (Redis session metadata) |
| `services/session_proxy/__init__.py` | Package init |
| `services/session_proxy/main.py` | FastAPI + WebSocket server + REST endpoints |
| `services/session_proxy/Dockerfile` | Multi-stage build (Node for React, Python for FastAPI) |
| `services/session_proxy/requirements.txt` | fastapi, uvicorn, redis |
| `services/session_proxy/client/` | React + Vite app (see structure in Phase 3) |
| `services/session_proxy/client/package.json` | React, vite, react-markdown, dependencies |

### Modified files
| File | Change |
|------|---------|
| `shared/sdk_executor.py` | Add `execute_sdk_streaming()` |
| `shared/sdk_factory.py` | Add `with_streaming()`, `with_can_use_tool()` |
| `services/sandbox_executor/sandbox_worker.py` | Branch on streaming_enabled |
| `services/agent_worker/processors/request_processor.py` | Generate tokens, post GitHub comment |
| `workflows/engine.py` | Add `StreamingConfig` to `WorkflowConfig` |
| `workflows.yaml` | Add `streaming` sections |
| `docker-compose.yml` | Add `session_proxy` service |

## Implementation Order

1. `shared/session_stream.py` — bridge classes (pure infrastructure)
2. `shared/session_store.py` — Redis metadata store
3. `shared/sdk_executor.py` — add `execute_sdk_streaming()`
4. `shared/sdk_factory.py` — add streaming builder methods
5. `services/session_proxy/` — WebSocket server + React UI
6. `services/sandbox_executor/sandbox_worker.py` — wire streaming in
7. `services/agent_worker/processors/request_processor.py` — tokens + GitHub comments
8. `workflows/engine.py` + `workflows.yaml` — config model
9. `docker-compose.yml` — new service

Steps 1-5 deliver working "watch the bot in real-time". Steps 6-9 wire it into the job pipeline.

## Relationship to Persistent Conversations Plan

These are complementary features:
- **Streaming** (this plan): real-time observation + interaction during a running session
- **Persistent conversations** (existing plan): resume sessions across GitHub comments

They share some concepts (session IDs, Redis metadata) but don't depend on each other. The SDK `session_id` (for resume) is separate from the streaming `session_token` (for WebSocket URL). They can be implemented independently and merged later.

## Verification

1. **Unit test** `SessionStreamBridge`: mock Redis, verify message publishing
2. **Integration test**: start session_proxy, publish messages to Redis, verify WebSocket receives them
3. **End-to-end test**: configure a workflow with `streaming.enabled: true`, trigger via GitHub comment, verify:
   - GitHub comment posted with session URL
   - Opening URL shows the streaming UI
   - Messages appear in real-time in the UI
   - Session completes and UI shows final status
4. **Tool approval test**: set `tool_approval: true`, verify approve/deny buttons appear and control the session

## Risks

| Risk | Mitigation |
|------|-----------|
| Redis pub/sub lost if session_proxy down | Buffer recent messages in Redis list (60s), replay on connect |
| canUseTool blocks forever | 30s timeout auto-approves; ControlChannel resolves all Futures on cleanup |
| Performance overhead | Messages are small JSON; Redis pub/sub handles this well natively |
| WebSocket disconnects | Client auto-reconnects; server replays buffered messages |
