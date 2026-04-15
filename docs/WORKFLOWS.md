# Workflows Guide

Complete guide to understanding and creating workflows in the Claude Code GitHub Agent.

## Overview

Workflows define how the agent responds to GitHub events and user commands. They are configured in a single YAML file (`workflows.yaml`) with no code changes required.

## Workflow Structure

Each workflow consists of:

- **name**: Human-readable workflow name
- **description**: What the workflow does
- **triggers**: Events and/or commands that activate the workflow
- **prompt**: Template and system context for the agent
- **skip_self**: Whether to skip events triggered by the bot itself (default: true)

## Example Workflow

```yaml
review-pr:
  description: "Comprehensive pull request review"
  triggers:
    events:
      - pull_request.opened
    commands:
      - /review
      - /pr-review
      - /review-pr
  prompt:
    template: "/pr-review-toolkit:review-pr {repo} {issue_number}"
    system_context: "review.md"
  skip_self: true # Don't review PRs created by the bot itself
```

## Triggers

### Event Triggers

Respond to GitHub webhook events using the format `event_type.action`:

```yaml
triggers:
  events:
    - pull_request.opened
    - pull_request.synchronize
```

**Common event formats:**

- `pull_request.opened` - PR opened
- `pull_request.synchronize` - PR updated with new commits
- `pull_request.closed` - PR closed
- `issues.opened` - Issue opened
- `issues.edited` - Issue edited
- `issue_comment.created` - Comment added to issue/PR
- `push` - Code pushed to repository (no action needed)
- `workflow_run.completed` - GitHub Actions workflow completed

### Command Triggers

Respond to `/command` in issue/PR comments:

```yaml
triggers:
  commands:
    - /review
    - /fix-ci
    - /agent
```

Commands are extracted from comment bodies using regex: `^(/\S+)\s*(.*)`

Note: Commands in YAML don't need quotes unless they contain special characters.

### Combined Triggers

A workflow can have both event and command triggers:

```yaml
triggers:
  events:
    - pull_request.opened
  commands:
    - /review
```

### Payload Filters

Filters let you restrict when a workflow triggers based on payload field values. Each filter is a `dot.path: expected_value` pair. All filters must match (AND logic).

**Single value filter:**

```yaml
triggers:
  events:
    - workflow_job.completed
  filters:
    workflow_job.conclusion: "failure"  # only trigger on failures
```

**Label-specific trigger:**

```yaml
triggers:
  events:
    - issues.labeled
  filters:
    label.name: "review"  # only when "review" label is added
```

**Multiple filters (all must match):**

```yaml
triggers:
  events:
    - workflow_job.completed
  filters:
    workflow_job.conclusion: "failure"
    workflow_job.head_branch: "develop"  # failure AND on develop branch
```

**List of accepted values:**

```yaml
triggers:
  events:
    - issues.labeled
  filters:
    label.name: ["bug", "security", "critical"]  # any of these labels
```

If a workflow has no `filters` section, it always triggers on matching events.

## Supported Events

The bot uses a payload extraction registry to support any GitHub event type. The following events have extraction rules defined:

| Event | Extracts | Notes |
|-------|----------|-------|
| `pull_request` | `issue_number`, `ref`, `user` | Ref resolves to `refs/pull/N/head` |
| `pull_request_review` | `issue_number`, `ref`, `user` | |
| `pull_request_review_comment` | `issue_number`, `ref`, `user` | |
| `pull_request_review_thread` | `issue_number`, `user` | Resolved/unresolved review threads |
| `issues` | `issue_number`, `user` | |
| `issue_comment` | `issue_number`, `user` | PR ref computed automatically when on a PR |
| `sub_issues` | `issue_number`, `user` | Parent issue number in extras |
| `discussion` | `issue_number`, `user` | |
| `discussion_comment` | `issue_number`, `user` | |
| `workflow_job` | `issue_number` (run_id), `ref`, CI extras | |
| `workflow_run` | `issue_number` (run_id), `ref`, CI extras | |
| `workflow_dispatch` | `ref`, `user` | Workflow path and inputs in extras |
| `check_run` | `ref`, CI extras | |
| `check_suite` | `ref`, CI extras | |
| `status` | `user` | Commit state/sha/context in extras |
| `release` | `ref` (tag), release extras | |
| `push` | `ref`, `user` | Used for cache warming |
| `create` | `ref`, `user` | Branch/tag creation, ref_type in extras |
| `delete` | `ref`, `user` | Branch/tag deletion, ref_type in extras |
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

