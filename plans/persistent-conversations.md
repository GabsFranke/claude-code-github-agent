# Persistent Conversation Histories

## Summary

Add the ability for the bot to continue conversations across GitHub comments. Today every invocation is a stateless one-shot session — the bot has no memory of its own previous responses in a thread. This plan wires up the SDK's `resume` / `fork` capabilities so the bot can pick up where it left off.

## The Problem

Current flow for every job:

```
GitHub comment -> Webhook -> Worker -> Sandbox (fresh worktree) -> ClaudeSDKClient (new session) -> Response
                                                                                                        |
                                                                                                  Cleanup worktree
                                                                                                  Discard session file
```

Every run starts from zero. If a developer replies to the bot's PR review with "also check the auth module", the bot re-reads every file, re-analyzes everything, and has no recollection of what it already said. This wastes tokens, produces inconsistent follow-ups, and makes multi-turn interactions impossible.

The SDK already supports everything we need:

| SDK Feature | Field | What it does |
|---|---|---|
| Resume | `ClaudeAgentOptions.resume = session_id` | Continue a specific session by ID |
| Continue | `ClaudeAgentOptions.continue_conversation = True` | Resume the most recent session in `cwd` |
| Fork | `ClaudeAgentOptions.fork_session = True` | Branch from a session without modifying the original |
| Session ID | `ResultMessage.session_id` | Captured after every run |
| Session listing | `list_sessions(directory)` | Find sessions on disk |
| Session messages | `get_session_messages(session_id)` | Read session history |

**None of these are wired up.** The `SDKOptionsBuilder` has no `with_resume()` or `with_continue()` methods. The `execute_sdk()` wrapper never extracts `session_id` from `ResultMessage`. The `JobQueue` payload has no `session_id` field.

## Architecture

### Core Idea: Deterministic Worktrees + Session Persistence

The root issue is that the SDK scopes sessions to `cwd`, and worktrees get random paths. Two runs for the same thread = two different paths = SDK can't find the session. The fix: **deterministic worktree paths** that stay consistent across runs for the same conversation.

```
Worktree path structure:
/tmp/worktrees/{owner}--{repo}/{thread_type}-{thread_id}/{workflow_name}/

Examples:
/tmp/worktrees/myorg--myrepo/pr-42/review-pr/
/tmp/worktrees/myorg--myrepo/pr-42/fix-ci/
/tmp/worktrees/myorg--myrepo/issue-42/generic/
/tmp/worktrees/myorg--myrepo/discussion-7/generic/
```

The path encodes four dimensions:
1. **Repo** (`{owner}--{repo}`) — isolated per repository
2. **Thread type** (`pr`, `issue`, `discussion`) — PR #42 and issue #42 are different things
3. **Thread ID** — the issue/PR/discussion number
4. **Workflow** (`review-pr`, `fix-ci`, `generic`) — different workflows on the same PR get separate worktrees and sessions

### Session Lifecycle

```
After job completes:
  1. Extract session_id from ResultMessage
  2. Store mapping: Redis key "session:map:{repo}:{thread_type}:{thread_id}:{workflow}" -> {session_id, metadata}
  3. Worktree stays on disk (persistent, not ephemeral)

Before continuation job:
  1. Look up session_id from Redis for this repo + thread + workflow
  2. Worktree already exists at deterministic path — git fetch to update
  3. Set ClaudeAgentOptions.resume = session_id
  4. Agent continues with full conversation context in the same worktree
```

### Two Approaches (recommended: both)

**Approach A: Full Session Resume (resume)**

The SDK's native `resume` loads the entire conversation history — every message, every tool call, every tool result. The agent has perfect recall of what it did.

- The worktree is already at a deterministic path, so `cwd` is consistent across runs
- Pass `resume=session_id` in `ClaudeAgentOptions`
- The SDK finds the session file in `~/.claude/projects/<encoded-cwd>/`
- Best for: follow-ups within the same workflow on the same thread

**Approach B: Conversation Summary (fallback)**

When full session resume isn't possible (session file corrupted, different host, expired), inject a structured summary as context.

- After each job, extract a summary using the existing `transcript_parser.py`
- Store in Redis alongside the session_id
- Inject as part of `system_prompt` or `repository_context`
- Best for: cross-host resume, expired sessions, lightweight follow-ups

### Worktree Management

Worktrees are no longer ephemeral — they persist between runs and are cleaned up by events.

