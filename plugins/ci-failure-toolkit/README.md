# CI Failure Toolkit Plugin

Automated CI/CD failure analysis and fixing for GitHub Actions workflows using specialized agents with shared knowledge skills.

## Overview

This plugin provides intelligent CI failure analysis and automated fixes through a coordinated system of specialized agents. When a CI workflow fails, the toolkit analyzes logs, identifies the failure type, and delegates to expert agents who implement fixes in isolated git worktrees.

### GitHub Actions MCP Tools

The plugin includes an in-process MCP server that provides efficient access to GitHub Actions workflow data:

- `mcp__github-actions__get_workflow_run_summary` - High-level overview without logs (~1-2KB)
- `mcp__github-actions__get_job_logs_raw` - Paginated access to job logs (500 lines per call)
- `mcp__github-actions__search_job_logs` - Search patterns in logs (~2-10KB)
- `mcp__github-actions__get_failed_steps` - Extract failed steps with log excerpts (~5-20KB)

These tools use progressive access strategy to minimize context usage while providing comprehensive failure analysis.

#### When to Use Each Tool

**Decision Matrix:**

| Scenario                                  | Tool to Use                | Why                                                                     |
| ----------------------------------------- | -------------------------- | ----------------------------------------------------------------------- |
| Just started investigating                | `get_workflow_run_summary` | Get overview of all jobs, identify which failed (1-2KB)                 |
| Know which job failed, need to see errors | `get_failed_steps`         | Most efficient - extracts only failed steps with relevant logs (5-20KB) |
| Failed steps aren't enough context        | `get_job_logs_raw`         | Paginated access to full logs, read in 500-line chunks                  |
| Looking for specific error pattern        | `search_job_logs`          | Find specific patterns without reading entire log                       |
| Need to read very large logs              | `get_job_logs_raw`         | Paginate through logs in manageable chunks                              |

**Performance Characteristics:**

- `get_workflow_run_summary`: ~1-2KB, <1s response time
- `get_failed_steps`: ~5-20KB, 1-3s response time (includes log excerpt)
- `get_job_logs_raw`: ~10-50KB per call, 2-5s response time (depends on chunk size)
- `search_job_logs`: ~2-10KB, 2-5s response time (depends on matches)

**Recommended Workflow:**

1. Start with `get_workflow_run_summary` to identify failed jobs
2. Use `get_failed_steps` for the failed job - this gives you the error in most cases
3. If you need more context, use `search_job_logs` to find specific patterns
4. Only use `get_job_logs_raw` if you need to read the entire log (paginate through it)

## Architecture

### Three-Layer Design

```
┌─────────────────────────────────────────────────────────┐
│                  Command Layer                          │
│  (Orchestration - fetches data, delegates, posts)      │
│                   commands/fix-ci.md                    │
└────────────────────┬────────────────────────────────────┘
                     │ delegates to
                     ↓
┌─────────────────────────────────────────────────────────┐
│                   Agent Layer                           │
│  (Specialists - implement domain-specific fixes)        │
│  • build-failure-analyzer                               │
│  • test-failure-analyzer                                │
│  • lint-failure-analyzer                                │
└────────────────────┬────────────────────────────────────┘
                     │ uses
                     ↓
┌─────────────────────────────────────────────────────────┐
│                   Skills Layer                          │
│  (Shared knowledge injected into agents)                │
│  • git-worktree-workflow (git, GitHub, file ops)        │
│  • analyze-logs (log parsing)                           │
└─────────────────────────────────────────────────────────┘
```

### Command Layer (`commands/`)

**fix-ci.md** - Main orchestrator that:

- Fetches workflow failure information from GitHub using MCP tools
- Analyzes logs to identify failure types
- Delegates to specialized agents via Task tool
- Posts comprehensive results back to GitHub

**Key principle:** The orchestrator does NOT implement fixes. It coordinates.

### Agent Layer (`agents/`)

Specialized agents that implement fixes:

- **build-failure-analyzer** - Compilation errors, dependency issues, configuration problems
- **test-failure-analyzer** - Test failures, flaky tests, assertion errors, timeouts
- **lint-failure-analyzer** - Linting errors, type errors, formatting issues

**Key principle:** Each agent has the `git-worktree-workflow` skill loaded in their frontmatter, giving them essential knowledge about their environment and workflows.

### Skills Layer (`skills/`)

Shared knowledge injected into agents via frontmatter:

**git-worktree-workflow** - Essential knowledge about:

- **Environment**: You're in a git worktree, NOT a fresh clone
- **File operations**: How to use Read, Write, Edit, Bash tools
- **Git workflow**: Committing, pushing to current branch with `git push origin HEAD`
- **GitHub integration**: Using MCP tools (`mcp__github__*`), NOT `gh` CLI
- **PR creation**: Determining target branch (never hardcode "main")
- **Common workflows**: Fix → Test → Commit → Push → Post results