Events not in this list still work -- they fall back to `sender.login` for the user and `main` for the ref. You can add extraction rules to `services/webhook/extraction_rules.py` to extract more fields.

## Skip Self

The `skip_self` option prevents infinite loops by ignoring events triggered by the bot itself.

### Default Behavior

By default, `skip_self: true` is applied to all workflows. This means:

- PRs created by the bot won't trigger automatic reviews
- Issues created by the bot won't trigger automatic triage
- CI failures on bot's PRs won't trigger automatic fixes (use `/fix-ci` command instead)

### Configuration

`skip_self` is optional and defaults to `true`. You can omit it entirely or set it explicitly:

```yaml
review-pr:
  # skip_self omitted - defaults to true
  triggers:
    events:
      - pull_request.opened
  # ... rest of workflow

fix-ci:
  skip_self: true # Explicitly set to true
  triggers:
    events:
      - workflow_job.completed
  # ... rest of workflow

generic:
  skip_self: false # Explicitly set to false - allow bot self-interaction
  triggers:
    commands:
      - /agent
  # ... rest of workflow
```

### How It Works

The webhook checks if the event was triggered by the bot by comparing:

- `sender.login` - Event sender
- `pull_request.user.login` - PR author
- `issue.user.login` - Issue author
- `comment.user.login` - Comment author

Against the configured `WEBHOOK_BOT_USERNAME` environment variable.

### When to Use skip_self: false

Use `skip_self: false` when:

- You want the bot to respond to its own commands (e.g., `/agent` in bot comments)
- You're building a workflow that should process bot-generated content
- You have external safeguards against infinite loops

### Example: Preventing Infinite Loops

**Without skip_self (dangerous):**

1. Bot creates PR
2. `pull_request.opened` event triggers review workflow
3. Bot reviews its own PR
4. Bot creates another PR based on review
5. Infinite loop! 🔄

**With skip_self: true (safe):**

1. Bot creates PR
2. `pull_request.opened` event received
3. Webhook checks: sender == bot username
4. Event ignored, no infinite loop ✓

### Manual Override

Even with `skip_self: true`, you can manually trigger workflows on bot PRs using commands:

```
/review  # Manually review bot's PR
/fix-ci  # Manually fix CI on bot's PR
```

Commands bypass the skip_self check when explicitly invoked by a human.

## Prompts

### Template

The template defines what gets sent to Claude. Use placeholders:

- `{repo}` - Repository full name (e.g., "owner/repo")
- `{issue_number}` - Issue or PR number
- `{user_query}` - User's query text (from command)

**Plugin invocation:**

```yaml
template: "/pr-review-toolkit:review-pr {repo} {issue_number}"
```

**Plain text:**

```yaml
template: "{user_query}"
```

**Mixed:**

```yaml
template: "Analyze {repo} PR #{issue_number}: {user_query}"
```

### System Context

System context provides instructions to the agent. Can be:

**Inline string:**

```yaml
system_context: "Focus on code quality, security, and performance"
```

**Markdown file:**

```yaml
system_context: "review.md"
```

The file should be in the `prompts/` directory.

### Prompt Building

The final prompt sent to Claude is built as:

```
{template} {system_context}. {user_query}
```

**Example:**

Template: `/pr-review-toolkit:review-pr owner/repo 123`
System context: `Focus on security`
User query: `check auth logic`

Final: `/pr-review-toolkit:review-pr owner/repo 123 Focus on security. check auth logic`

## Creating a New Workflow

### Step 1: Edit workflows.yaml

Add your workflow definition:

```yaml
workflows:
  fix-ci:
    description: "Analyze and fix CI failures"
    triggers:
      events:
        - workflow_run.completed
      commands:
        - /fix-ci
        - /fix-build
    prompt:
      template: "/fix-ci {repo}"
      system_context: "fix-ci.md"
```

### Step 2: Create System Context

Create `prompts/fix-ci.md`:

```markdown
# CI Failure Analysis

When analyzing CI failures:

1. Read the workflow logs
2. Identify the root cause
3. Propose specific fixes
4. Consider edge cases
5. Update tests if needed

Focus on:

- Build errors
- Test failures
- Linting issues
- Dependency problems
```

### Step 3: Restart Worker

```bash
docker-compose restart worker
```

The workflow is now active!

## Built-in Workflows

### review-pr

**Triggers:**

- Event: `pull_request.opened`
- Commands: `/review`, `/pr-review`, `/review-pr`

**Purpose:** Comprehensive PR review with code quality, security, and best practices analysis.

**Template:** `/pr-review-toolkit:review-pr {repo} {issue_number}`

### triage-issue

**Triggers:**