| Event | Action |
|---|---|
| PR closed/merged | Clean up all worktrees for that PR across all workflows |
| Issue closed | Clean up after a short delay (24h, might reopen) |
| Branch deleted | Clean up any worktrees tracking that branch |
| Workflow completes, no follow-up within TTL | Clean up worktree, keep session summary |
| Session TTL expires | Full cleanup — worktree + session |
| New push to PR with active session | `git fetch` in existing worktree, no new worktree |

**Worktree reuse:** on continuation, the existing worktree is updated via `git fetch` rather than recreated. The agent's session remembers what files it already read, so unchanged files benefit from prompt caching.

**Worktree recreation:** if a worktree was cleaned up but the session is still active (within TTL), a fresh worktree is created at the same deterministic path from the latest ref. The session carries conversation context, and the agent re-reads files it needs.

### Concurrency

Two jobs targeting the same PR + workflow should never run in parallel — they'd corrupt the worktree and session.

**Redis lock per path:** `lock:worktree:{owner}--{repo}:{thread_type}-{thread_id}:{workflow}`

- Second job waits for the first to complete, then resumes the updated session
- Timeout: if the lock is held beyond a reasonable time (e.g., 10 minutes), the second job proceeds with a fresh session and posts a note that context was lost

**Different workflows on the same PR** don't conflict — they have separate worktrees and sessions. `review-pr` and `fix-ci` can run simultaneously on PR #42 without issues.

### Session Storage

Session files live at `~/.claude/projects/<encoded-cwd>/<session-id>.jsonl` — the SDK manages these automatically. Since `cwd` is now deterministic, the SDK finds sessions without any file copying or symlink tricks.

**Redis metadata:**

```
session:map:{owner/repo}:{thread_type}:{thread_id}:{workflow} = {
    "session_id": "uuid",
    "repo": "owner/repo",
    "thread_type": "pr",
    "thread_id": "42",
    "workflow_name": "review-pr",
    "ref": "feature-branch",
    "worktree_path": "/tmp/worktrees/owner--repo/pr-42/review-pr/",
    "created_at": "2026-04-18T10:00:00Z",
    "last_run": "2026-04-18T10:05:00Z",
    "turn_count": 15,
    "status": "active"
}
TTL: configurable per-workflow (default 7 days), refreshed on each continuation
```

**No shared volume needed.** Session files live in the SDK's native location, and worktrees persist on disk. The Redis metadata is the only additional state.

## UX Design

### Commands

| Command | Behavior |
|---|---|
| `/agent -c <query>` | Continue the last conversation in this issue/PR. Uses `resume`. |
| `/agent --continue <query>` | Long form of `-c`. |
| `/agent -f <query>` | Fork from the last conversation — starts a new branch. Uses `fork_session`. |
| `/agent --fork <query>` | Long form of `-f`. |
| `/agent --new <query>` | Explicitly start a fresh session (ignore any existing conversation). |
| `/sessions` | List active sessions for this repo. Shows last run time, turn count. |
| `/sessions close` | Close the current session, start fresh on next interaction. |

### Auto-Continue Behavior

When someone replies in a thread where the bot has already commented (without any command):

- **Default behavior**: Start a fresh session (backward compatible, safe)
- **Opt-in auto-continue**: Repos can enable auto-continue via config. When enabled, any reply in a thread where the bot has an active session (within TTL) automatically uses `resume`.
- **Smart detection**: If the comment is clearly a follow-up ("check that file too", "what about the tests?"), use `resume`. If it's a new topic, start fresh. This can be heuristic-based initially and improved over time.

### Workflow Config Extension

```yaml
workflows:
  generic:
    triggers:
      commands:
        - /agent
    conversation:
      persist: true              # Enable session persistence (default: false)
      ttl_hours: 168             # Session TTL in hours (default: 168 = 7 days)
      max_turns: 50              # Max total turns across continuations
      auto_continue: false       # Auto-resume on replies (default: false)
      summary_fallback: true     # Use summary injection when full resume fails
    prompt:
      template: "{user_query}"
      system_context: "generic.md"
```

For PR review, continuation works differently since the agent operates on a PR, not a generic issue:

```yaml
workflows:
  review-pr:
    triggers:
      events:
        - event: pull_request.opened
      commands:
        - /review
        - /pr-review
        - /agent -c              # Continue a review conversation
    conversation:
      persist: true
      ttl_hours: 48              # Shorter TTL for PRs (reviews are time-sensitive)
```

