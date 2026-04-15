---
description: "Comprehensive PR review using specialized agents in worktree"
argument-hint: "[owner/repo] [pr-number] [review-aspects]"
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
   - Parse optional review aspects (comments, tests, errors, types, architecture, code, simplify, all)
   - Check git status to identify changed files: `git diff main --name-only`
   - Default: Run all applicable reviews

2. **Available Review Aspects:**
   - **comments** - Analyze code comment accuracy and maintainability
   - **tests** - Review test coverage quality and completeness
   - **errors** - Check error handling for silent failures
   - **types** - Analyze type design and invariants (if new types added)
   - **architecture** - Review architectural concerns, coupling, and patterns
   - **code** - General code review for project guidelines
   - **simplify** - Simplify code for clarity and maintainability
   - **all** - Run all applicable reviews (default)

3. **Identify Changed Files**
   - Run `git diff main --name-only` to see modified files in worktree
   - Run `git diff main --stat` for per-file change statistics
   - Agents can read files directly from the working directory
   - Identify file types and what reviews apply

   **Prefer local git commands over GitHub MCP for file lists and diffs.**
   GitHub MCP `get_files` and `get_diff` can produce very large responses (often 50KB+) that exceed tool output limits and get persisted to disk, requiring extra parsing steps and prone to errors. Instead:
   - Use `git diff main --name-only` instead of `pull_request_read(method="get_files")`
   - Use `git diff main` instead of `pull_request_read(method="get_diff")`
   - Use `git diff main --stat` instead of parsing file stats from MCP JSON

   GitHub MCP `pull_request_read(method="get")` is still needed for PR metadata (title, body, head SHA for review submission).

4. **Determine Applicable Reviews**

   **Core reviews (always run for any code changes):**
   - **code-reviewer** — Run for ALL PRs with code changes. Non-negotiable baseline review.

   **Standard reviews (run for most PRs unless trivial):**
   - **code-architecture-reviewer** — Run when the PR touches multiple files, adds new modules/classes, changes imports, modifies shared code, or introduces cross-cutting concerns. Skip only for trivial single-file fixes (typo, config value change, etc.).
   - **code-simplifier** — Run when the diff adds complex logic, nested conditionals, or duplicated patterns. Skip for simple, already-clean changes.

   **Conditional reviews (run when relevant):**
   - **pr-test-analyzer** — Run if test files changed OR if source code was added/modified without corresponding tests.
   - **silent-failure-hunter** — Run if the diff contains try-catch/try-except blocks, error handlers, fallback logic, or new error types.
   - **comment-analyzer** — Run if docstrings, comments, or documentation files were added/modified.
   - **type-design-analyzer** — Run if new classes/data models/types were added or existing ones modified.

   **Sizing heuristic:**
   - **Small PR** (<50 lines, 1-2 files): code-reviewer + any conditional reviews that match
   - **Medium PR** (50-300 lines, 3-8 files): code-reviewer + code-architecture-reviewer + code-simplifier + conditional reviews
   - **Large PR** (>300 lines or 8+ files): All applicable reviews — the complexity warrants full coverage

5. **Launch Review Agents: tool_name: Agent**

   **Parallel approach** (default):
   - Launch all agents simultaneously in a single message
   - Faster for comprehensive review
   - Results come back together
   - Each agent receives the list of changed files and the PR context

   **Sequential approach**:
   - Easier to understand and act on
   - Each report is complete before next
   - Good for interactive review
   - Agents read files directly from worktree

   **When passing context to agents:**
   - Always specify which files to focus on (from the diff)
   - Include the PR description/title if available
   - Note any specific concerns from the PR author
   - For architecture-reviewer, mention which modules/areas are affected