**analyze-logs** - Log parsing and failure classification patterns

## How It Works

### 1. Orchestration Flow

```
User/Webhook → fix-ci command
                    ↓
            Fetch GitHub logs (MCP)
                    ↓
            Analyze failure type
                    ↓
            Delegate to specialist agent (Task)
                    ↓
            Post results to GitHub (MCP)
```

### 2. Agent Execution Flow

```
Agent receives task with context
        ↓
Loads git-worktree-workflow skill
        ↓
Analyzes specific failure type
        ↓
Implements fixes (Read/Write/Edit)
        ↓
Tests locally (Bash)
        ↓
Commits: git add . && git commit -m "..."
        ↓
Pushes: git push origin HEAD
        ↓
Returns structured results
```

### 3. Git Worktree Environment

Agents operate in pre-configured worktrees:

- ✅ Repository already cloned (no `git clone` needed)
- ✅ Dedicated branch created (`fix/ci-failure-run-{id}-job-{id}`)
- ✅ Git credentials pre-configured
- ✅ Direct file system access
- ✅ Isolated from other jobs

**The `git-worktree-workflow` skill ensures agents understand this environment.**

## Usage

### Automatic Trigger

Configured via webhook to trigger on CI failures:

```yaml
# workflows.yaml
- name: ci-failure-auto-fix
  trigger:
    event: workflow_job
    conditions:
      - conclusion: failure
  action:
    command: /ci-failure-toolkit:fix-ci
    args: "{repo} {run_id}"
```

### Manual Trigger

Comment on a PR or issue:

```
/ci-failure-toolkit:fix-ci owner/repo 12345678
/ci-failure-toolkit:fix-ci owner/repo 12345678 test
/ci-failure-toolkit:fix-ci owner/repo 12345678 build
```

## Key Features

### Separation of Concerns

- **Orchestrator** (fix-ci command): Fetches data, analyzes, delegates, posts results
- **Specialists** (agents): Implement domain-specific fixes
- **Shared Knowledge** (skills): Common workflows and patterns

This prevents duplication and ensures consistency.

### Skills-Based Knowledge Sharing

Instead of duplicating git/GitHub instructions in every agent, we use the `git-worktree-workflow` skill:

```yaml
# In agent frontmatter
---
description: "Specialist in fixing build failures"
skills:
  - git-worktree-workflow
---
```

The skill content is automatically injected into the agent's context.

### GitHub MCP Integration

All GitHub interactions use MCP tools (documented in `git-worktree-workflow` skill):

- `mcp__github__list_workflow_run_jobs` - Get job details
- `mcp__github__download_workflow_run_logs` - Fetch logs
- `mcp__github__add_issue_comment` - Post comments
- `mcp__github__create_pull_request` - Create PRs

**The `gh` CLI is NOT available** - this is documented in the skill.

### Intelligent Delegation

The orchestrator analyzes logs and routes to the right specialist:

```python
if "compilation error" in logs or "build failed" in logs:
    → build-failure-analyzer
elif "test failed" in logs or "assertion error" in logs:
    → test-failure-analyzer
elif "lint error" in logs or "type error" in logs:
    → lint-failure-analyzer
```

## Configuration

### Required Environment Variables

```bash
# GitHub App credentials (for MCP)
GITHUB_APP_ID=123456
GITHUB_APP_PRIVATE_KEY="-----BEGIN RSA PRIVATE KEY-----..."
GITHUB_APP_INSTALLATION_ID=12345678

# Anthropic API
ANTHROPIC_API_KEY=sk-ant-...
```

### Workflow Integration

Add to `workflows.yaml`:

```yaml
workflows:
  - name: ci-failure-auto-fix
    trigger:
      event: workflow_job
      conditions:
        - conclusion: failure
    action:
      command: /ci-failure-toolkit:fix-ci
      args: "{repo} {run_id}"
```

## Development

### Adding New Failure Types

1. Create a new agent in `agents/`:

```markdown
---
description: "Specialist in [failure type]"
skills:
  - git-worktree-workflow
---

# [Failure Type] Analyzer

You are a [failure type] specialist...

**IMPORTANT:** You have the `git-worktree-workflow` skill loaded. This provides essential knowledge about:

- Your workspace environment (you're in a git worktree, NOT a fresh clone)
- How to use file operation tools (Read, Write, Edit, Bash)
- Git workflow (committing, pushing to current branch)
- GitHub integration (using MCP tools, NOT `gh` CLI)

Refer to that skill for all git and GitHub operations.

## Analysis Process:

...
```

