# Workflows Guide

How to create and configure workflows — the rules that determine how the agent responds to GitHub events and commands.

Everything lives in a single file: `workflows.yaml`. No code changes needed.

## Workflow Structure

```yaml
my-workflow:
  description: "What this workflow does"
  triggers:
    events:
      - event: issues.opened
      - event: issues.labeled
        filters:
          label.name: "bug"                # optional, exact match or list
    commands:
      - /my-command
  prompt:
    template: "Do something with {repo} #{issue_number}"
    system_context: "my-context.md"        # optional, filename from prompts/ or inline
  context:
    repomap_budget: 4096                   # token budget for structural context
    personalized: true                     # personalize repomap toward changed files
    include_test_files: true               # include test files in personalization
    priority_focus: ["build_system"]       # focus areas for repomap ranking
    thread_history:                        # inject issue/PR comment history
      enabled: true                        # default: true
      max_comments: 100                    # default: 100
      include_pr_reviews: true             # default: true (PRs only)
      include_issue_body: true             # default: true
  skip_self: true                          # default: true
```

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `triggers.events` | At least one of events/commands | `[]` | List of event entries (see below) |
| `triggers.commands` | At least one of events/commands | `[]` | Slash command triggers |
| `prompt.template` | Yes | — | Prompt with placeholders (`{repo}`, `{issue_number}`, `{user_query}`) or plugin invocation |
| `prompt.system_context` | No | `None` | Agent instructions. Filename from `prompts/` or inline string |
| `context.repomap_budget` | No | `2048` | Token budget for the repomap |
| `context.personalized` | No | `false` | Personalize repomap toward changed files |
| `context.include_test_files` | No | `true` | Include test files in personalization |
| `context.priority_focus` | No | `[]` | Focus areas for repomap ranking (e.g. `build_system`, `test_structure`) |
| `context.thread_history.enabled` | No | `true` | Whether to inject issue/PR comment history into the agent's context |
| `context.thread_history.max_comments` | No | `100` | Maximum number of comments to fetch from GitHub |
| `context.thread_history.include_pr_reviews` | No | `true` | Whether to include PR review comments (inline code comments). Only applies to PRs |
| `context.thread_history.include_issue_body` | No | `true` | Whether to include the issue/PR description as the root of the history |
| `description` | No | `""` | Human-readable description |
| `conversation` | No | — | Multi-turn conversation settings (see below) |
| `streaming` | No | — | Real-time streaming settings (see below) |
| `skip_self` | No | `true` | Skip events triggered by the bot itself |

### Conversation Persistence

Add a `conversation:` block to let users continue a conversation across multiple comments:

```yaml
conversation:
  persist: true           # save session state after each run
  ttl_hours: 720          # how long before the session expires (default: 720 = 30 days)
  auto_continue: true     # automatically resume on next trigger (default: false)
  max_turns: 50           # turn limit before forcing a new session (optional)
  summary_fallback: true  # inject a summary if the full session can't be resumed (default: true)
```

When `persist: true`, users can control continuation in their comments:

| Flag | Behavior |
|------|----------|
| `/review -c` or `--continue` | Resume the last session in this thread |
| `/review -f` or `--fork` | Fork the last session into a new conversation |
| `/review --new` | Start a fresh session, ignoring any existing one |

Without any flag, the behavior depends on `auto_continue`: if `true`, the session resumes automatically; if `false` (default), a new session starts each time.

### Streaming

Add a `streaming:` block to enable real-time session streaming. When enabled, the sandbox worker publishes SDK messages to Redis pub/sub and posts a live-view URL in the GitHub comment. The session_proxy service bridges those messages to a browser via WebSocket.

```yaml
streaming:
  enabled: true            # enable real-time streaming (default: false)
  tool_approval: false     # require human approval before each tool call (default: false)
  auto_approve_timeout: 30 # seconds to wait for approval before auto-approving (default: 30)
```

