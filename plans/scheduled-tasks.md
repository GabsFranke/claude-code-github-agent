# Scheduled Tasks Feature Plan

## Summary

Add a `SchedulerService` that extends the existing event-driven architecture with time-based workflow triggers. The scheduler reads cron expressions from `workflows.yaml`, enqueues jobs into the existing `agent-requests` Redis queue, and reuses the entire processing pipeline (Worker, Sandbox, SDK, MCP servers, post-processing).

## Why

The bot is currently purely reactive — it only responds to GitHub webhooks. Many valuable workflows are periodic by nature: stale PR management, nightly health reports, security audits, dependency updates. Adding scheduling turns the bot into a proactive agent that maintains repository health without human triggers.

## Architecture

```
workflows.yaml (schedule triggers)
        |
        v
  SchedulerService (new container)
        |
        v
  Redis agent-requests queue
        |
        v
  [Existing pipeline: Worker -> Sandbox -> Claude SDK -> Post-processing]
```

The scheduler is just another trigger source alongside webhooks and commands. No changes to the core pipeline.

## Phase 1: Core Scheduler

### 1.1 Workflow Config Extension

Extend `workflows.yaml` with a `schedule` trigger type:

```yaml
workflows:
  stale-pr-checker:
    triggers:
      schedule:
        cron: "0 9 * * 1-5"        # Weekdays 9am
        timezone: "UTC"             # Optional, default UTC
        repos: ["owner/repo"]       # Optional: scope to specific repos
        enabled: true               # Optional: disable without removing
    prompt:
      template: "Check for stale PRs older than 7 days in {repo}. Comment with a reminder. Close issues with no activity for 30 days."
    description: "Stale PR and issue management"
    context:
      repomap_budget: 2048
```

A workflow can have both event and schedule triggers:

```yaml
workflows:
  security-audit:
    triggers:
      events:
        - event: secret_scanning_alert.created
      schedule:
        cron: "0 2 * * 1"          # Monday 2am
    prompt:
      template: "Run security audit for {repo}..."
```

### 1.2 Scheduler Service

New service at `services/scheduler/`:

```
services/scheduler/
  main.py              # Entry point, APScheduler setup
  scheduler.py         # Core scheduler logic
  config.py            # Schedule config parser
  Dockerfile
```

**Dependencies:**
- `APScheduler>=3.10` with Redis job store (for distributed locking)
- Reuses existing `shared/config.py` and `shared/queue.py`

**`scheduler.py` responsibilities:**
1. On startup: read `workflows.yaml`, extract schedules, register cron jobs
2. On cron tick: for each matching repo, publish a job to `agent-requests`
3. Handle config hot-reload (watch `workflows.yaml` for changes via Redis pub/sub or file watch)
4. Distributed lock via Redis to prevent duplicate scheduling across multiple instances
5. Health reporting via existing `shared/health.py`

**Job payload** (published to `agent-requests`):
```python
{
    "workflow_name": "stale-pr-checker",
    "repo": "owner/repo",
    "ref": "main",                    # Default branch
    "trigger_type": "schedule",       # Distinguishes from "event" / "command"
    "scheduled_at": "2026-04-18T09:00:00Z",
    "installation_id": 12345
}
```

### 1.3 WorkflowEngine Changes

Minimal changes to `shared/workflow_engine.py`:

- Add `get_scheduled_workflows()` method that returns workflows with schedule triggers
- Add `get_repos_for_schedule(workflow_name)` to resolve target repos
- Validate cron expressions at load time (fail fast on bad config)

### 1.4 Worker Changes

Minimal changes to `services/agent_worker/worker.py`:

- Handle `trigger_type: "schedule"` in job processing
- For scheduled jobs: resolve repo list from installation data if `repos` not explicitly configured
- Skip CLAUDE.md fetch for scheduled jobs if repo not yet installed (log warning, skip job)

### 1.5 Docker Compose

Add to `docker-compose.yml`:

```yaml
  scheduler:
    build:
      context: .
      dockerfile: services/scheduler/Dockerfile
    depends_on:
      - redis
    environment:
      - REDIS_URL=redis://redis:6379
      - GITHUB_APP_ID=${GITHUB_APP_ID}
      - GITHUB_PRIVATE_KEY=${GITHUB_PRIVATE_KEY}
    volumes:
      - ./workflows.yaml:/app/workflows.yaml:ro
    healthcheck:
      test: ["CMD", "python", "-c", "import requests; requests.get('http://localhost:8080/health')"]
      interval: 30s
      timeout: 10s
      retries: 3
```

Not included in `docker-compose.minimal.yml` — scheduling is an optional add-on.

### 1.6 Rate Limiting

Extend `shared/rate_limiter.py`:

- Add priority classes: `webhook=high`, `command=medium`, `schedule=low`
- Scheduled jobs yield to webhook/command jobs when rate limit is near capacity
- Implement per-repo scheduling limits (e.g., max 1 scheduled job per repo per hour by default, configurable)

## Phase 2: Built-in Scheduled Workflows

### 2.1 Starter Workflow Pack

Ship these as commented-out examples in `workflows.yaml`:

| Workflow | Cron | Description |
|----------|------|-------------|
| `stale-pr-checker` | `0 9 * * 1-5` | Comment on PRs with no activity >7d, close stale issues >30d |
| `nightly-health` | `0 2 * * *` | Architecture review, tech debt scan, complexity trends |
| `weekly-security` | `0 9 * * 1` | Dependency vulnerability scan, audit branch protection |
| `flaky-test-detector` | `0 */6 * * *` | Analyze recent CI runs, identify intermittently failing tests |
| `dependency-updater` | `0 10 * * *` | Check for new dependency versions, create PRs for safe bumps |
| `ci-health-monitor` | `0 */4 * * *` | Track CI duration trends, alert on regressions |
| `repo-maintenance` | `0 3 * * 0` | Clean stale branches, validate settings |
| `weekly-digest` | `0 17 * * 5` | Summarize week's reviews, fixes, activity into a digest issue |

