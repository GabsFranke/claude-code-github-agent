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

## Workflow Routing

### How It Works

1. **Webhook** receives GitHub event
2. **Webhook** extracts: `event_type`, `action`, `command` (if present), `user_query`
3. **Webhook** queues raw event data to Redis
4. **Worker** receives event data
5. **WorkflowEngine** routes:
   - If `command` present → find workflow with matching command trigger
   - Else → find workflow with matching event trigger
6. If workflow found:
   - Trigger repo sync
   - Build prompt from template + context + query
   - Create job for sandbox execution
7. If no workflow found:
   - Log "No workflow configured"
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