| Field | Default | Description |
|-------|---------|-------------|
| `streaming.enabled` | `false` | Enable real-time streaming for this workflow |
| `streaming.tool_approval` | `false` | Require human approval before each tool call. If `false`, tools run automatically but are still visible in the UI |
| `streaming.auto_approve_timeout` | `30` | Seconds to wait for tool approval before auto-approving. Only relevant when `tool_approval: true` |

### Event Entries

Each entry in `triggers.events` uses the `event:` key with optional per-event `filters`:

```yaml
events:
  - event: pull_request.opened             # no filters, always triggers
  - event: pull_request.labeled            # only triggers when label matches
    filters:
      label.name: ["review", "bug"]
```

Per-event filters are scoped to that event only — different events in the same workflow can have different (or no) filters. Filters use dot-path notation to match fields in the webhook payload. All filters must match (AND logic). Values can be a string for exact match or a list for any-of matching.

Plain strings are also supported for backward compatibility:

```yaml
events:
  - pull_request.opened                    # equivalent to: event: pull_request.opened
```

## Triggers

### Event Triggers

Respond to GitHub webhook events using `event_type.action`:

```yaml
triggers:
  events:
    - event: pull_request.opened
    - event: issues.opened
    - event: workflow_job.completed
```

Some events have no action (e.g. `push`).

#### Per-Event Filters

Attach filters to specific events so different events in the same workflow can have different conditions:

```yaml
triggers:
  events:
    - event: pull_request.opened
    - event: pull_request.labeled
      filters:
        label.name: ["review", "pr-review"]
```

When `pull_request.opened` fires, no filters are checked. When `pull_request.labeled` fires, only payloads with a matching label pass.

### Command Triggers

Anyone can trigger a workflow by typing `/command` in an issue or PR comment. The command must be at the start of the comment body:

```
/review
/fix-ci
/agent review the auth logic for security issues
```

Anything after the command becomes `{user_query}` in the prompt template.

```yaml
triggers:
  commands:
    - /review
    - /fix-ci
    - /agent
```

You can define multiple commands for the same workflow (aliases):

```yaml
triggers:
  commands:
    - /fix-ci
    - /fix-build
    - /fix-tests
```

A workflow can have both event and command triggers.

### Payload Filters

Restrict when a workflow triggers based on payload values. Filters can be attached per-event (recommended) or at the trigger level (applies to all events without per-event filters). All filters must match (AND logic).

```yaml
# Per-event filters (recommended)
triggers:
  events:
    - event: workflow_job.completed
      filters:
        workflow_job.conclusion: "failure"
    - event: issues.labeled
      filters:
        label.name: ["bug", "security", "critical"]       # any of these values

# Combine multiple filters on a single event (all must match)
triggers:
  events:
    - event: workflow_job.completed
      filters:
        workflow_job.conclusion: "failure"
        workflow_job.head_branch: "develop"                # failure AND on develop
```

If no `filters` are defined on an event entry, the workflow always triggers on that event.

#### Workflow-Level Filters

You can also set `filters` at the trigger level as a fallback for events that don't have their own per-event filters:

```yaml
# Workflow-level filters apply to all events without per-event filters
triggers:
  filters:
    action: "opened"                   # applies to events without their own filters
  events:
    - event: pull_request.opened       # no per-event filters → uses trigger-level filters
    - event: pull_request.labeled      # has own filters → ignores trigger-level filters
      filters:
        label.name: ["review"]
```

Per-event filters take priority — when an event entry has its own `filters`, the trigger-level filters are ignored for that event.

## Prompts

### Template Placeholders

| Placeholder | Description |
|-------------|-------------|
| `{repo}` | Repository full name (e.g. `owner/repo`) |
| `{issue_number}` | Issue or PR number |
| `{user_query}` | User's text after a command |

### Plugin Invocation

Prefix with a plugin command to delegate to a specialized agent:

```yaml
template: "/pr-review-toolkit:review-pr {repo} {issue_number}"
```

### System Context

Instructions for the agent. Provide a filename (loaded from the `prompts/` directory) or an inline string:

```yaml
# Loads prompts/review.md
system_context: "review.md"

# Inline
system_context: |
  You are a code reviewer. Focus on security and performance.
```

### Skip Self

By default, workflows ignore events triggered by the bot itself (prevents infinite loops). Set `skip_self: false` to allow the bot to respond to its own events.

Even with `skip_self: true`, humans can manually trigger workflows on bot PRs using commands like `/review`.

### Thread History

By default, every agent invocation includes the full comment history from the issue or PR it's running on. This means a `/triage` run followed by an `/agent` run on the same issue shares context — the second agent sees everything the first one posted.

Thread history is injected as a `<thread_history>` block in the system prompt, between the workflow instructions and the structural context (file tree/repomap). If the total prompt exceeds the 12K token budget, older comments are truncated first, keeping the most recent ones.

Supported thread types:

| Type | API Used | What's Included |
|------|----------|----------------|
| `issue` | REST API | Issue body + all comments |
| `pr` | REST API | PR description + issue comments + review comments (inline) |
| `discussion` | GraphQL API | Discussion body + comments + nested replies |

Configuration (under `context.thread_history`):

```yaml
context:
  thread_history:
    enabled: true           # default: true — set false to disable
    max_comments: 100       # default: 100 — cap on comments fetched
    include_pr_reviews: true  # default: true — inline review comments on PRs
    include_issue_body: true  # default: true — include the issue/PR description
```

All fields are optional — the defaults work for most workflows. Override only when you need different behavior, e.g. fewer comments for lightweight tasks:

```yaml
triage-issue:
  context:
    thread_history:
      max_comments: 50        # less history for a quick triage
```

Thread history is **disabled for events without a thread** (e.g., `workflow_job`, `push`). The fetch silently returns empty and the agent runs without history.

## Creating a New Workflow

### 1. Add to `workflows.yaml`

```yaml
workflows:
  my-workflow:
    description: "What it does"
    triggers:
      events:
        - event: issues.opened
      commands:
        - /my-command
    prompt:
      template: "Analyze {repo} #{issue_number}"
      system_context: "my-context.md"
    context:
      repomap_budget: 2048
```

### 2. (Optional) Create system context

Add `prompts/my-context.md` with agent instructions:

```markdown
When analyzing this repo:
1. Read the relevant files
2. Check for common issues
3. Propose specific fixes
```

### 3. Restart

```bash
docker-compose restart worker
```

## Built-in Workflows

| Workflow | Events | Commands | Description |
|----------|--------|----------|-------------|
| `review-pr` | `pull_request.opened`, `pull_request.labeled` (label: `review`, `pr-review`, `review-pr`) | `/review`, `/pr-review`, `/review-pr` | Full PR review via pr-review-toolkit |
| `triage-issue` | `issues.opened`, `issues.labeled` (label: `triage`) | `/triage`, `/triage-issue` | Triage with priority and complexity assessment |
| `fix-ci` | `workflow_job.completed` (failure only) | `/fix-ci`, `/fix-build`, `/fix-tests` | Analyze CI logs and push fix via ci-failure-toolkit |
| `fix-review` | `pull_request.labeled` (label: `fix-review`) | `/fix-it` | Read review feedback and implement fixes via pr-fix plugin |
| `test-toolkit` | — | `/test` | Generic task via test-toolkit plugin |
| `generic` | — | `/agent` | Free-form request with natural language |

## Examples

### Filtered CI Workflow

Only trigger on CI failures on specific branches:

```yaml
fix-ci-develop:
  triggers:
    events:
      - event: workflow_job.completed
        filters:
          workflow_job.conclusion: "failure"
          workflow_job.head_branch: "develop"
  prompt:
    template: "/ci-failure-toolkit:fix-ci {repo} {issue_number}"
```

### Multi-Action Event

Respond to multiple actions on the same event:

```yaml
pr-updated:
  triggers:
    events:
      - event: pull_request.opened
      - event: pull_request.synchronize
      - event: pull_request.reopened
  prompt:
    template: "/pr-review-toolkit:review-pr {repo} {issue_number}"
```

