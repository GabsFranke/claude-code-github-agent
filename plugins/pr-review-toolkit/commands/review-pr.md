---
description: "Comprehensive PR review using specialized agents via GitHub MCP"
argument-hint: "[owner/repo] [pr-number] [review-aspects]"
allowed-tools: ["Task", "mcp__github__*"]
---

# Comprehensive GitHub PR Review

Run a comprehensive pull request review using multiple specialized agents via GitHub MCP tools.

**Arguments:** "$ARGUMENTS"
- First argument: Repository (owner/repo format, required)
- Second argument: PR number (required)
- Additional arguments: Specific review aspects (optional)

## Review Workflow:

1. **Parse Arguments**
   - Extract repository from $ARGUMENTS (e.g., "owner/repo")
   - Extract PR number from $ARGUMENTS
   - Parse optional review aspects (comments, tests, errors, types, code, simplify, all)
   - Default: Run all applicable reviews

2. **Available Review Aspects:**

   - **comments** - Analyze code comment accuracy and maintainability
   - **tests** - Review test coverage quality and completeness
   - **errors** - Check error handling for silent failures
   - **types** - Analyze type design and invariants (if new types added)
   - **code** - General code review for project guidelines
   - **simplify** - Simplify code for clarity and maintainability
   - **all** - Run all applicable reviews (default)

3. **Read PR via GitHub MCP**
   - Use `get_pull_request` to get PR details
   - Use `list_pull_request_files` to see changed files
   - Use `get_pull_request_diff` to analyze the actual changes
   - Identify file types and what reviews apply

4. **Determine Applicable Reviews**

   Based on changes:
   - **Always applicable**: code-reviewer (general quality)
   - **If test files changed**: pr-test-analyzer
   - **If comments/docs added**: comment-analyzer
   - **If error handling changed**: silent-failure-hunter
   - **If types added/modified**: type-design-analyzer
   - **After passing review**: code-simplifier (polish and refine)

5. **Launch Review Agents**

   **Parallel approach** (default):
   - Launch all agents simultaneously
   - Faster for comprehensive review
   - Results come back together

   **Sequential approach** (user can request):
   - Easier to understand and act on
   - Each report is complete before next
   - Good for interactive review


6. **Aggregate Results**

   After agents complete, organize findings:
   - **Critical Issues** (must fix before merge)
   - **Important Issues** (should fix)
   - **Suggestions** (nice to have)
   - **Positive Observations** (what's good)

7. **Post Review to GitHub**

   Use GitHub MCP tools to post results:
   
   1. **Summary Comment** (use `add_issue_comment`):
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
   4. Re-run review after fixes
   ```

   2. **Inline Comments** (if critical/important issues found):
   - Create pending review: `pull_request_review_write(method="create")`
   - Add comments SEQUENTIALLY: `add_comment_to_pending_review()` for top 15-20 issues
   - Submit review: `pull_request_review_write(method="submit_pending", event="COMMENT"/"REQUEST_CHANGES"/"APPROVE")`

## Usage Examples:

**Full review (default):**
```
/pr-review-toolkit:review-pr 123
# Reviews PR #123 with all applicable agents
```

**Specific aspects:**
```
/pr-review-toolkit:review-pr 123 tests errors
# Reviews only test coverage and error handling

/pr-review-toolkit:review-pr 123 comments
# Reviews only code comments

/pr-review-toolkit:review-pr 123 simplify
# Simplifies code after passing review
```

**Parallel review:**
```
/pr-review-toolkit:review-pr 123 all parallel
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

## GitHub MCP Tools Used:

- `get_pull_request` - Get PR metadata
- `list_pull_request_files` - List changed files
- `get_pull_request_diff` - Get the actual diff
- `add_issue_comment` - Post summary comment
- `pull_request_review_write` - Create/submit review
- `add_comment_to_pending_review` - Add inline comments

## Notes:

- Agents run autonomously and return detailed reports
- Each agent focuses on its specialty for deep analysis
- Results are actionable with specific file:line references
- All findings are posted back to GitHub automatically
- Supports both sequential and parallel agent execution