## Implementation Plan

### Phase 1: Deterministic Worktrees

**1.1 Refactor worktree creation**

Currently the sandbox worker creates random worktrees. Change to deterministic paths:

```python
def get_worktree_path(repo: str, thread_type: str, thread_id: str, workflow: str) -> Path:
    """Deterministic worktree path for a conversation."""
    safe_repo = repo.replace("/", "--")
    return Path(f"/tmp/worktrees/{safe_repo}/{thread_type}-{thread_id}/{workflow}")
```

Thread type is resolved from the webhook event:

```python
def resolve_thread_type(event_data: dict) -> str:
    """Determine thread type from webhook payload."""
    if "pull_request" in event_data:
        return "pr"
    if event_data.get("issue", {}).get("pull_request"):
        return "pr"  # Comment on a PR (issue_comment payload with pull_request field)
    if "discussion" in event_data:
        return "discussion"
    return "issue"
```

**1.2 Worktree reuse logic**

Before creating a new worktree, check if one exists:

```python
worktree_path = get_worktree_path(repo, thread_type, thread_id, workflow)

if worktree_path.exists() and session_mode == "resume":
    # Reuse existing worktree — fetch latest changes
    await run_command(f"git -C {worktree_path} fetch origin")
    await run_command(f"git -C {worktree_path} checkout {ref}")
    await run_command(f"git -C {worktree_path} pull origin {ref}")
else:
    # Create fresh worktree at deterministic path
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    await create_worktree(bare_repo, ref, worktree_path)
```

**1.3 Concurrency lock**

```python
lock_key = f"lock:worktree:{safe_repo}:{thread_type}-{thread_id}:{workflow}"
lock = await redis.set(lock_key, job_id, nx=True, ex=600)  # 10 min timeout

if not lock:
    # Another job is running — wait or reject
    await post_comment("Already working on this — will continue shortly.")
    # Wait for lock release, then proceed
```

### Phase 2: Session Capture and Storage

**2.1 Extract session_id from SDK results**

Modify `shared/sdk_executor.py`:

```python
# After execute_sdk() completes, extract session_id from ResultMessage
session_id = None
for msg in all_messages:
    if isinstance(msg, ResultMessage):
        session_id = msg.session_id
return SDKResult(messages=all_messages, session_id=session_id, ...)
```

**2.2 Session metadata in Redis**

New module at `shared/session_store.py`:

```python
class SessionStore:
    """Manages session metadata in Redis for conversation continuity."""

    async def save_session(
        self,
        repo: str,
        thread_type: str,
        thread_id: str,
        workflow: str,
        session_id: str,
        worktree_path: Path,
        ref: str,
    ) -> None: ...

    async def get_session(
        self, repo: str, thread_type: str, thread_id: str, workflow: str
    ) -> SessionInfo | None: ...

    async def close_session(
        self, repo: str, thread_type: str, thread_id: str, workflow: str
    ) -> None: ...

    async def list_sessions(self, repo: str) -> list[SessionInfo]: ...
```

Redis schema:

```
session:map:{owner/repo}:{thread_type}:{thread_id}:{workflow} = {
    "session_id": "uuid",
    "repo": "owner/repo",
    "thread_type": "pr",
    "thread_id": "42",
    "workflow_name": "review-pr",
    "ref": "feature-branch",
    "worktree_path": "/tmp/worktrees/owner--repo/pr-42/review-pr/",
    "created_at": "2026-04-18T10:00:00Z",
    "last_run": "2026-04-18T10:05:00Z",
    "turn_count": 15,
    "status": "active"
}
TTL: configurable per-workflow (default 7 days)
```

### Phase 3: Session Resume

**3.1 SDKOptionsBuilder extensions**

Add to `shared/sdk_factory.py`:

```python
class SDKOptionsBuilder:
    def with_session_resume(self, session_id: str) -> "SDKOptionsBuilder":
        self._resume = session_id
        return self

    def with_session_continue(self) -> "SDKOptionsBuilder":
        self._continue = True
        return self

    def with_session_fork(self, session_id: str) -> "SDKOptionsBuilder":
        self._resume = session_id
        self._fork = True
        return self

    def build(self) -> ClaudeAgentOptions:
        return ClaudeAgentOptions(
            ...,
            resume=self._resume,
            continue_conversation=self._continue,
            fork_session=self._fork,
        )
```