2. Update `fix-ci.md` to route to the new agent:

```python
elif failure_type == "new-type":
    Task({
        "agent": "new-type-analyzer",
        "prompt": f"""Analyze and fix the {failure_type} failure in {repo}...

Instructions:
1. Analyze the failure
2. Implement fixes
3. Test locally
4. Commit and push
5. Return structured summary
"""
    })
```

### Adding New Skills

Create a new skill in `skills/`:

```markdown
---
name: "skill-name"
description: "What this skill provides"
---

# Skill Name

Knowledge and patterns for...

## Key Concepts

...

## Common Patterns

...

## Examples

...
```

Reference in agent frontmatter:

```yaml
skills:
  - git-worktree-workflow
  - skill-name
```

### Updating Existing Skills

When you update a skill (e.g., `git-worktree-workflow`), all agents that reference it automatically get the updated knowledge. No need to update each agent individually.

## Best Practices

### For Orchestrator (fix-ci command)

✅ Fetch comprehensive logs from GitHub
✅ Provide clear context to agents
✅ Don't implement fixes yourself - delegate
✅ Post detailed summaries to GitHub

❌ Don't duplicate agent logic
❌ Don't include git/GitHub instructions (that's in skills)

### For Agents

✅ Reference `git-worktree-workflow` skill in frontmatter
✅ Trust the skill's instructions
✅ Test fixes locally before committing
✅ Use descriptive commit messages
✅ Return structured results

❌ Don't duplicate git/GitHub workflows (use the skill)
❌ Don't try to clone the repository
❌ Don't use `gh` CLI (use MCP tools)
❌ Don't hardcode branch names (use `HEAD`)

### For Skills

✅ Keep focused on specific knowledge domains
✅ Provide clear examples
✅ Document common patterns
✅ Include "what NOT to do" sections

❌ Don't duplicate content across skills
❌ Don't include agent-specific logic

## Troubleshooting

### Agent tries to clone repository

**Problem:** Agent runs `git clone`

**Solution:** Ensure the agent has `git-worktree-workflow` skill in frontmatter:

```yaml
skills:
  - git-worktree-workflow
```

The skill explicitly states: "DO NOT clone the repository! You are already in a worktree."

### Agent uses `gh` CLI

**Problem:** Agent tries to use `gh` command

**Solution:** The `git-worktree-workflow` skill documents that only MCP tools are available. Ensure:

1. Agent has the skill loaded
2. Agent references the skill in their instructions

### Commits pushed to wrong branch

**Problem:** Agent pushes to `main` instead of current branch

**Solution:** The `git-worktree-workflow` skill documents using `git push origin HEAD`. Check:

1. Agent has the skill loaded
2. Agent follows the skill's git workflow section

### PR targets wrong base branch

**Problem:** PR created targeting `main` when it should target feature branch

**Solution:** The `git-worktree-workflow` skill has a section on "Determining the Target Branch for PRs" with logic to extract the source branch. Ensure agent follows this pattern.

### Orchestrator implements fixes instead of delegating

**Problem:** The fix-ci command tries to implement fixes itself

**Solution:** The command should only:

1. Fetch logs
2. Analyze failure type
3. Delegate to specialist agent via Task tool
4. Post results

Update the command to delegate properly.

## Output Format

The agent posts a comprehensive summary:

```markdown
## CI Failure Analysis - Run #12345

### Failure Type

Test failures

### Root Cause

Test expected old API response format after recent API changes

### Changes Made

- Updated test assertions to match new API format
- Updated test fixtures with current response structure
- Fixed 3 related tests in test_api.py

### Files Modified

- `tests/test_api.py` - Updated assertions for new API format
- `tests/fixtures/api_responses.json` - Updated fixture data

### Verification

All tests pass after fixes (ran 10 times to check for flakiness)

### Prevention

- Add API contract tests
- Update fixtures when API changes
- Use schema validation in tests

---

🤖 Analyzed and fixed by CI Failure Toolkit
```

## Why This Architecture?

### Problem: Duplication and Inconsistency

Previously, each agent had duplicated instructions about:

- Git worktree environment
- File operations
- Git commands
- GitHub MCP integration
- PR creation logic

This led to:

- Inconsistent behavior across agents
- Difficult maintenance (update in 3+ places)
- Agents not following best practices
- Orchestrator doing agent work

### Solution: Skills-Based Knowledge Sharing

Now:

- **One source of truth**: `git-worktree-workflow` skill
- **Automatic propagation**: Update skill → all agents get update
- **Clear separation**: Orchestrator coordinates, agents implement, skills provide knowledge
- **Consistency**: All agents follow same patterns

## License

Same as parent project.
