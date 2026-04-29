# Plugins

Plugins bundle specialized agents, commands, and MCP servers into self-contained packages. They extend the agent's capabilities without modifying core code.

## Plugin Structure

```
plugin-name/
├── .claude-plugin/
│   └── plugin.json          # Required: plugin metadata
├── agents/                  # Optional: specialized agents (*.md)
├── commands/                # Optional: slash commands (*.md)
├── skills/                  # Optional: reusable workflows
└── hooks/                  # Optional: event hooks
```

Every plugin needs a `.claude-plugin/plugin.json`. Everything else is optional.

## Adding a Plugin

Create your plugin directory under `plugins/` and rebuild:

```bash
docker-compose build sandbox_worker
docker-compose up -d sandbox_worker
```

Plugins are auto-discovered at runtime — no code changes needed.

## Built-in Plugins

### pr-review-toolkit

PR review workflow with specialized agents.

- **Commands**: `/review-pr`
- **Agents**: code-reviewer, code-architecture-reviewer, code-simplifier, comment-analyzer, pr-test-analyzer, silent-failure-hunter, type-design-analyzer
- **Triggered by**: `pull_request.opened`, `/review`, `/pr-review`, `/review-pr`

### pr-fix

Implements fixes based on PR review feedback. Reads all review comments, deduplicates findings, and delegates implementation to subagents.

- **Commands**: `/fix-review`
- **Workflow**: `fix-review`
- **Key files**: `plugins/pr-fix/commands/fix-review.md`
- **Agents**: — (orchestrator pattern, delegates to Task tool agents)
- **Triggered by**: `pull_request.labeled` (label: `fix-review`, `fix-it`, `pr-fix`), `/fix-it`

### ci-failure-toolkit

CI failure analysis and auto-fix with specialized agents for different failure types.

- **Commands**: `/fix-ci`, `/fix-build`, `/fix-tests`
- **Agents**: `build-failure-analyzer`, `deploy-failure-analyzer`, `lint-failure-analyzer`, `test-failure-analyzer`
- **MCP Server**: GitHub Actions (stdio) — `get_job_logs`, `list_workflow_runs`, `get_workflow_run`, `get_workflow_run_jobs`
- **Triggered by**: `workflow_job.completed` (failure only), `/fix-ci`, `/fix-build`, `/fix-tests`

## Installing Plugins

The easiest way to add a plugin is via the Claude Code CLI:

```bash
/plugin install owner/repo --scope user
```

Plugins installed with `--scope user` are saved to `~/.claude/plugins/` and automatically discovered by the SDK at runtime. See the [Anthropic plugin docs](https://docs.anthropic.com/en/docs/claude-code/plugins) and [plugins reference](https://docs.anthropic.com/en/docs/claude-code/plugins-reference) for more.

## Creating a Custom Plugin

### 1. Create the directory

```bash
mkdir -p plugins/my-plugin/.claude-plugin
mkdir -p plugins/my-plugin/agents
mkdir -p plugins/my-plugin/commands
```

### 2. Add `plugin.json`

```json
{
  "name": "my-plugin",
  "version": "1.0.0",
  "description": "What this plugin does"
}
```

### 3. Add agents and commands

**Agent** (`agents/my-agent.md`):
```markdown
---
name: my-agent
description: "When to use this agent"
model: inherit
---

System prompt instructions for the agent...
```

**Command** (`commands/my-command.md`):
```markdown
---
argument-hint: "[repo] [issue-number]"
allowed-tools: ["Task", "mcp__github__*"]
---

Instructions for what the command should do...
```

### 4. Rebuild

```bash
docker-compose build sandbox_worker
docker-compose up -d sandbox_worker
```

Agents are available via the Task tool. Commands are invoked as `/my-plugin:my-command`.

## See Also

- [Workflows](WORKFLOWS.md) - How to create workflows that trigger plugins
- [Subagents](SUBAGENTS.md) - Subagent system
- [Architecture](ARCHITECTURE.md) - System design