**3.2 Job payload extension**

Add to `shared/job_queue.py`:

```python
# New fields in job payload:
{
    ...,
    "session_mode": "resume" | "continue" | "fork" | "new",   # default: "new"
    "session_id": "uuid" | None,                                # for resume/fork
    "thread_type": "pr" | "issue" | "discussion",              # thread type
    "thread_id": "42",                                          # issue/PR/discussion number
    "workflow_name": "review-pr",                               # workflow for worktree path
}
```

**3.3 Worker changes**

In `services/agent_worker/worker.py` (the coordinator), when building a job:

```python
thread_type = resolve_thread_type(event_data)
thread_id = event_data.get("issue_number") or event_data.get("discussion_number")

job_data["thread_type"] = thread_type
job_data["thread_id"] = str(thread_id)
job_data["workflow_name"] = workflow_name

session_info = await session_store.get_session(repo, thread_type, thread_id, workflow_name)

if session_info and command_has_continue_flag(user_query):
    job_data["session_mode"] = "resume"
    job_data["session_id"] = session_info.session_id
elif session_info and workflow_config.conversation.auto_continue:
    if looks_like_followup(user_query):
        job_data["session_mode"] = "resume"
        job_data["session_id"] = session_info.session_id
```

**3.4 Sandbox worker changes**

In `services/sandbox_executor/sandbox_worker.py`:

Before SDK invocation — use deterministic worktree:

```python
worktree_path = get_worktree_path(repo, thread_type, thread_id, workflow_name)
session_mode = job_data.get("session_mode", "new")

if session_mode in ("resume", "fork") and worktree_path.exists():
    # Reuse worktree, fetch latest
    await run_command(f"git -C {worktree_path} fetch origin")
    await run_command(f"git -C {worktree_path} checkout {ref}")
else:
    # Create fresh worktree at deterministic path
    await create_worktree(bare_repo, ref, worktree_path)

builder = builder.with_cwd(str(worktree_path))

if session_mode == "resume":
    builder = builder.with_session_resume(job_data["session_id"])
elif session_mode == "fork":
    builder = builder.with_session_fork(job_data["session_id"])
```

After SDK invocation — save session:

```python
new_session_id = result.session_id
if new_session_id and workflow_config.conversation.persist:
    await session_store.save_session(
        repo=repo,
        thread_type=thread_type,
        thread_id=thread_id,
        workflow=workflow_name,
        session_id=new_session_id,
        worktree_path=worktree_path,
        ref=ref,
    )
```

### Phase 4: Command Parsing

**4.1 Flag parsing in extraction_rules.py**

Extend the command parser to handle flags:

```python
# Current: "/agent check the auth module"
# New:     "/agent -c check the auth module too"
# New:     "/agent --fork try a different approach"

COMMAND_PATTERN = r'^(/\S+)\s*(-[cf]|--continue|--fork|--new)?\s*(.*)'
```

Parsed into:

```python
{
    "command": "/agent",
    "flags": {"continue": True, "fork": False, "new": False},
    "user_query": "check the auth module too",
}
```

**4.2 New /sessions command**

```python
# /sessions          -> list sessions for this repo
# /sessions close    -> close current thread's session
```

Routes through existing command dispatcher. The handler reads from `SessionStore` and formats a GitHub comment.

### Phase 5: Worktree Cleanup

**5.1 Event-driven cleanup**

Listen for GitHub events that signal a conversation is done:

```python
# In the worker or a dedicated cleanup handler:
async def handle_cleanup(event_type: str, event_data: dict):
    repo = event_data["repository"]["full_name"]

    if event_type in ("pull_request.closed", "pull_request.merged"):
        thread_id = str(event_data["pull_request"]["number"])
        await cleanup_worktrees(repo, "pr", thread_id)

    elif event_type == "issues.closed":
        thread_id = str(event_data["issue"]["number"])
        await schedule_cleanup(repo, "issue", thread_id, delay_hours=24)

    elif event_type == "delete":  # branch deleted
        branch = event_data["ref"]
        await cleanup_worktrees_by_branch(repo, branch)
```

**5.2 TTL-based cleanup**

Periodic scan (via the scheduler service or a simple cron) removes expired sessions and their worktrees:

```python
async def cleanup_expired_sessions():
    """Remove sessions past their TTL and clean up worktrees."""
    sessions = await session_store.list_all_expired()
    for session in sessions:
        worktree_path = Path(session.worktree_path)
        if worktree_path.exists():
            shutil.rmtree(worktree_path)
        await session_store.close_session(session.repo, session.thread_type, session.thread_id, session.workflow_name)
```

**5.3 Orphan detection**

Startup scan to catch worktrees that lost their Redis metadata (e.g., Redis restart without persistence):

```python
async def detect_orphan_worktrees():
    """Find worktrees on disk with no corresponding Redis session."""
    worktree_base = Path("/tmp/worktrees")
    if not worktree_base.exists():
        return

    for repo_dir in worktree_base.iterdir():
        for thread_dir in repo_dir.iterdir():
            for workflow_dir in thread_dir.iterdir():
                # Parse path components
                # Check Redis for corresponding session
                # If no session AND not recently modified, clean up
```

### Phase 6: Summary Extraction (Fallback)

**6.1 Summary generation**

Extend the existing `Stop` hook in `SDKOptionsBuilder.with_transcript_staging()`:

- After staging the transcript, also generate a conversation summary
- Use the existing `transcript_parser.extract_conversation()` to get the text
- Truncate to a configurable budget (default 4096 tokens)
- Store alongside the session metadata in Redis

Summary structure:

```json
{
  "topic": "What the conversation was about",
  "key_decisions": ["Decision 1", "Decision 2"],
  "files_examined": ["src/auth.py", "src/middleware.py"],
  "files_modified": ["src/auth.py"],
  "tools_used": ["Read", "Edit", "Grep", "Bash"],
  "last_action": "Posted review comment on PR #123",
  "turn_count": 15,
  "summary": "Full conversation summary text..."
}
```

**6.2 Fallback injection**

When full session resume fails (session corrupted, worktree recreated):

```python
summary = await session_store.get_summary(repo, thread_type, thread_id, workflow)
if summary:
    builder = builder.with_system_prompt(
        existing_prompt + f"\n\n## Previous Conversation Context\n{summary}"
    )
```

## Scope and Limits

### Session TTL

Sessions expire after a configurable TTL (default 7 days). On expiry:
- Redis key is deleted (auto via TTL)
- Worktree is cleaned up on next cleanup scan
- Next interaction starts fresh

### Turn Limits

Cumulative turn count is tracked. If total turns exceed `max_turns` (default 50 across all continuations):
- Session is auto-closed
- New interaction starts fresh with a summary of the previous conversation injected
- Bot posts a comment: "Starting a fresh session (previous conversation reached turn limit)"

### Session Size Limits

Session files grow with each turn. Guard rails:
- If session JSONL exceeds 10MB, force-close and start fresh with summary
- If context window is filling too fast (high cache_read_input_tokens), suggest starting a new session

### Multi-Repo Isolation

Sessions are scoped to `repo + thread_type + thread_id + workflow`. Different issues in the same repo get different sessions. The same issue number in different repos is isolated by the repo prefix. Different workflows on the same PR get independent sessions.

## Files to Create

```
shared/session_store.py          # Session persistence manager (Redis metadata)
services/sandbox_executor/worktree_manager.py  # Deterministic worktree creation + cleanup
```

## Files to Modify

```
shared/sdk_factory.py            # Add with_session_resume/continue/fork to SDKOptionsBuilder
shared/sdk_executor.py           # Extract session_id from ResultMessage, return it
shared/job_queue.py              # Add session_mode, session_id, thread_type, thread_id to payload
shared/queue.py                  # Add session fields to message payloads
shared/transcript_parser.py      # Add summary extraction function
shared/config.py                 # Add session config dataclass
shared/workflow_engine.py        # Parse conversation config from workflows.yaml
services/agent_worker/worker.py  # Look up sessions, set session_mode in job data
services/agent_worker/processors/request_processor.py  # Parse -c/-f flags from commands
services/webhook/extraction_rules.py  # Extended command parsing with flags
services/sandbox_executor/sandbox_worker.py  # Use deterministic worktrees, configure resume/fork
workflows.yaml                   # Add conversation config sections
docker-compose.yml               # Add /tmp/worktrees as a named volume
```

## Implementation Order