### 2.2 Per-Workflow Prompts

Create prompt templates in `prompts/` for each scheduled workflow:

- `prompts/stale-pr.md` — Instructions for identifying and managing stale PRs/issues
- `prompts/nightly-health.md` — Code health analysis instructions
- `prompts/weekly-security.md` — Security audit instructions
- `prompts/weekly-digest.md` — Digest generation format

### 2.3 Idempotency Guards

Each scheduled prompt should include:
- Check for existing bot comments before creating new ones (avoid duplicates on re-run)
- Use deterministic issue titles (e.g., "[Weekly Digest] Week of 2026-04-14")
- Reference the `scheduled_at` timestamp to avoid double-processing

## Phase 3: Per-Repo Schedule Configuration

### 3.1 Repo-Level Schedules

Allow repos to define their own scheduled workflows via `.claude/schedules.yaml`:

```yaml
# .claude/schedules.yaml (in the target repo)
schedules:
  dependency-updater:
    cron: "0 6 * * 1-5"     # Override: weekdays 6am (instead of bot default)
    enabled: true

  custom-check:
    cron: "0 8 * * *"
    prompt: "Run the custom health check script and report results"
    context:
      repomap_budget: 4096
```

### 3.2 Schedule Resolution

Priority order (highest wins):
1. Repo-level `.claude/schedules.yaml` (if exists)
2. Bot-level `workflows.yaml` with explicit `repos` scope
3. Bot-level `workflows.yaml` with `repos: ["*"]` (all installed repos)

### 3.3 Discovery

- On `installation_repositories.added`: read `.claude/schedules.yaml` from each new repo
- Cache schedules in Redis with repo-scoped keys
- Scheduler refreshes cache on cron tick (lazy) or via repo sync event

## Phase 4: Observability & Control

### 4.1 Schedule Status Tracking

Redis keys for schedule state:

```
scheduler:schedules:{workflow_name}       # Schedule config (cron, repos, enabled)
scheduler:last_run:{workflow_name}:{repo}  # Last run timestamp
scheduler:next_run:{workflow_name}:{repo}  # Next scheduled run
scheduler:runs:{workflow_name}:{repo}      # Run history (last 10, capped list)
```

### 4.2 Slash Command Control

Add commands for managing schedules via GitHub comments:

- `/schedule list` — Show active schedules for this repo
- `/schedule enable <workflow>` — Enable a scheduled workflow
- `/schedule disable <workflow>` — Disable a scheduled workflow
- `/schedule run <workflow>` — Manually trigger a scheduled workflow immediately
- `/schedule status` — Show last/next run times

These route through the existing command dispatcher in `extraction_rules.py`.

### 4.3 Health & Metrics

- Expose `/health` endpoint (like other services)
- Track: jobs enqueued per schedule, jobs completed, jobs failed, jobs skipped (rate-limited)
- Log schedule fires at INFO level
- Alert (via GitHub issue?) if a schedule hasn't fired successfully in 3 consecutive attempts

## Implementation Order

1. **Phase 1.2–1.5**: Scheduler service + Docker config + queue integration
2. **Phase 1.1**: Workflow config extension + parser
3. **Phase 1.3–1.4**: Minimal Worker/WorkflowEngine changes
4. **Phase 1.6**: Rate limiting with priority classes
5. **Phase 2.1–2.2**: Starter workflows + prompt templates
6. **Phase 2.3**: Idempotency guards in prompts
7. **Phase 4.1**: Schedule status tracking in Redis
8. **Phase 4.2**: Slash command control
9. **Phase 3**: Per-repo schedule configuration
10. **Phase 4.3**: Observability and metrics

Steps 1–4 form the MVP. Steps 5–6 add immediate value. Steps 7–10 are polish.

## Files to Create

```
services/scheduler/
  __init__.py
  main.py                    # FastAPI app + scheduler startup
  scheduler.py               # APScheduler wrapper, cron registration
  config.py                  # Schedule config parser (reads workflows.yaml)
  Dockerfile
  requirements.txt           # apscheduler, redis

plans/
  scheduled-tasks.md         # This file
```

## Files to Modify

```
workflows.yaml               # Add schedule triggers to workflow definitions
docker-compose.yml           # Add scheduler service
shared/workflow_engine.py    # Add get_scheduled_workflows()
shared/queue.py              # Add priority field to job payload
shared/config.py             # Add scheduler config vars
services/agent_worker/worker.py  # Handle trigger_type=schedule
docs/ARCHITECTURE.md         # Document scheduler component
docs/WORKFLOWS.md            # Document schedule triggers
```

## Dependencies

- `APScheduler>=3.10` — Lightweight, Redis-backed job store, cron expressions
- No new external services — reuses existing Redis

## Risks & Mitigations

| Risk | Mitigation |
|------|-----------|
| Rate limit exhaustion from scheduled jobs | Priority-based rate limiting (schedule = low) |
| Duplicate runs on scheduler restart | Redis distributed lock + `last_run` tracking |
| Runs against repos where bot is uninstalled | Check installation status before enqueuing |
| Config error breaks all schedules | Validate at load time, skip invalid schedules, log errors |
| Memory pressure from many repos | Batch repos, stagger cron ticks with jitter |

## Out of Scope

- Cron expression editor UI — config-file-driven only
- Claude Code Remote Triggers integration — self-hosted, no claude.ai dependency
- Per-minute polling — use webhooks for real-time
- Custom schedule state machine — keep it simple: enabled/disabled + cron
