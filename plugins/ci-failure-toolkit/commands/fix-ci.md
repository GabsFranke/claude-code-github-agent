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

Analyze GitHub Actions workflow failures and implement fixes using specialized agents. Agents run in a git worktree with direct file access and use GitHub MCP to interact with GitHub.

**Arguments:** "$ARGUMENTS"

- First argument: Repository (owner/repo format, required)
- Second argument: Workflow run ID or PR number (required)
- Third argument: Failure type (optional: build, test, lint, deploy, all)

## Workflow:

### 1. Parse Arguments & Gather Context

Extract from $ARGUMENTS:

- Repository (owner/repo)
- Run ID or PR number
- Failure type filter (optional)

### 2. Fetch Workflow Failure Information

You already have the run_id from the Workflow Failure Context injected into your prompt.

Use GitHub MCP tools to get detailed failure information:

**Get job details and identify which step failed:**

```python
mcp__github__list_workflow_run_jobs(owner, repo, run_id)
# Returns: jobs array with name, conclusion, steps (each step has name, conclusion, number)
```

**Download complete logs with error messages:**

```python
mcp__github__download_workflow_run_logs(owner, repo, run_id)
# Returns: Full log output as text with all error messages and stack traces
```

Parse the logs to identify:

- Which job failed
- Which step in that job failed
- The actual error message
- Stack traces if available

### 3. Analyze Failure Logs

Parse logs to identify:

- **Failure type**: build, test, lint, type-check, deploy
- **Error messages**: Extract key error text
- **Failed step**: Which CI step failed
- **Stack traces**: Full error context
- **Exit codes**: Process exit status

### 4. Analyze and Implement Fixes

Based on the failure type, implement appropriate fixes:

**For Build Failures:**

- Fix compilation errors, missing imports, syntax issues
- Update dependencies in requirements.txt/package.json
- Fix configuration files

**For Test Failures:**

- Fix test logic errors and assertions
- Update test expectations if API/behavior changed
- Fix flaky tests (race conditions, timeouts)
- Run tests locally to verify: `pytest tests/` or `npm test`

**For Lint/Type Failures:**

- Use auto-fixers first: `black .`, `isort .`, `ruff check --fix .`
- Add missing type annotations
- Fix import order and unused imports
- Run linters to verify fixes

**For Deploy Failures:**

- Fix Dockerfile syntax and paths
- Update docker-compose.yml environment variables
- Fix health checks and resource limits
- Test locally: `docker build -t test .`

### 5. Commit and Push Changes

After fixes are implemented:

```bash
# Stage changes
git add .

# Commit with descriptive message
git commit -m "fix: resolve CI failure - [brief description]

- Root cause: [explanation]
- Changes: [what was fixed]
- Tested: [how it was verified]

Fixes workflow run #${run_id}"

# Push to branch
git push origin HEAD
```

### 6. Post Results to GitHub

Use GitHub MCP to communicate results:

**Option A: Comment on PR**

```python
mcp__github__add_issue_comment(
    owner=owner,
    repo=repo,
    issue_number=pr_number,
    body=summary_comment
)
```

**Option B: Create new PR with fixes**

```python
# Create branch
mcp__github__create_branch(owner, repo, f"fix/ci-failure-{run_id}", base_branch)

# Push commits (already done in step 7)

# Create PR
mcp__github__create_pull_request(
    owner=owner,
    repo=repo,
    title=f"Fix CI failure from run #{run_id}",
    body=detailed_description,
    head=f"fix/ci-failure-{run_id}",
    base="main"
)
```

### 7. Summary Format

Post a comprehensive summary:

```markdown
# CI Failure Analysis - Run #${run_id}

## Failure Type

${failure_type}

## Root Cause

${root_cause_explanation}

## Changes Made

- ${change_1}
- ${change_2}
- ${change_3}

## Files Modified

- `${file_1}` - ${description}
- `${file_2}` - ${description}

## Verification

${how_fix_was_tested}

## Prevention

${recommendations_to_prevent_recurrence}

---

🤖 Analyzed and fixed by CI Failure Toolkit
```

## Failure Type Detection

Auto-detect failure type from logs:

**Build failures:**

- Keywords: "compilation error", "build failed", "cannot find module"
- Exit codes: 1, 2
- Failed steps: "build", "compile", "install"

**Test failures:**

- Keywords: "test failed", "assertion error", "expected", "actual"
- Exit codes: 1
- Failed steps: "test", "pytest", "jest", "mocha"

**Lint failures:**

- Keywords: "lint error", "style violation", "formatting"
- Exit codes: 1
- Failed steps: "lint", "format", "style"

**Type failures:**

- Keywords: "type error", "mypy", "typescript error"
- Exit codes: 1
- Failed steps: "type-check", "mypy", "tsc"

**Deploy failures:**

- Keywords: "deployment failed", "docker build", "container"
- Exit codes: 1, 125, 126, 127
- Failed steps: "deploy", "docker", "push"

## Usage Examples:

**Auto-triggered on workflow failure:**

```
# Webhook receives workflow_job.completed with conclusion=failure
# Automatically triggers: /ci-failure-toolkit:fix-ci owner/repo 12345
```

**Manual trigger with run ID:**

```
/ci-failure-toolkit:fix-ci owner/repo 12345
# Analyzes workflow run #12345 and fixes issues
```

**Manual trigger with PR number:**

```
/ci-failure-toolkit:fix-ci owner/repo 456
# Finds failed workflow runs for PR #456 and fixes
```

**Specific failure type:**

```
/ci-failure-toolkit:fix-ci owner/repo 12345 test
# Only analyzes test failures

/ci-failure-toolkit:fix-ci owner/repo 12345 build
# Only analyzes build failures
```

## Key GitHub MCP Tools:

- `list_workflow_run_jobs` - Get job details and identify failed steps
- `download_workflow_run_logs` - Fetch complete error logs
- `add_issue_comment` - Post analysis results to PR/issue
- `create_branch` - Create fix branch (if needed)
- `create_pull_request` - Open PR with fixes (if needed)

## Tips:

- **Auto-triggered**: Runs automatically on CI failures
- **Root cause focus**: Fix underlying issues, not symptoms
- **Test locally**: Verify fixes before pushing
- **Clear commits**: Descriptive commit messages with context
- **Prevention**: Suggest improvements to prevent recurrence

## Workflow Integration:

**Automatic trigger:**

```
1. CI workflow fails
2. GitHub sends workflow_job.completed webhook
3. Agent analyzes logs and identifies failure
4. Specialized agent implements fix
5. Changes committed and pushed
6. Summary posted to PR/issue
```

**Manual trigger:**

```
1. Developer comments: /fix-ci owner/repo 12345
2. Agent fetches workflow logs
3. Analyzes failure and implements fix
4. Posts results to GitHub
```

## Notes:

- Agents run in git worktree with direct file system access
- Each agent specializes in specific failure types
- Fixes are tested locally before committing
- GitHub MCP handles all GitHub interactions
- All agents available in `/agents` list
