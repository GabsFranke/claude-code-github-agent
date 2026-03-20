---
description: "Analyze and fix CI/CD failures using specialized agents"
argument-hint: "[owner/repo] [run-id-or-pr-number] [failure-type]"
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
  ]
---

# CI/CD Failure Analysis and Fix

Analyze GitHub Actions workflow failures and coordinate specialized agents to implement fixes. You orchestrate the entire process: create the branch, delegate work, and create the PR.

**Arguments:** "$ARGUMENTS"

- First argument: Repository (owner/repo format, required)
- Second argument: Workflow run ID or PR number (required)
- Third argument: Failure type (optional: build, test, lint, deploy, all)

**Context Variables Available:**

You have access to these variables from your job context:

- `ref` - The git ref where the failure occurred (e.g., "refs/heads/feature-branch" or "main")
- `event_data` - Full event data including run_id, workflow_name, job_name, conclusion
- `repo` - Repository name (owner/repo)
- `issue_number` - PR or issue number if applicable

**CRITICAL:** Use `ref` to determine the target branch for your PR. This is the branch where the CI failure occurred.

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

```bash
# Create a meaningful branch name
git checkout -b fix/ci-failure-run-{run_id}

# Verify branch created
git branch --show-current
# Output: fix/ci-failure-run-12345
```

**Why this matters:**

- All subagents you invoke will work in this same worktree
- They will see your branch and commit to it
- This keeps all fixes organized in one branch
- You'll create a single PR from this branch at the end

### Step 1: Parse Arguments & Gather Context

Extract from $ARGUMENTS:

- Repository (owner/repo)
- Run ID or PR number
- Failure type filter (optional)

### Step 2: Fetch Workflow Failure Information

**Use GitHub MCP tools, NOT `gh` CLI (not available)**

Get job details and identify which step failed:

```python
mcp__github__list_workflow_run_jobs({
    "owner": "owner-name",
    "repo": "repo-name",
    "run_id": 12345678
})
```

Download complete logs:

```python
mcp__github__download_workflow_run_logs({
    "owner": "owner-name",
    "repo": "repo-name",
    "run_id": 12345678
})
```

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

IMPORTANT: You are working in a shared worktree. The main agent has already created a branch for you:
- Current branch: {current_branch}
- DO NOT create a new branch
- Commit your fixes to this branch
- Push when done: git push origin HEAD

Instructions:
1. Verify you're on the correct branch: git branch --show-current
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

IMPORTANT: You are working in a shared worktree. The main agent has already created a branch for you:
- Current branch: {current_branch}
- DO NOT create a new branch
- Commit your fixes to this branch
- Push when done: git push origin HEAD

Instructions:
1. Verify you're on the correct branch: git branch --show-current
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

IMPORTANT: You are working in a shared worktree. The main agent has already created a branch for you:
- Current branch: {current_branch}
- DO NOT create a new branch
- Commit your fixes to this branch
- Push when done: git push origin HEAD

Instructions:
1. Verify you're on the correct branch: git branch --show-current
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

First, determine the target branch (where the failure occurred):

```bash
# The ref is provided in your job context - this is the branch where the failure occurred
# Extract it from the event data or job context

# Option 1: From job_data.ref (preferred - this is what the worktree was created from)
# The ref format is like "refs/heads/feature-branch" or just "feature-branch"
target_branch="${ref}"

# Clean up the ref format if needed
if [[ "$target_branch" == refs/heads/* ]]; then
    target_branch="${target_branch#refs/heads/}"
fi

# Option 2: From event_data if available
if [ -z "$target_branch" ]; then
    target_branch=$(echo "$event_data" | jq -r '.head_branch // .ref' | sed 's|refs/heads/||')
fi

# Option 3: Fallback to main only if nothing else works
if [ -z "$target_branch" ] || [ "$target_branch" == "null" ]; then
    target_branch="main"
fi

echo "Target branch for PR: $target_branch"
```

**CRITICAL:** The `ref` variable in your context tells you which branch the worktree was created from. This is the branch where the CI failure occurred, and it's where your PR should target.

Then create the PR using GitHub MCP:

```python
current_branch = bash("git branch --show-current").strip()

# Use the ref from job context - this is the branch where the failure occurred
target_branch = ref.replace("refs/heads/", "") if ref else "main"

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

## Key GitHub MCP Tools

- `list_workflow_run_jobs` - Get job details
- `download_workflow_run_logs` - Fetch logs
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

```bash
# 0. Check context variables
echo "Working on ref: $ref"
echo "Repository: $repo"

# 1. Create branch
git checkout -b fix/ci-failure-run-12345

# 2. Fetch logs from GitHub (MCP)
logs = mcp__github__download_workflow_run_logs(...)

# 3. Analyze failure type
failure_type = analyze_logs(logs)  # "test"

# 4. Delegate to specialist
result = Task({
    "agent": "test-failure-analyzer",
    "prompt": f"Fix test failures. Branch: {current_branch}. Logs: {logs}"
})

# 5. Agent commits and pushes to your branch
# (happens automatically in the agent)

# 6. Determine target branch from context
target_branch = ref.replace("refs/heads/", "") if ref else "main"

# 7. Create PR
pr = mcp__github__create_pull_request({
    "head": "fix/ci-failure-run-12345",
    "base": target_branch,  # Use ref from context, NOT hardcoded "main"
    "title": "Fix CI failure from run #12345",
    "body": result.summary
})

# 8. Post summary
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