1. **services/sandbox_executor/worktree_manager.py** — Deterministic worktree paths + reuse logic
2. **shared/session_store.py** — Redis session metadata store
3. **shared/sdk_executor.py** — Extract session_id from ResultMessage
4. **shared/sdk_factory.py** — Add `with_session_resume/continue/fork`
5. **shared/job_queue.py** — Add session + thread fields to payload
6. **services/sandbox_executor/sandbox_worker.py** — Use worktree manager, restore sessions, save after
7. **services/agent_worker/worker.py** — Session lookup, thread type resolution, job enrichment
8. **services/webhook/extraction_rules.py** — Parse `-c` / `-f` / `--new` flags
9. **Worktree cleanup** — Event-driven + TTL-based + orphan detection
10. **Summary extraction** — Conversation summary for fallback
11. **workflows.yaml** — Conversation config sections
12. **/sessions command** — List and close sessions

Steps 1–7 form the MVP (deterministic worktrees + manual `-c` flag). Steps 8–12 add QoL.

## Risks

| Risk | Mitigation |
|---|---|
| Disk usage from persistent worktrees | Event-driven cleanup on PR close/merge, TTL expiry, orphan detection |
| Two jobs on same worktree concurrently | Redis lock per worktree path, second job waits |
| Orphan worktrees after Redis restart | Startup scan compares disk state to Redis metadata |
| Session file too large (many turns) | Enforce max_turns. Auto-close and summarize at limit. |
| Worktree becomes stale (outdated branch) | `git fetch` on resume, agent re-reads files it needs |
| Stale session references deleted branch | Cleanup on branch delete event, recreate worktree from default branch |
| Same PR number as issue number | Thread type in path (`pr-42` vs `issue-42`) — no collision |
| Multiple workflows on same PR | Workflow name in path — independent worktrees and sessions |

## Out of Scope

- Long-running persistent `ClaudeSDKClient` connections — this design uses ephemeral containers with session restore, not long-lived processes
- Cross-repo sessions — sessions are scoped to repo + thread
- Session editing/branching UI — just `/sessions list` and `/sessions close`
- Streaming responses to GitHub comments — sessions help with continuity, not real-time streaming
- Storing session files in external object storage (S3, etc.) — local disk is sufficient; external storage can be added later if multi-host deployment requires it

---

## Phase 2: CLI-Friendly Volume Consolidation

**Status: Planned. Builds on top of Phase 1 (session persistence) to make the SDK CLI-friendly.**

### Context

The original design stores worktrees at `/tmp/worktrees/` (ephemeral), memory at `/home/bot/agent-memory/`, and transcripts at `/home/bot/transcripts/` — three separate Docker volumes. Meanwhile, `~/.claude/` (where the SDK writes sessions and where the CLI expects them) is ephemeral. This means:

1. Sessions are lost on container restart
2. `claude --resume` cannot work locally
3. Three volumes need management instead of one
4. Transcript staging copies files that already exist under `~/.claude/projects/`

The fix: consolidate everything under `~/.claude/` and bind-mount it to the host's `~/.claude/` so the local `claude` CLI finds sessions natively.

### Directory Layout

```
/home/bot/.claude/                          ← bind-mounted to ~/.claude/ on host (C:\Users\Gabs\.claude\)
  projects/                                 ← SDK session JSONL (native SDK path)
  memory/{owner}/{repo}/memory/             ← Per-repo memory (was /home/bot/agent-memory/)
    index.md
    ...
  worktrees/{owner}--{repo}/{thread}-{id}/{workflow}/  ← Persistent worktrees (was /tmp/worktrees/)
  transcripts/{owner}/{repo}/               ← Staged copies (eliminated in Phase 2D)
```

### Phase 2A: Seed plugins/skills into bind-mounted `~/.claude/`

**Why:** The Dockerfile COPY-bakes plugins into `/home/bot/.claude/plugins/` and skills into `/home/bot/.claude/skills/`. A bind mount at `~/.claude/` overlays these with host data, hiding the baked-in files. Instead, store them at `/app/` in the image and copy into the bind mount on startup.

**Changes:**
- `services/sandbox_executor/Dockerfile`: `COPY plugins/ /app/plugins/`, `COPY skills/ /app/skills/`
- Add an entrypoint script that seeds plugins/skills into `~/.claude/` before the main process starts:
  ```bash
  cp -rn /app/plugins/* /home/bot/.claude/plugins/ 2>/dev/null
  cp -rn /app/skills/* /home/bot/.claude/skills/ 2>/dev/null
  ```
  `cp -rn` only copies files that don't already exist — personal plugins stay untouched, bot plugins get seeded on first run, then persist via bind mount.