6. **Aggregate Results**

   After agents complete, organize findings:
   - **Critical Issues** (must fix before merge)
   - **Important Issues** (should fix)
   - **Suggestions** (nice to have)
   - **Positive Observations** (what's good)
   - **Architecture Notes** (design observations from architecture reviewer)

7. **Post Review to GitHub (Optional)**

   If GitHub MCP is available, post results:

   **Option A: Summary Comment Only**
   - Use `add_issue_comment` to post comprehensive summary

   ```markdown
   # PR Review Summary

   ## Review Scope
   - Agents run: [list of agents]
   - Files analyzed: [count]

   ## Critical Issues (X found)

   - [agent-name]: Issue description [file:line]

   ## Important Issues (X found)

   - [agent-name]: Issue description [file:line]

   ## Suggestions (X found)

   - [agent-name]: Suggestion [file:line]

   ## Architecture Notes

   - [architecture-reviewer observations]

   ## Strengths

   - What's well-done in this PR

   ## Recommended Action
   1. Fix critical issues first
   2. Address important issues
   3. Consider suggestions
   ```

   **Option B: Full Review with Inline Comments**
   - Create pending review: `pull_request_review_write(method="create")`
   - Add comments in a single parallel batch: include all `add_comment_to_pending_review()` calls (top 15-20 issues) in one message for efficiency
   - Submit review: `pull_request_review_write(method="submit_pending", event="COMMENT"/"REQUEST_CHANGES"/"APPROVE")`

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

/pr-review-toolkit:review-pr owner/repo 123 architecture code
# Reviews architecture and general code quality
```

**Parallel review:**

```
/pr-review-toolkit:review-pr owner/repo 123 all parallel
# Launches all agents in parallel
```

## Agent Descriptions:

**code-reviewer** (always run):
- Checks CLAUDE.md compliance
- Detects bugs and issues
- Reviews general code quality
- Scores issues by confidence (0-100)

**code-architecture-reviewer** (standard, most PRs):
- Reviews coupling and dependency direction
- Checks separation of concerns
- Validates pattern consistency with codebase
- Analyzes module boundaries and API design
- Uses codebase tools to gather broader context

**code-simplifier** (standard, non-trivial changes):
- Simplifies complex code
- Improves clarity and readability
- Applies project standards
- Preserves functionality

**pr-test-analyzer** (conditional, test/source changes):
- Reviews behavioral test coverage
- Identifies critical gaps
- Evaluates test quality

**silent-failure-hunter** (conditional, error handling):
- Finds silent failures
- Reviews catch blocks
- Checks error logging

**comment-analyzer** (conditional, docs/comments):
- Verifies comment accuracy vs code
- Identifies comment rot
- Checks documentation completeness

**type-design-analyzer** (conditional, types/models):
- Analyzes type encapsulation
- Reviews invariant expression
- Rates type design quality

## GitHub MCP Tools (Optional):

- `add_issue_comment` - Post summary comment to PR
- `pull_request_review_write` - Create/submit review with inline comments
- `add_comment_to_pending_review` - Add inline comments to pending review

## Tips:

- **Triggered by PR events**: This command runs when a PR is opened or updated
- **Focus on changes**: Agents analyze git diff by default but gather broader context when needed
- **Address critical first**: Fix high-priority issues before lower priority
- **Re-run after fixes**: Can be manually triggered again after pushing fixes
- **Use specific reviews**: Target specific aspects when you know the concern
- **Large persisted outputs**: When GitHub API responses (get_files, get_diff) are too large and get persisted to disk, use the Read tool on the persisted file path to examine the content. Do not use `cat | python3` inline scripts — they are fragile with JSON structures (e.g., added files may lack a `deletions` key) and waste turns on recoverable errors

## Workflow Integration:

**Automatic trigger:**

```
1. Developer creates/updates PR
2. Webhook triggers review command
3. Agents analyze changes in worktree + gather codebase context
4. Results posted as PR comments/review
```

**Manual trigger (via comment):**

```
1. Comment on PR: /pr-review-toolkit:review-pr owner/repo 123
2. Optionally specify aspects: /pr-review-toolkit:review-pr owner/repo 123 architecture tests
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
- Agents use codebase tools (find_definitions, find_references, search_codebase, read_file_summary) and semantic search to understand broader context beyond the diff
- Results are actionable with specific file:line references
- GitHub MCP tools are optional for posting results
- All agents available in `/agents` list
