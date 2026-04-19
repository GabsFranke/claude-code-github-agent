---
description: "Read PR review feedback and implement fixes using subagent delegation"
argument-hint: "[owner/repo] [pr-number]"
---

# Fix PR Review Feedback

Read all review feedback from a PR, build a fix plan, delegate implementation to agents, and create a PR with the fixes.

**Arguments:** "$ARGUMENTS"

- First argument: Repository (owner/repo format, required)
- Second argument: PR number (required)

## Your Role as Orchestrator

You coordinate the entire process. Your responsibilities:

1. **Create a branch** for the fix work
2. Fetch all review feedback from the PR
3. Parse and deduplicate findings
4. **DELEGATE implementation to agents** (they do ALL the coding)
5. Run quality checks
6. **Create the PR** targeting the original PR's branch
7. Post summary comment

**Key principles:**

- **YOU create the branch** — use a meaningful name like `fix/review-{pr_number}`
- **YOU gather and analyze** — understand all feedback before delegating
- **YOU NEVER implement fixes yourself** — always delegate to agents
- **Agents implement ALL fixes** — they work in your branch and commit changes
- **YOU create the PR** — after all fixes are committed by agents
- **YOU post the final summary** — comprehensive results to GitHub
- **`gh` CLI is NOT available** — use `mcp__github__*` tools exclusively

## Workflow

### Step 0: Create a Branch for This Work

**CRITICAL: Do this FIRST before any other work!**

The worktree starts in detached HEAD state. Create a branch first:

```bash
timestamp=$(date +%s)
branch_name="fix/review-${pr_number}-${timestamp}"

git checkout -b "$branch_name"

# Verify
current_branch=$(git branch --show-current)
echo "Created branch: $current_branch"
```

All agents you invoke will work in this same branch.

### Step 1: Parse Arguments

Extract from $ARGUMENTS:
- Repository (owner/repo)
- PR number

### Step 2: Fetch PR Metadata

Get the PR details to understand what we're working with:

```
mcp__github__pull_request_read(method="get", owner=<owner>, repo=<repo>, pull_number=<pr_number>)
```

From this response, extract:
- `head.ref` — the PR's feature branch (this is the **target** for our fix PR)
- `head.sha` — the head commit SHA
- `title` and `body` — PR description context
- `changed_files` — number of files changed
- `additions` / `deletions` — size of changes

**CRITICAL:** Store `head.ref` — this is the branch our fix PR will target as `base`. Never default to `main` or `develop`.

### Step 3: Fetch All Review Feedback

Gather feedback from every source. Call all three in parallel:

**3a: Submitted Reviews**

```
mcp__github__pull_request_read(method="get_reviews", owner=<owner>, repo=<repo>, pull_number=<pr_number>)
```

Each review has: `body` (review body text), `state` (APPROVE/COMMENT/REQUEST_CHANGES), `user.login`.

**3b: Inline Review Comments**

```
mcp__github__pull_request_read(method="get_review_comments", owner=<owner>, repo=<repo>, pull_number=<pr_number>)
```

Each comment has: `path` (file), `line` (line number), `body` (comment text), `diff_hunk` (context). These are the most actionable — they point to specific code.

**3c: PR Conversation Comments**

```
mcp__github__pull_request_read(method="get_comments", owner=<owner>, repo=<repo>, pull_number=<pr_number>)
```

General conversation comments on the PR. May contain feedback, suggestions, or context.

### Step 4: Parse Findings

From all feedback sources, extract actionable findings. Structure each finding as:

| Field | Source |
|-------|--------|
| `file` | `path` from inline comment, or extracted from body text |
| `line` | `line` from inline comment, or estimated from context |
| `severity` | Inferred from review state and language |
| `source` | `user.login` of the reviewer |
| `issue` | The problem described |
| `suggestion` | Suggested fix (if provided) |

**Severity classification:**

- **CRITICAL** — Reviews with `state: REQUEST_CHANGES`, or comments containing words like "bug", "broken", "error", "security", "vulnerability", "crash"
- **IMPORTANT** — General feedback, suggestions with clear code changes, "should fix", "needs to be"
- **SUGGESTION** — Comments with "nit", "suggestion", "consider", "optional", "style"

**Deduplication:**

Group findings by `(file, line)`. If multiple reviewers mention the same issue at the same location, keep the most detailed one and note it was mentioned by multiple reviewers.

**If no actionable findings are found:**

Post a comment and stop:

```
mcp__github__add_issue_comment({
    owner: <owner>,
    repo: <repo>,
    issue_number: <pr_number>,
    body: "## Review Fix Check\n\nNo actionable review feedback found. All comments appear to be informational or already addressed."
})
```

Then exit. Do NOT create an empty PR.

### Step 5: Build Fix Plan and Delegate

Organize findings into logical groups for delegation. Group related findings that touch the same files or area of code.