- Plugins end up in `C:\Users\Gabs\.claude\plugins\` on the host — natively available to the local `claude` CLI
- No changes to `sdk_factory.py` plugin discovery — SDK still finds them at `~/.claude/plugins/`

### Phase 2B: Consolidate volumes to `~/.claude` bind mount

**Docker changes** — `docker-compose.yml` and `docker-compose.minimal.yml`:
- Remove named volumes `agent-memory` and `transcripts`
- Add bind mount `~/.claude:/home/bot/.claude:rw` on: sandbox_worker, memory_worker, retrospector_worker
- Add bind mount `~/.claude:/home/bot/.claude:ro` on: worker (agent_worker, reads memory only)
- Data appears at `C:\Users\Gabs\.claude\` on the host — same location the local `claude` CLI uses natively

**Note:** This merges bot data (sessions, memory, worktrees) into the user's `~/.claude/`. The bot's data lives in subdirectories that don't conflict with personal Claude Code usage (e.g., `memory/` and `worktrees/` are bot-specific). SDK sessions go to `projects/<encoded-cwd>/` which is already how Claude Code organizes by project — the bot's Docker paths just add more project entries.

**Memory path changes** (6 files):
- `mcp_servers/memory/tools.py:12` — `/home/bot/agent-memory/` → `/home/bot/.claude/memory/`
- `services/memory_worker/memory_worker.py:63` — same
- `services/agent_worker/processors/repository_context_loader.py:103` — same
- `shared/sdk_factory.py:683` — same
- `shared/sdk_factory.py:344` — same
- `shared/context_builder.py:20` — `DEFAULT_CACHE_DIR` → `Path("/home/bot/.claude")`

**Transcript staging path** (1 file):
- `shared/post_processing.py:51` — `/home/bot/transcripts/` → `/home/bot/.claude/transcripts/`

**Dockerfile updates:**
- `services/sandbox_executor/Dockerfile` — remove `mkdir /home/bot/agent-memory` and `/home/bot/transcripts`
- `services/memory_worker/Dockerfile` — same, add `mkdir -p /home/bot/.claude/memory`
- `services/retrospector_worker/Dockerfile` — remove `mkdir /home/bot/transcripts`

**Data migration:** One-shot container copies old volumes → new bind mount before switching.

### Phase 2C: Persistent worktrees in `~/.claude/`

**Why:** Worktrees at `/tmp/worktrees/` are lost on container restart. Move into the persistent bind-mounted volume.

- `services/sandbox_executor/worktree_manager.py` — `WORKTREE_BASE = Path("/home/bot/.claude/worktrees")`
- Ephemeral (non-persistent) sessions still use `/tmp/`

### Phase 2D: Eliminate transcript staging

**Why:** The SDK writes sessions to `~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`. With the shared bind mount, workers can read directly — no copy needed.

**New flow:**
1. SDK writes transcript to `~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`
2. Hook captures native path from `input_data["transcriptPath"]`
3. Pass native path directly to Redis job (no copy)
4. Workers read from native path via shared bind mount

**Changes:**
- `shared/post_processing.py` — remove `stage_transcript()` and `stage_transcript_with_retry()`, update `flush_pending_post_jobs()` to pass native path
- `shared/sdk_factory.py` lines 401-523 — simplify `with_transcript_staging()` to capture path without copying
- Workers unchanged (already read `transcript_path` from Redis message)

### Phase 2E: CLI access

Data is on disk at `~/.claude/` via the bind mount — same location the CLI uses natively. To resume a session locally:

```bash
claude --resume <session-id>
```

The encoded-cwd paths differ between Docker (e.g. `/home/bot/.claude/worktrees/owner--repo/...`) and local, but `--resume <session-id>` searches across all projects by session ID. Verify during implementation.

**Helper script** (`scripts/cli-access.sh`):
- Lists recent sessions from `~/.claude/projects/`
- Resumes a session by ID

### Verification

After each sub-phase:
1. `bash ./check-code.sh` — lint, type-check, format
2. `pytest tests/` — unit tests
3. Docker build — verify image builds
4. Deploy and run a full job pipeline: webhook → sandbox → memory_worker → retrospector_worker
5. Verify data under `~/.claude/` — check projects, memory, worktrees exist
6. After Phase 2E: test `claude --resume <session-id>` locally from the host
