# PR Review Toolkit

A comprehensive collection of specialized agents for thorough pull request review, covering architecture, code quality, test coverage, error handling, type design, code comments, and code simplification.

## Overview

This plugin bundles 7 expert review agents that each focus on a specific aspect of code quality. All agents are instructed to gather context from the broader codebase using codebase tools and semantic search — not just the PR diff — to provide feedback grounded in the actual codebase patterns.

Use them individually for targeted reviews or together via the `/review-pr` command for comprehensive PR analysis.

## Agents

### 1. code-reviewer
**Focus**: General code review for project guidelines and bug detection

**Analyzes:**
- CLAUDE.md compliance
- Style violations
- Bug detection (logic errors, null handling, race conditions, security)
- Code quality issues

**When to use:** Always — this is the baseline review for any PR with code changes.

**Triggers:**
```
"Review my recent changes"
"Check if everything looks good"
"Review this code before I commit"
```

### 2. code-architecture-reviewer
**Focus**: Architectural soundness and pattern consistency

**Analyzes:**
- Component coupling and dependency direction
- Separation of concerns and module boundaries
- Pattern consistency with the broader codebase
- API design and extensibility

**When to use:** For most non-trivial PRs — especially multi-file changes, new modules, or changes to shared code. Skip only for trivial single-file fixes.

**Triggers:**
```
"Review the architecture of this change"
"Check if this fits our existing patterns"
"Analyze the coupling in this PR"
```

### 3. code-simplifier
**Focus**: Code simplification and clarity

**Analyzes:**
- Code clarity and readability
- Unnecessary complexity and nesting
- Redundant code and abstractions
- Consistency with project standards
- Overly compact or clever code

**When to use:** After writing or modifying non-trivial code. Run when the diff adds complex logic, nested conditionals, or duplicated patterns.

**Triggers:**
```
"Simplify this code"
"Make this clearer"
"Refine this implementation"
```

**Note**: This agent preserves functionality while improving code structure and maintainability.

### 4. pr-test-analyzer
**Focus**: Test coverage quality and completeness

**Analyzes:**
- Behavioral vs line coverage
- Critical gaps in test coverage
- Test quality and resilience
- Edge cases and error conditions

**When to use:**
- After creating a PR
- When adding new functionality
- To verify test thoroughness

**Triggers:**
```
"Check if the tests are thorough"
"Review test coverage for this PR"
"Are there any critical test gaps?"
```

### 5. silent-failure-hunter
**Focus**: Error handling and silent failures

**Analyzes:**
- Silent failures in catch blocks
- Inadequate error handling
- Inappropriate fallback behavior
- Missing error logging

**When to use:**
- After implementing error handling
- When reviewing try/catch blocks
- Before finalizing PRs with error handling

**Triggers:**
```
"Review the error handling"
"Check for silent failures"
"Analyze catch blocks in this PR"
```

### 6. comment-analyzer
**Focus**: Code comment accuracy and maintainability

**Analyzes:**
- Comment accuracy vs actual code
- Documentation completeness
- Comment rot and technical debt
- Misleading or outdated comments

**When to use:**
- After adding documentation
- Before finalizing PRs with comment changes
- When reviewing existing comments

**Triggers:**
```
"Check if the comments are accurate"
"Review the documentation I added"
"Analyze comments for technical debt"
```

### 7. type-design-analyzer
**Focus**: Type design quality and invariants

**Analyzes:**
- Type encapsulation (rated 1-10)
- Invariant expression (rated 1-10)
- Type usefulness (rated 1-10)
- Invariant enforcement (rated 1-10)

**When to use:**
- When introducing new types
- During PR creation with data models
- When refactoring type designs

**Triggers:**
```
"Review the UserAccount type design"
"Analyze type design in this PR"
"Check if this type has strong invariants"
```

## Context-Aware Reviews

All agents are designed to gather context beyond the PR diff using codebase tools:

- **`find_definitions`** — Locate where symbols are defined
- **`find_references`** — Trace how symbols are used across the codebase
- **`search_codebase`** — Regex search for patterns, utilities, and conventions
- **`read_file_summary`** — Get file API surfaces without reading full implementations
- **`semantic_search`** — Conceptual search for related code by meaning

This means agents evaluate your PR against the actual codebase patterns, not just theoretical best practices.

## Usage Patterns

### Comprehensive PR Review (Recommended)

Use the `/review-pr` command for automatic agent selection based on PR size and content:

```
/pr-review-toolkit:review-pr owner/repo 123              # Full review
/pr-review-toolkit:review-pr owner/repo 123 tests errors  # Specific aspects
/pr-review-toolkit:review-pr owner/repo 123 all parallel   # Parallel mode
```

The command automatically:
- **Always** runs `code-reviewer` for any code changes
- Runs `code-architecture-reviewer` and `code-simplifier` for medium/large PRs
- Conditionally runs specialized agents based on what changed

### Individual Agent Usage

Simply ask questions that match an agent's focus area, and Claude will automatically trigger the appropriate agent:

```
"Can you check if the tests cover all edge cases?"
→ Triggers pr-test-analyzer

"Review the error handling in the API client"
→ Triggers silent-failure-hunter

"I've added documentation - is it accurate?"
→ Triggers comment-analyzer
```

### Proactive Review

Claude may proactively use these agents based on context:

- **After writing code** → code-reviewer
- **For multi-file changes** → code-architecture-reviewer
- **After adding docs** → comment-analyzer
- **Before creating PR** → Multiple agents as appropriate
- **After adding types** → type-design-analyzer

## Installation

Install from your personal marketplace:

```bash
/plugins
# Find "pr-review-toolkit"
# Install
```

Or add manually to settings if needed.

## Agent Details

### Confidence Scoring

Agents provide confidence scores for their findings:

**code-reviewer**: Scores issues 0-100 (reports >= 80). Critical: 90-100, Important: 80-89.

**code-architecture-reviewer**: Scores concerns 0-100 (reports >= 70). Emphasizes evidence from the actual codebase.

**code-simplifier**: Identifies complexity and suggests simplifications.

**pr-test-analyzer**: Rates test gaps 1-10 (10 = critical, must add).

**silent-failure-hunter**: Flags severity of error handling issues (CRITICAL/HIGH/MEDIUM).

**type-design-analyzer**: Rates 4 dimensions on 1-10 scale.

**comment-analyzer**: Identifies issues with high confidence in accuracy checks.

### Output Formats

All agents provide structured, actionable output:
- Clear issue identification
- Specific file and line references
- Explanation of why it's a problem
- Suggestions for improvement
- Prioritized by severity

## Best Practices

### Recommended Review Flow

**For any PR with code changes:**

1. **code-reviewer** — Always. Catches bugs, style issues, and guideline violations.
2. **code-architecture-reviewer** — For multi-file changes or new modules. Ensures changes fit the codebase.
3. **code-simplifier** — When the diff has complex logic. Improves clarity before merge.

**Conditionally (based on content):**

4. **pr-test-analyzer** — If source changed without tests, or tests were added.
5. **silent-failure-hunter** — If error handling was added/modified.
6. **comment-analyzer** — If documentation or comments were added.
7. **type-design-analyzer** — If new types or data models were introduced.

### Running Multiple Agents

You can request multiple agents to run in parallel or sequentially:

**Parallel** (faster):
```
"Run code-reviewer and code-architecture-reviewer in parallel"
```

**Sequential** (when one informs the other):
```
"First review architecture, then simplify the code"
```

## Tips

- **Be specific**: Target specific agents for focused review
- **Use proactively**: Run before creating PRs, not after
- **Address critical first**: Agents prioritize findings
- **Iterate**: Run again after fixes to verify
- **Trust the context**: Agents gather codebase context to avoid false positives

## Troubleshooting

### Agent Not Triggering

**Issue**: Asked for review but agent didn't run

**Solution**:
- Be more specific in your request
- Mention the agent type explicitly
- Reference the specific concern (e.g., "test coverage")

### Agent Analyzing Wrong Files

**Issue**: Agent reviewing too much or wrong files

**Solution**:
- Specify which files to focus on
- Reference the PR number or branch
- Mention "recent changes" or "git diff"

## Integration with Workflow

This plugin works great with:
- **ci-failure-toolkit**: Analyze CI failures before or after review
- **Project-specific agents**: Combine with your custom agents

**Recommended workflow:**
1. Write code → **code-reviewer** (always)
2. Multi-file changes → **code-architecture-reviewer**
3. Fix issues → **silent-failure-hunter** (if error handling)
4. Add tests → **pr-test-analyzer**
5. Document → **comment-analyzer**
6. Review passes → **code-simplifier** (polish)
7. Create PR

## Contributing

Found issues or have suggestions? These agents are maintained in:
- User agents: `~/.claude/agents/`
- Project agents: `.claude/agents/` in claude-cli-internal

## License

Apache 2.0

---

**Quick Start**: Just ask for review and the right agent will trigger automatically!
