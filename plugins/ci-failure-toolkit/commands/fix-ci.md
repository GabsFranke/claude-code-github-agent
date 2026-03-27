---
description: "Analyze and fix CI/CD failures using specialized agents"
argument-hint: "[owner/repo] [run-id-or-pr-number] [failure-type]"
skills:
  - git-worktree-workflow
  - python-code-quality
allowed-tools:
  [
    "Task",
    "Bash",
    "Glob",
    "Grep",
    "Read",
    "Write",
    "Edit",
    "List",
    "Search",
    "mcp__github__*",
    "mcp__github-actions__*",
  ]
---

# CI/CD Failure Analysis and Fix

Analyze GitHub Actions workflow failures and coordinate specialized agents to implement fixes. You orchestrate the entire process: create the branch, delegate work, and create the PR.

**Arguments:** "$ARGUMENTS"

- First argument: Repository (owner/repo format, required)
- Second argument: Workflow run ID or PR number (required)
- Third argument: Failure type (optional: build, test, lint, deploy, all)

**Context Variables Available:**

Your prompt includes a `## Workflow Failure Context` section with:

- `Run ID` - The workflow run ID to analyze
- `Head Branch` - The branch where the failure occurred (e.g., `feat/ci-fix`)
- `Target Branch for PR` - **Use this as the `base` when creating your PR**
- `Workflow Name`, `Failed Job`, `Conclusion` - Additional failure metadata

**CRITICAL:** The `Target Branch for PR` field in your context tells you exactly which branch to target with your fix PR. This is the branch where the CI failure occurred. Always use it — never default to `main` or `develop` unless it explicitly says so.

## Your Role as Orchestrator

You are the main coordinator with these responsibilities:

1. **Create a meaningful branch** for the fix work
2. Fetch workflow failure information from GitHub
3. Analyze logs to identify failure type
4. Delegate to specialized agents (they commit to your branch)
5. **Create the PR** with all fixes
6. Post comprehensive results to GitHub

**Key principles:**

- **YOU create the branch** - Use meaningful names like `fix/ci-failure-run-{run_id}`
- **YOU create the PR** - After all fixes are committed
- **Subagents implement fixes** - They work in your branch and commit their changes
- **YOU post the final summary** - Comprehensive results to GitHub

## Workflow

### Step 0: Create a Branch for This Work

**CRITICAL: Do this FIRST before any other work!**

The worktree starts in detached HEAD state (not on any branch). You must create a branch first.

```bash
# Create a unique branch name (use timestamp to avoid conflicts with existing worktrees)
timestamp=$(date +%s)
branch_name="fix/ci-failure-run-${run_id}-${timestamp}"

git checkout -b "$branch_name"

# Verify branch created (this will now show the branch name)
current_branch=$(git branch --show-current)
echo "Created branch: $current_branch"
```

**Why this matters:**

- The worktree starts in detached HEAD (no branch)
- `git checkout -b` creates a new branch from current HEAD
- Using timestamp ensures unique branch names (avoids conflicts with existing worktrees)
- Each job gets its own independent branch
- All subagents you invoke will work in this same worktree
- They will see your branch and commit to it
- This keeps all fixes organized in one branch
- You'll create a single PR from this branch at the end

**If you get "already used by worktree" error:**

- The branch name is already in use by another worktree
- This means another job is still running with that branch
- The timestamp suffix should prevent this, but if it happens, the job will fail
- This is expected behavior - each job should have a unique branch

### Step 1: Parse Arguments & Gather Context

Extract from $ARGUMENTS:

- Repository (owner/repo)
- Run ID or PR number
- Failure type filter (optional)

### Step 2: Fetch Workflow Failure Information

**Use the GitHub Actions MCP tools for efficient log access:**

The tools are available as MCP tools with the prefix `mcp__github-actions__`:

- `mcp__github-actions__get_workflow_run_summary`
- `mcp__github-actions__get_failed_steps`
- `mcp__github-actions__get_job_logs`
- `mcp__github-actions__search_job_logs`

**Step 2a: Get High-Level Summary (Always start here)**

Use the MCP tool to get workflow summary:

```python
from tools.github_actions import get_workflow_run_summary

summary = await get_workflow_run_summary(
    owner="owner-name",
    repo="repo-name",
    run_id="12345678"
)

# Returns: run metadata + job list with status (NO logs)
# Identify which jobs failed: look for conclusion="failure"
```

**Step 2b: Get Failed Steps (Most efficient for diagnosis)**

```python
from tools.github_actions import get_failed_steps

failed = await get_failed_steps(
    owner="owner-name",
    repo="repo-name",
    job_id="failed_job_id_from_summary",
    log_lines_per_step=100  # Last 100 lines per failed step
)

# Returns: Only failed steps with log excerpts
# This is usually enough to diagnose the issue
```

**Step 2c: Get Full Job Logs (Only if needed)**

```python
from tools.github_actions import get_job_logs

logs = await get_job_logs(
    owner="owner-name",
    repo="repo-name",
    job_id="failed_job_id",
    max_lines=500  # Optional: limit to last 500 lines
)

# Returns: Complete job logs
# Use only if failed steps aren't enough
```

**Step 2d: Search Logs (For specific errors)**

```python
from tools.github_actions import search_job_logs

matches = await search_job_logs(
    owner="owner-name",
    repo="repo-name",
    job_id="failed_job_id",
    pattern="error|exception|failed",  # Regex pattern
    context_lines=5  # Lines before/after match
)

# Returns: Only matching lines with context
# Useful for finding specific errors in long logs
```

**Recommended Flow:**

1. Start with `get_workflow_run_summary` - identify failed jobs
2. Use `get_failed_steps` - get failed step logs (usually sufficient)
3. Only use `get_job_logs` if you need more context
4. Use `search_job_logs` to find specific error patterns

### Step 3: Analyze Failure Type

Parse logs to identify:

- **Failure type**: build, test, lint, type-check, deploy
- **Error messages**: Key error text
- **Failed step**: Which CI step failed
- **Stack traces**: Full error context

**Failure Type Detection:**

**Build failures:**

- Keywords: "compilation error", "build failed", "cannot find module"
- Failed steps: "build", "compile", "install"

**Test failures:**

- Keywords: "test failed", "assertion error", "expected", "actual"
- Failed steps: "test", "pytest", "jest", "mocha"

**Lint failures:**

- Keywords: "lint error", "style violation", "formatting", "type error"
- Failed steps: "lint", "format", "style", "type-check", "mypy"

**Deploy failures:**

- Keywords: "deployment failed", "docker build", "container"
- Failed steps: "deploy", "docker", "push"

### Step 4: Delegate to Specialized Agent

**Use the Task tool to delegate to the appropriate agent.**

The agent will:

1. See your branch (they're in the same worktree)
2. Implement fixes
3. Commit to your branch
4. Push to your branch
5. Return results to you

**For Build Failures:**

```python
Task({
    "agent": "build-failure-analyzer",
    "prompt": f"""Analyze and fix the build failure in {repo}.

Workflow Run ID: {run_id}
Failed Job: {job_name}
Failed Step: {step_name}

Error Log:
{error_log_excerpt}

IMPORTANT: You are working in a shared worktree. The main agent has already created a branch for you.
- Current branch: {current_branch}
- DO NOT create a new branch
- Verify you're on the correct branch: git branch --show-current
- Commit your fixes to this branch
- Push when done: git push origin HEAD

Instructions:
1. Verify you're on the correct branch: git branch --show-current (should show {current_branch})
2. Analyze the build failure
3. Implement fixes using Read/Write/Edit tools
4. Test locally with Bash
5. Commit changes: git add . && git commit -m "fix: ..."
6. Push: git push origin HEAD
7. Return a structured summary of your fixes
"""
})
```

**For Test Failures:**

```python
Task({
    "agent": "test-failure-analyzer",
    "prompt": f"""Analyze and fix the test failures in {repo}.

Workflow Run ID: {run_id}
Failed Tests: {failed_test_names}

Error Log:
{error_log_excerpt}

IMPORTANT: You are working in a shared worktree. The main agent has already created a branch for you.
- Current branch: {current_branch}
- DO NOT create a new branch
- Verify you're on the correct branch: git branch --show-current
- Commit your fixes to this branch
- Push when done: git push origin HEAD

Instructions:
1. Verify you're on the correct branch: git branch --show-current (should show {current_branch})
2. Analyze the test failures
3. Implement fixes using Read/Write/Edit tools
4. Run tests locally to verify
5. Commit changes: git add . && git commit -m "fix: ..."
6. Push: git push origin HEAD
7. Return a structured summary of your fixes
"""
})
```

**For Lint/Type Failures:**

```python
Task({
    "agent": "lint-failure-analyzer",
    "prompt": f"""Analyze and fix the linting/type errors in {repo}.

Workflow Run ID: {run_id}
Linting Errors: {error_count}

Error Log:
{error_log_excerpt}

IMPORTANT: You are working in a shared worktree. The main agent has already created a branch for you.
- Current branch: {current_branch}
- DO NOT create a new branch
- Verify you're on the correct branch: git branch --show-current
- Commit your fixes to this branch
- Push when done: git push origin HEAD

Instructions:
1. Verify you're on the correct branch: git branch --show-current (should show {current_branch})
2. Run auto-fixers first (black, isort, ruff, etc.)
3. Fix remaining issues manually
4. Verify with linters
5. Commit changes: git add . && git commit -m "fix: ..."
6. Push: git push origin HEAD
7. Return a structured summary of your fixes
"""
})
```

### Step 5: Create Pull Request

**After the specialized agent completes and pushes their fixes, YOU create the PR.**

First, determine the target branch from the `## Workflow Failure Context` section in your prompt:

```
## Workflow Failure Context

- Run ID: 12345678
- Head Branch: feat/ci-fix          <-- the branch where CI failed
- Target Branch for PR: feat/ci-fix  <-- use this as `base` in create_pull_request
```

Read the `Target Branch for PR` value directly from that section. Do not guess, do not default to `main` or `develop` unless the field explicitly says so.

Then create the PR using GitHub MCP:

```python
current_branch = bash("git branch --show-current").strip()

# Read target branch from the "Workflow Failure Context" section injected into your prompt.
# It appears as: "- Target Branch for PR: feat/ci-fix"
# Parse it from the context — never default to "main" or "develop" unless stated explicitly.

mcp__github__create_pull_request({
    "owner": owner,
    "repo": repo,
    "title": f"Fix CI failure from run #{run_id}",
    "body": f"""## CI Failure Analysis - Run #{run_id}

### Failure Type
{failure_type}

### Root Cause
{agent_result.root_cause}

### Changes Made
{format_changes_list(agent_result.fixes_applied)}

### Files Modified
{format_files_list(agent_result.fixes_applied)}

### Verification
{agent_result.verification}

### Prevention Recommendations
{format_prevention_list(agent_result.prevention)}

---
🤖 Automated fix by CI Failure Toolkit

**Workflow Run:** https://github.com/{owner}/{repo}/actions/runs/{run_id}
**Fixed by:** {agent_name}
**Target Branch:** {target_branch}
""",
    "head": current_branch,
    "base": target_branch  # Use the ref from context, not hardcoded "main"
})
```

````

### Step 6: Post Summary Comment

After creating the PR, post a summary comment:

```python
mcp__github__add_issue_comment({
    "owner": owner,
    "repo": repo,
    "issue_number": pr_number,
    "body": f"""## ✅ CI Failure Fixed

The CI failure from run #{run_id} has been analyzed and fixed.

### Summary
- **Failure Type:** {failure_type}
- **Root Cause:** {agent_result.root_cause}
- **Files Modified:** {len(agent_result.fixes_applied)}
- **Branch:** `{current_branch}`

See the PR description for full details.

---
🤖 CI Failure Toolkit
"""
})
````

## Available Specialized Agents

- **build-failure-analyzer** - Compilation errors, dependencies, configuration
- **test-failure-analyzer** - Test failures, flaky tests, assertions
- **lint-failure-analyzer** - Linting, formatting, type errors

All agents have the `git-worktree-workflow` skill and know how to:

- Work in a shared worktree
- Commit to the branch you created
- Push their changes
- Return structured results

## Key Tools Available

### GitHub Actions Tools (Progressive Access)

1. **get_workflow_run_summary(owner, repo, run_id)**
   - High-level overview with job list (no logs)
   - Use FIRST to identify failed jobs
   - Fast and token-efficient

2. **get_failed_steps(owner, repo, job_id, log_lines_per_step=100)**
   - Only failed steps with log excerpts
   - Most efficient for diagnosis
   - Usually sufficient to fix issues

3. **get_job_logs(owner, repo, job_id, max_lines=None)**
   - Full logs for specific job
   - Use if failed steps aren't enough
   - Can limit to last N lines

4. **search_job_logs(owner, repo, job_id, pattern, context_lines=5)**
   - Find specific patterns in logs
   - Returns matches with context
   - Useful for long logs

### GitHub MCP Tools

- `create_pull_request` - Create PR (YOU do this)
- `add_issue_comment` - Post comments

## Important Notes

- **You create the branch first** - Before delegating to agents
- **Agents commit to your branch** - They work in the same worktree
- **You create the PR** - After all fixes are done
- **GitHub MCP only** - Use `mcp__github__*` tools, NOT `gh` CLI
- **Delegate with context** - Provide error logs and clear instructions
- **Trust the agents** - They have the `git-worktree-workflow` skill

## Complete Example Flow

```python
from tools.github_actions import (
    get_workflow_run_summary,
    get_failed_steps,
    get_job_logs,
    search_job_logs
)

# 0. Read target branch from the "Workflow Failure Context" section in your prompt
# Look for: "- Target Branch for PR: <branch>"
echo "Repository: $repo"

# 1. Create unique branch from detached HEAD (use timestamp to avoid conflicts)
timestamp=$(date +%s)
branch_name="fix/ci-failure-run-${run_id}-${timestamp}"
git checkout -b "$branch_name"
current_branch=$(git branch --show-current)
echo "Created branch: $current_branch"

# 2. Fetch workflow summary (fast, no logs)
summary = await get_workflow_run_summary(owner, repo, run_id)

# 3. Identify failed jobs
failed_jobs = [j for j in summary["jobs"] if j["conclusion"] == "failure"]
print(f"Found {len(failed_jobs)} failed jobs")

# 4. Get failed steps for first failed job (efficient)
failed_steps = await get_failed_steps(owner, repo, failed_jobs[0]["id"])

# 5. Analyze failure type from failed steps
failure_type = analyze_failure_type(failed_steps)  # "test"

# 6. Delegate to specialist with failed step logs
result = Task({
    "agent": "test-failure-analyzer",
    "prompt": f"""Fix test failures in {repo}.

Branch: {current_branch}
Failed Steps: {failed_steps}

Instructions:
1. Verify branch: git branch --show-current
2. Implement fixes
3. Commit: git add . && git commit -m "fix: ..."
4. Push: git push origin HEAD
"""
})

# 7. Agent commits and pushes to your branch
# (happens automatically in the agent)

# 8. Determine target branch from the "Workflow Failure Context" in the prompt
# Read "- Target Branch for PR: <branch>" — do NOT default to main/develop

# 9. Create PR
pr = mcp__github__create_pull_request({
    "head": current_branch,
    "base": target_branch,
    "title": f"Fix CI failure from run #{run_id}",
    "body": f"""## CI Failure Analysis - Run #{run_id}

### Failure Type
{failure_type}

### Root Cause
{result.root_cause}

### Changes Made
{result.fixes_applied}

---
🤖 Automated fix by CI Failure Toolkit
"""
})

# 10. Post summary
mcp__github__add_issue_comment({
    "issue_number": pr.number,
    "body": "✅ CI failure fixed!"
})
```

## Usage Examples

**Auto-triggered on workflow failure:**

```
# Webhook receives workflow_job.completed with conclusion=failure
# Automatically triggers: /ci-failure-toolkit:fix-ci owner/repo 12345
```

**Manual trigger:**

```
/ci-failure-toolkit:fix-ci owner/repo 12345
/ci-failure-toolkit:fix-ci owner/repo 12345 test
```

## Summary

Your workflow as orchestrator:

1. ✅ Create branch: `git checkout -b fix/ci-failure-run-{run_id}`
2. ✅ Fetch logs from GitHub
3. ✅ Analyze failure type
4. ✅ Delegate to specialist agent
5. ✅ Agent commits to your branch
6. ✅ Create PR: `mcp__github__create_pull_request`
7. ✅ Post summary comment

Remember: You coordinate, agents implement, you create the PR!