**Delegation strategy:**

Launch one or more agents via the `Agent` tool. Each agent receives:
- The list of findings to address
- The file paths and line numbers
- The suggested fixes
- Instructions to implement, verify, commit, and push

**Example agent invocation:**

```
Agent({
    description: "Implement review fixes",
    prompt: `You are fixing review feedback in ${repo} PR #${pr_number}.

Current branch: ${current_branch}
Target: implement fixes and commit+push to this branch.

## Findings to Address

${formatted_findings_table}

## Instructions

1. Read each file mentioned in the findings
2. Understand the current code at the referenced lines
3. Implement the suggested fix (or a better solution if the suggestion is incomplete)
4. For CRITICAL findings: verify the fix doesn't break related code
5. Run quality checks if applicable:
   - bash ./check-code.sh (if the repo has one)
   - Or: black --check, isort --check, ruff check (Python)
   - Or: npm run lint, npm test (Node.js)
6. Commit ALL changes with a descriptive message
7. Push to origin

## Important

- Work ONLY in the current branch: ${current_branch}
- Do NOT create a new branch
- Do NOT create a PR (the orchestrator handles that)
- Preserve existing functionality — fixes should be minimal and targeted
- If a finding is invalid (e.g., the code is already correct), skip it and note why
- Use mcp__github__ tools for any GitHub operations, NEVER gh CLI`
})
```

**Batching guidance:**

- For 1-5 findings: single agent invocation with all findings
- For 6-15 findings: split by file area (e.g., backend vs frontend, or service A vs service B)
- For 15+ findings: split into batches of ~8, grouped by file proximity
- Each batch becomes one agent invocation — agents run in parallel when independent

### Step 6: Verify Quality

After all agents complete:

```bash
# Check what changed
git diff --stat HEAD~<N>  # where N is the number of agent commits

# If the repo has a quality check script
bash ./check-code.sh

# Verify branch is clean
git status
```

If quality checks fail, do NOT create the PR. Instead:
1. Post a comment explaining what failed
2. Attempt to fix via another agent invocation with the quality errors as context
3. If the fix succeeds, continue to PR creation

### Step 7: Create the Fix PR

Create a PR targeting the original PR's head branch:

```
mcp__github__create_pull_request({
    owner: <owner>,
    repo: <repo>,
    title: `Fix review feedback for PR #${pr_number}`,
    body: `## Review Feedback Fixes for PR #${pr_number}

### Summary
Addresses ${total_findings} findings from PR review feedback.

### Findings Addressed

#### Critical (${critical_count})
${critical_list}

#### Important (${important_count})
${important_list}

#### Suggestions (${suggestion_count})
${suggestion_list}

### Changes Made
${changes_summary}

### Files Modified
${files_list}

### Quality Checks
${quality_check_results}

---
Automated fix by PR Fix Toolkit
**Original PR:** #${pr_number}
**Target Branch:** ${head_ref}`,
    head: "${current_branch}",
    base: "${head_ref}"
})
```

### Step 8: Post Summary on Original PR

After creating the fix PR, post a summary comment on the original PR:

```
mcp__github__add_issue_comment({
    owner: <owner>,
    repo: <repo>,
    issue_number: <pr_number>,
    body: `## Review Fixes Implemented

A fix PR has been created addressing the review feedback.

### Summary
- **Findings addressed:** ${total_findings}
- **Critical:** ${critical_count}
- **Important:** ${important_count}
- **Suggestions:** ${suggestion_count}
- **Files modified:** ${files_count}
- **Fix PR:** #${fix_pr_number}

### What Was Fixed
${fix_summary}

---
Automated by PR Fix Toolkit`
})
```

## Error Handling

| Scenario | Action |
|----------|--------|
| No actionable findings | Post informational comment, exit without PR |
| Quality checks fail after fix | Attempt one more fix round; if still failing, post comment with errors |
| Agent fails to implement | Post comment listing which findings couldn't be fixed and why |
| Branch push fails | Post error comment; do not retry automatically |

## GitHub MCP Tools Used

| Tool | Purpose |
|------|---------|
| `pull_request_read(method="get")` | PR metadata (head branch, title) |
| `pull_request_read(method="get_reviews")` | Submitted reviews |
| `pull_request_read(method="get_review_comments")` | Inline review comments |
| `pull_request_read(method="get_comments")` | PR conversation comments |
| `create_pull_request` | Create the fix PR |
| `add_issue_comment` | Post summary on original PR |

## Tips

- Fetch all review data BEFORE starting any implementation
- Parse findings carefully — the quality of fixes depends on understanding the feedback
- Deduplicate aggressively — don't fix the same issue twice
- Group related findings for delegation — a single agent can fix related issues more coherently
- If review feedback is contradictory, prioritize REQUEST_CHANGES over suggestions
- Always verify the fix PR targets the correct branch (the PR's head, not main)