### Mixed Filtered and Unfiltered Events

One workflow with both filtered and unfiltered event triggers:

```yaml
review-pr:
  triggers:
    events:
      - event: pull_request.opened
      - event: pull_request.labeled
        filters:
          label.name: ["review", "pr-review"]
    commands:
      - /review
  prompt:
    template: "/pr-review-toolkit:review-pr {repo} {issue_number}"
```

### Command Aliases

Multiple commands pointing to the same workflow:

```yaml
help:
  triggers:
    commands:
      - /help
      - /?
      - /docs
  prompt:
    template: "Provide help documentation for {repo}"
```

## Supported Events

The agent can handle any GitHub webhook event. Events with extraction rules (resolving issue number, ref, user):

| Event | Extracts | Notes |
|-------|----------|-------|
| `pull_request` | `issue_number`, `ref`, `user` | Ref resolves to `refs/pull/N/head` |
| `pull_request_review` | `issue_number`, `ref`, `user` | |
| `pull_request_review_comment` | `issue_number`, `ref`, `user` | |
| `pull_request_review_thread` | `issue_number`, `user` | |
| `issues` | `issue_number`, `user` | |
| `issue_comment` | `issue_number`, `user` | PR ref computed automatically when on a PR |
| `sub_issues` | `issue_number`, `user` | Parent issue number in extras |
| `discussion` | `issue_number`, `user` | |
| `discussion_comment` | `issue_number`, `user` | |
| `workflow_job` | `issue_number` (run_id), `ref` | CI extras |
| `workflow_run` | `issue_number` (run_id), `ref` | CI extras |
| `workflow_dispatch` | `ref`, `user` | Workflow path and inputs in extras |
| `check_run` | `ref` | CI extras |
| `check_suite` | `ref` | CI extras |
| `status` | `user` | Commit state/sha/context in extras |
| `release` | `ref` (tag) | Release extras |
| `push` | `ref`, `user` | |
| `create` | `ref`, `user` | Branch/tag creation |
| `delete` | `ref`, `user` | Branch/tag deletion |
| `commit_comment` | `user` | Commit ID in extras |
| `repository_dispatch` | `ref`, `user` | Client payload in extras |
| `deployment` | `ref`, `user` | |
| `deployment_status` | `ref`, `user` | |
| `gollum` | `user` | Wiki pages in extras |
| `merge_group` | `ref`, `user` | Merge queue, head_sha/base_ref in extras |
| `label` | `user` | Label name/color in extras |
| `milestone` | `user` | Milestone title in extras |
| `star` | `user` | |
| `watch` | `user` | |
| `fork` | `user` | Fork full name in extras |
| `member` | `user` | Member login in extras |
| `repository` | `user` | |
| `branch_protection_configuration` | `user` | |
| `branch_protection_rule` | `user` | Rule name in extras |
| `team` | `user` | |
| `organization` | `user` | |
| `installation` | `user` | |
| `installation_repositories` | `user` | |
| `ping` | `user` | |
| `package` | `user` | |
| `secret_scanning_alert` | `user` | Alert number in extras |
| `dependabot_alert` | `user` | Alert number in extras |
| `code_scanning_alert` | `user` | Alert number in extras |

Other events still work — they fall back to `sender.login` for user and `main` for ref. You can add extraction rules in `services/webhook/extraction_rules.py`.

## Troubleshooting

| Symptom | Check |
|---------|-------|
| Workflow not triggering | Verify event type and action in YAML, check worker logs |
| Wrong prompt | Verify template placeholders, check `prompts/` file exists |
| Event ignored | Normal for unhandled events. Logs show: `No workflow configured for event=...` |
| Bot responding to itself | Set `skip_self: true` or verify `WEBHOOK_BOT_USERNAME` matches the App |

## See Also

- [Architecture](ARCHITECTURE.md) - System design and internal routing
- [Configuration](CONFIGURATION.md) - Environment variables
- [Plugins](PLUGINS.md) - Plugin system
