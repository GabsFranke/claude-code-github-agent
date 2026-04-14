---
description: "Comprehensive PR review using specialized agents in worktree"
argument-hint: "[owner/repo] [pr-number] [review-aspects]"
allowed-tools: ["Task", "Bash", "Glob", "Grep", "Read", "mcp__github__*"]
---

# Comprehensive PR Review

Run a comprehensive pull request review using multiple specialized agents. Agents run in a git worktree with direct file access and use GitHub MCP to post results.

**Arguments:** "$ARGUMENTS"

- First argument: Repository (owner/repo format, required for posting review)
- Second argument: PR number (required for posting review)
- Additional arguments: Specific review aspects (optional)

## Review Workflow:

1. **Parse Arguments & Determine Review Scope**
   - Extract repository from $ARGUMENTS (e.g., "owner/repo")
   - Extract PR number from $ARGUMENTS
   - Parse optional review aspects (comments, tests, errors, types, code, simplify, all)
   - Check git status to identify changed files: `git diff main --name-only`
   - Default: Run all applicable reviews

2. **Available Review Aspects:**
   - **comments** - Analyze code comment accuracy and maintainability
   - **tests** - Review test coverage quality and completeness
   - **errors** - Check error handling for silent failures
   - **types** - Analyze type design and invariants (if new types added)
   - **code** - General code review for project guidelines
   - **simplify** - Simplify code for clarity and maintainability
   - **all** - Run all applicable reviews (default)

3. **Identify Changed Files**
   - Run `git diff main --name-only` to see modified files in worktree
   - Run `git diff --stat main` to get per-file addition/deletion counts (prefer this over parsing MCP API responses)
   - Agents can read files directly from the working directory
   - Identify file types and what reviews apply
   - **Prefer local git commands over MCP API for file listing and statistics** — MCP `get_files` returns large JSON that is hard to parse; `git diff --stat main` is faster and more reliable

4. **Determine Applicable Reviews**

   Based on changes:
   - **Always applicable**: code-reviewer (general quality)
   - **If test files changed**: pr-test-analyzer
   - **If comments/docs added**: comment-analyzer
   - **If error handling changed**: silent-failure-hunter
   - **If types added/modified**: type-design-analyzer
   - **After passing review**: code-simplifier (polish and refine)

5. **Launch Review Agents: tool_name: Agent**

   **Parallel approach** (default):
   - Launch all agents simultaneously
   - Faster for comprehensive review
   - Results come back together

   **Sequential approach**:
   - Easier to understand and act on
   - Each report is complete before next
   - Good for interactive review
   - Agents read files directly from worktree


6. **Aggregate Results**

   After agents complete, organize findings:
   - **Critical Issues** (must fix before merge)
   - **Important Issues** (should fix)
   - **Suggestions** (nice to have)
   - **Positive Observations** (what's good)

7. **Post Review to GitHub (Optional)**

   If GitHub MCP is available, post results:

   **Option A: Summary Comment Only**
   - Use `add_issue_comment` to post comprehensive summary

   ```markdown
   # PR Review Summary

   ## Critical Issues (X found)

   - [agent-name]: Issue description [file:line]

   ## Important Issues (X found)

   - [agent-name]: Issue description [file:line]

   ## Suggestions (X found)

   - [agent-name]: Suggestion [file:line]

   ## Strengths

   - What's well-done in this PR

   ## Recommended Action

   1. Fix critical issues first
   2. Address important issues
   3. Consider suggestions
   ```

   **Option B: Full Review with Inline Comments**
   - Create pending review: `pull_request_review_write(method="create")`
   - Add comments in PARALLEL BATCHES: issue multiple `add_comment_to_pending_review()` calls at once for top 15-20 issues
   - Submit review: `pull_request_review_write(method="submit_pending", event="COMMENT"/"REQUEST_CHANGES"/"APPROVE")`
   - **Use `get_diff` sparingly** — in worktree context, reading files directly with the Read tool is preferred. Only use `get_diff` when you need to see the actual diff markers (additions vs deletions).

   **If MCP not available:** Display results in console for manual review

## Usage Examples:

**Full review (default):**

```
/pr-review-toolkit:review-pr owner/repo 123
# Reviews PR #123 with all applicable agents
```

**Specific aspects:**

```
/pr-review-toolkit:review-pr owner/repo 123 tests errors
# Reviews only test coverage and error handling

/pr-review-toolkit:review-pr owner/repo 123 comments
# Reviews only code comments

/pr-review-toolkit:review-pr owner/repo 123 simplify
# Simplifies code after passing review
```

**Parallel review:**

```
/pr-review-toolkit:review-pr owner/repo 123 all parallel
# Launches all agents in parallel
```

## Agent Descriptions:

**comment-analyzer**:

- Verifies comment accuracy vs code
- Identifies comment rot
- Checks documentation completeness

**pr-test-analyzer**:

- Reviews behavioral test coverage
- Identifies critical gaps
- Evaluates test quality

**silent-failure-hunter**:

- Finds silent failures
- Reviews catch blocks
- Checks error logging

**type-design-analyzer**:

- Analyzes type encapsulation
- Reviews invariant expression
- Rates type design quality

**code-reviewer**:

- Checks CLAUDE.md compliance
- Detects bugs and issues
- Reviews general code quality

**code-simplifier**:

- Simplifies complex code
- Improves clarity and readability
- Applies project standards
- Preserves functionality

## GitHub MCP Tools (Optional):

- `add_issue_comment` - Post summary comment to PR
- `pull_request_review_write` - Create/submit review with inline comments
- `add_comment_to_pending_review` - Add inline comments to pending review

## Tips:

- **Triggered by PR events**: This command runs when a PR is opened or updated
- **Focus on changes**: Agents analyze git diff by default
- **Address critical first**: Fix high-priority issues before lower priority
- **Re-run after fixes**: Can be manually triggered again after pushing fixes
- **Use specific reviews**: Target specific aspects when you know the concern

## Workflow Integration:

**Automatic trigger:**

```
1. Developer creates/updates PR
2. Webhook triggers review command
3. Agents analyze changes in worktree
4. Results posted as PR comments/review
```

**Manual trigger (via comment):**

```
1. Comment on PR: /pr-review-toolkit:review-pr owner/repo 123
2. Optionally specify aspects: /pr-review-toolkit:review-pr owner/repo 123 security tests
3. Review results posted to PR
```

**After addressing feedback:**

```
1. Developer pushes fixes to PR branch
2. Can manually re-trigger review to verify fixes
3. Or wait for automatic trigger on push
```

## Notes:

- Agents run in git worktree with direct file system access
- Each agent focuses on its specialty for deep analysis
- Results are actionable with specific file:line references
- GitHub MCP tools are optional for posting results
- All agents available in `/agents` list
