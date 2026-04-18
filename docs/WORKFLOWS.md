# Workflows Guide

How to create and configure workflows — the rules that determine how the agent responds to GitHub events and commands.

Everything lives in a single file: `workflows.yaml`. No code changes needed.

## Workflow Structure

```yaml
my-workflow:
  description: "What this workflow does"
  triggers:
    events:
      - issues.opened
    commands:
      - /my-command
    filters:
      label.name: "bug"                    # optional, exact match or list
  prompt:
    template: "Do something with {repo} #{issue_number}"
    system_context: "my-context.md"        # optional, filename from prompts/ or inline
  context:
    repomap_budget: 4096                   # token budget for structural context
    personalized: true                     # personalize repomap toward changed files
    include_test_files: true               # include test files in personalization
    priority_focus: ["build_system"]       # focus areas for repomap ranking
  skip_self: true                          # default: true
```

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `triggers.events` | At least one of events/commands | `[]` | GitHub event triggers (`event_type.action`) |
| `triggers.commands` | At least one of events/commands | `[]` | Slash command triggers |
| `triggers.filters` | No | `{}` | Payload field filters. All must match (AND logic). Values can be a string or list |
| `prompt.template` | Yes | — | Prompt with placeholders (`{repo}`, `{issue_number}`, `{user_query}`) or plugin invocation |
| `prompt.system_context` | No | `None` | Agent instructions. Filename from `prompts/` or inline string |
| `context.repomap_budget` | No | `2048` | Token budget for the repomap |
| `context.personalized` | No | `false` | Personalize repomap toward changed files |
| `context.include_test_files` | No | `true` | Include test files in personalization |
| `context.priority_focus` | No | `[]` | Focus areas for repomap ranking (e.g. `build_system`, `test_structure`) |
| `description` | No | `""` | Human-readable description |
| `skip_self` | No | `true` | Skip events triggered by the bot itself |

## Triggers

### Event Triggers

Respond to GitHub webhook events using `event_type.action`:

```yaml
triggers:
  events:
    - pull_request.opened
    - issues.opened
    - workflow_job.completed
```

Some events have no action (e.g. `push`).

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

Restrict when a workflow triggers based on payload values. All filters must match (AND logic).

```yaml
# Only trigger on CI failures
triggers:
  events:
    - workflow_job.completed
  filters:
    workflow_job.conclusion: "failure"

# Only trigger for specific labels
triggers:
  events:
    - issues.labeled
  filters:
    label.name: ["bug", "security", "critical"]           # any of these values

# Combine multiple filters (all must match)
triggers:
  events:
    - workflow_job.completed
  filters:
    workflow_job.conclusion: "failure"
    workflow_job.head_branch: "develop"                    # failure AND on develop
```

If no `filters` are defined, the workflow always triggers on matching events.

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

## Creating a New Workflow

### 1. Add to `workflows.yaml`

```yaml
workflows:
  my-workflow:
    description: "What it does"
    triggers:
      events:
        - issues.opened
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
| `review-pr` | `pull_request.opened` | `/review`, `/pr-review`, `/review-pr` | Full PR review via pr-review-toolkit |
| `triage-issue` | `issues.opened` | `/triage`, `/triage-issue` | Triage with priority and complexity assessment |
| `fix-ci` | `workflow_job.completed` (failure only) | `/fix-ci`, `/fix-build`, `/fix-tests` | Analyze CI logs and push fix via ci-failure-toolkit |
| `triage-on-label` | `issues.labeled` (label: `triage`) | `/triage` | Triage triggered by label |
| `test-toolkit` | — | `/test` | Generic task via test-toolkit plugin |
| `generic` | — | `/agent` | Free-form request with natural language |

## Examples

### Filtered CI Workflow

Only trigger on CI failures on specific branches:

```yaml
fix-ci-develop:
  triggers:
    events:
      - workflow_job.completed
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
      - pull_request.opened
      - pull_request.synchronize
      - pull_request.reopened
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