- Event: `issues.opened`
- Commands: `/triage`, `/triage-issue`

**Purpose:** Analyze and triage issues with labels and priority.

**Template:** `/pr-review-toolkit:triage-issue {repo} {issue_number}`

### generic

**Triggers:**

- Commands: `/agent`

**Purpose:** Handle generic agent requests without specific structure.

**Template:** `{user_query}`

## Advanced Examples

### Multi-Action Event

Respond to multiple actions on the same event:

```yaml
pr-updated:
  description: "Review PR on open or update"
  triggers:
    events:
      - pull_request.opened
      - pull_request.synchronize
      - pull_request.reopened
  prompt:
    template: "/pr-review-toolkit:review-pr {repo} {issue_number}"
    system_context: "review.md"
```

### Command Aliases

Multiple commands for the same workflow:

```yaml
help:
  description: "Provide help documentation"
  triggers:
    commands:
      - /help
      - /?
      - /docs
  prompt:
    template: "Provide help documentation for {repo}"
    system_context: "help.md"
```

### Context-Only Workflow

No template, just system context:

```yaml
explain:
  description: "Explain code in detail"
  triggers:
    commands:
      - /explain
  prompt:
    template: "{user_query}"
    system_context: |
      You are a code explainer. When asked to explain code:
      1. Read the relevant files
      2. Explain the purpose and logic
      3. Highlight important patterns
      4. Note potential issues
```

### Filtered Event Workflow

Only trigger on specific payload values:

```yaml
review-on-label:
  description: "Auto-review when issue is labeled 'review'"
  triggers:
    events:
      - issues.labeled
    filters:
      label.name: "review"
  prompt:
    template: "Issue #{issue_number} in {repo} was labeled for review. Review and provide feedback."
    system_context: "review.md"
```

### Multi-Filter CI Workflow

Only trigger on CI failures on specific branches:

```yaml
fix-ci-develop:
  description: "Fix CI failures on develop branch"
  triggers:
    events:
      - workflow_job.completed
    filters:
      workflow_job.conclusion: "failure"
      workflow_job.head_branch: "develop"
  prompt:
    template: "/ci-failure-toolkit:fix-ci {repo} {issue_number}"
```

## Workflow Routing

### How It Works

1. **Webhook** receives GitHub event
2. **Webhook** extracts: `event_type`, `action`, `command` (if present), `user_query`
3. **PayloadExtractor** resolves `issue_number`, `ref`, `user` from the payload
4. **Webhook** queues raw event data to Redis
5. **Worker** receives event data
6. **WorkflowEngine** routes:
   - If `command` present → find workflow with matching command trigger
   - Else → find workflow with matching event trigger
7. **WorkflowEngine** checks payload filters (if any defined)
8. If workflow found and filters match:
   - Trigger repo sync
   - Build prompt from template + context + query
   - Create job for sandbox execution
9. If no workflow found or filters don't match:
   - Log "No workflow configured" or "filters did not match"
   - Ignore event gracefully

### Routing Priority

1. **Commands** are checked first (if present)
2. **Events** are checked second
3. **First match wins** (order in YAML doesn't matter, but be specific)

## Best Practices

### Naming

- Use lowercase with hyphens: `review-pr`, `fix-ci`
- Be descriptive but concise
- Avoid special characters

### Commands

- Start with `/` (e.g., `/review`)
- Keep short and memorable
- Provide aliases for common variations
- Document in repository README

### System Context

- Be specific about what the agent should do
- Include examples when helpful
- Keep focused on the workflow's purpose
- Use markdown files for longer context

### Templates

- Use plugin invocations for structured tasks
- Use plain `{user_query}` for flexible requests
- Include necessary placeholders (`{repo}`, `{issue_number}`)
- Test with different inputs

## Troubleshooting

### Workflow Not Triggering

1. Check workflow name matches in YAML
2. Verify event type and action are correct
3. Check command starts with `/`
4. Restart worker after YAML changes
5. Check worker logs: `docker-compose logs worker`

### Wrong Prompt Generated

1. Verify template placeholders are correct
2. Check system context file exists in `prompts/`
3. Review WorkflowEngine logs for prompt building
4. Test with simple template first

### Event Ignored

This is normal for unhandled events. Check:

1. Is the event type in your triggers?
2. Is the action correct?
3. Worker logs will show: "No workflow configured for event=..."

## See Also

- [Architecture](ARCHITECTURE.md) - System design and workflow engine
- [Development](DEVELOPMENT.md) - Testing and contributing
- [Configuration](CONFIGURATION.md) - Environment variables
