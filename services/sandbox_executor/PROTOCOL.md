# Sandbox Worker — Session Context Protocol

Boundary contract between the **agent worker** (job producer) and the **sandbox executor** (job consumer / `JobProcessor`).

--------------------------------------------------------------------------------

## 1. Worker Receives (IN)

Five session-context fields arrive in `job_data`. The worker reads
them — it never creates, allocates, or decides them:

| Field | Type | Source | Meaning |
|---|---|---|---|
| `session_id` | `str \| None` | `request_processor.py:626` | SDK session ID from a prior run. Non‑null for `resume` / `fork` / `continue` modes; `None` for `new`. The worker passes this to the SDK’s resume API and reports it back on completion. |
| `session_mode` | `str` | `request_processor.py:625` | Session lifecycle mode: `"new"`, `"resume"`, `"fork"`, or `"continue"`. Controls whether the worker skips repo‑setup (`"resume"`/`"continue"`) and how the SDK is initialised. |
| `session_token` | `str \| None` | `request_processor.py:639` | Streaming session token that links this job to a live `StreamingSessionStore` record. Used for remote‑control signalling (publish progress, receive interrupts, report errors). `None` when streaming is disabled. |
| `conversation_summary` | `str \| None` | `request_processor.py:636` | LLM‑generated summary of the prior conversation. Injected into the SDK prompt on resume when `ConversationConfig.summary_fallback` is enabled. |
| `conversation_config.persist` | `bool` | `request_processor.py:629` | Whether to persist the worktree across job boundaries. The worker derives `self.persist_session` from this field. When `True`: worktree is saved, a `WorktreeLock` is acquired, and session metadata is written to `SessionStore`. When `False`: worktree is removed during cleanup. |

### Additional job_data fields (for completeness)

The fields above are the **session‑context protocol** subset. The worker
also receives many operational fields — see
`request_processor.py:600‑640` for the full dict. Notable non‑session
fields include `repo`, `ref`, `issue_number`, `github_token`, `prompt`,
`claude_md`, `memory_index`, `event_data`, `user`, `workflow_name`,
`user_query`, and `parent_span_id`.

--------------------------------------------------------------------------------

## 2. Worker Manages (responsible)

Three areas are owned entirely by the sandbox executor:

### 2.1 Worktree lifecycle

- Acquires a `WorktreeLock` for persistent sessions.
- Calls `wait_for_repo_sync` to ensure the bare repo is up‑to‑date.
- Creates or reuses a git worktree via `reuse_or_create_worktree`.
- Configures git credentials, author identity, and submodules.
- Runs repo‑setup commands (dependency installs, etc.) for `"new"` sessions;
  skips them for `"resume"` / `"continue"`.
- Cleans up on job completion: removes non‑persistent worktrees,
  releases locks, scrubs credentials.

### 2.2 SDK execution

- Builds the SDK options (`SDKOptionsBuilder`) — model, streaming URL,
  context profile, CLI arguments.
- Runs the SDK loop via `execute_sdk`, with configurable retries
  (`SDK_MAX_RETRIES` / `SDK_RETRY_BASE_DELAY`).
- Manages `user_interrupt_event` for cancellation signals arriving
  through the streaming bridge.
- Tracks turn count and feeds `conversation_summary` into the prompt
  when resuming.

### 2.3 Result reporting

- Marks the job `success`, `cancelled`, or `error` via
  `JobQueue.complete_job`.
- On success with `persist_session=True`: writes session metadata to
  `SessionStore.save_session` (session_id, worktree_path, turn_count,
  ref, ttl).
- On success with `session_token` present: updates the streaming store
  with `update_session_id` and `update_transcript_path`.
- On error: marks the streaming session as errored and publishes the
  error message.
- Releases the session dedup lock so follow‑up requests can proceed.

--------------------------------------------------------------------------------

## 3. Worker Does NOT Manage (out of scope)

Four areas are owned by **upstream** or **side‑channel** components.
The worker consumes their outputs but never mutates them directly:

| Area | Owner | Why the worker stays hands‑off |
|---|---|---|
| **Session creation** | `StreamingSessionStore.create_session` (called in `request_processor.py:557`) | The worker receives an already‑created `session_token` and job. It never calls `create_session` or `set_running` — the agent worker owns the full lifecycle state machine. |
| **Session transitions** | `request_processor.py:288‑343` (`_build_job_data`) | `session_mode` and `session_id` are resolved upstream by examining the `SessionStore`, parsing user flags (`-c`, `-f`, `-n`), checking turn limits, and consulting `ConversationConfig.auto_continue`. The worker only reads `session_mode` to decide whether to skip repo setup. |
| **Inbox (pending messages)** | `SessionStoreV2.push_inbox_message` / `pop_inbox_messages` (called by `SessionStreamBridge` / `session_proxy`) | The inbox buffers remote‑control commands and user follow‑up messages received during a running session. The worker never pushes or pops inbox messages; it only receives an interrupt signal when one arrives. |
| **Transcript path** | `StreamingSessionStore.update_transcript_path` (called by `request_processor` / `session_proxy` after the job completes) | The worker calls `find_transcript_path` to locate the transcript JSONL file produced by the SDK and passes it to `update_transcript_path`, but the **storage and serving** of the transcript path belongs to the streaming store. The worker does not decide where transcripts live. |

--------------------------------------------------------------------------------

## 4. Data Flow Summary

```
 ┌─────────────────────┐
 │  Agent Worker        │  resolves session_mode, session_id,
 │  (request_processor) │  conversation_summary, session_token
 └────────┬─────────────┘
          │  enqueue job_data (Redis)
          ▼
 ┌─────────────────────┐
 │  Sandbox Executor    │  reads the 5 session fields,
 │  (JobProcessor)      │  runs worktree + SDK, reports results
 └────────┬─────────────┘
          │  complete_job (Redis)
          ▼
 ┌─────────────────────┐
 │  Agent Worker        │  reads result, updates streaming store,
 │  (request_processor) │  posts GitHub comment with session URL
 └─────────────────────┘
```

The **SessionStore / SessionStoreV2** acts as persistent storage that
both sides read/write through Redis, but the **ownership** of each
operation is strictly partitioned as described above.
