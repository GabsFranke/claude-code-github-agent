# PR Review Flow with Plugin System

## Overview

When a pull request is opened, the system performs a comprehensive multi-agent review using the PR Review Toolkit plugin, which provides specialized review agents for different aspects of code quality.

## Flow Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                    PR Opened on GitHub                       │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                   Webhook Receives Event                     │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│              Message Published to Redis Queue                │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│           Worker Picks Up Message (auto_review=true)         │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│         Main Agent Invokes Plugin Command                    │
│         /pr-review-toolkit:review-pr [repo] [pr#] all        │
│         (PR Review Toolkit Plugin Loaded)                    │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│          Read PR and Analyze Changes (Step 1)                │
│          - Get PR diff via GitHub MCP                        │
│          - Assess scope and type of changes                  │
│          - Decide which plugin agents are needed             │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
         ┌───────────────┴───────────────┐
         │  Spawn Plugin Review Agents   │
         │  (0-6 agents based on need)   │
         └───────────────┬───────────────┘
                         │
         ┌───────────────┼───────────────┬───────────────┬───────────────┬───────────────┐
         │               │               │               │               │               │
         ▼               ▼               ▼               ▼               ▼               ▼
┌─────────────┐ ┌─────────────┐ ┌─────────────┐ ┌─────────────┐ ┌─────────────┐ ┌─────────────┐
│   Comment   │ │  PR Test    │ │   Silent    │ │Type Design  │ │    Code     │ │    Code     │
│  Analyzer   │ │  Analyzer   │ │  Failure    │ │  Analyzer   │ │  Reviewer   │ │ Simplifier  │
│ (if needed) │ │ (if needed) │ │   Hunter    │ │ (if needed) │ │  (always)   │ │ (optional)  │
└──────┬──────┘ └──────┬──────┘ └──────┬──────┘ └──────┬──────┘ └──────┬──────┘ └──────┬──────┘
       │               │               │               │               │               │
       │ Comment       │ Test          │ Error         │ Type          │ General       │ Code
       │ Accuracy      │ Coverage      │ Handling      │ Encapsulation │ Quality       │ Simplification
       │ Documentation │ Quality       │ Silent        │ Invariants    │ Bugs          │ Clarity
       │ Maintainability│ Gaps         │ Failures      │ Design        │ Standards     │ Readability
       │               │               │               │               │               │
       └───────────────┴───────────────┴───────────────┴───────────────┴───────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│          Coordinator Collects JSON Results                   │
│          - Parses findings from each subagent                │
│          - Prioritizes by severity (Critical > High > Med)   │
│          - Groups by category                                │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│              Post Summary Comment (Step 1)                   │
│              - Overall assessment                            │
│              - Findings by category                          │
│              - Issue counts by severity                      │
│              - Positive notes                                │
│              Tool: add_issue_comment                         │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
                    ┌────┴────┐
                    │ Issues? │
                    └────┬────┘
                         │
              ┌──────────┴──────────┐
              │                     │
              ▼ Yes                 ▼ No
┌─────────────────────────┐  ┌──────────────┐
│  Create Pending Review  │  │   Done ✓     │
│  (Step 2a)              │  └──────────────┘
└────────┬────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────────┐
│         Add Inline Comments Sequentially (Step 2b)           │
│         - Top 15-20 priority issues                          │
│         - Format: **[Severity] [Category]**: Issue           │
│         - Include explanation and suggestion                 │
│         Tool: add_comment_to_pending_review (sequential)     │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│              Submit Review (Step 2c)                         │
│              - event: COMMENT or REQUEST_CHANGES             │
│              - Brief summary                                 │
│              Tool: pull_request_review_write                 │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│              Developer Sees Review on GitHub                 │
│              - Summary comment in conversation               │
│              - Inline comments on specific lines             │
└─────────────────────────────────────────────────────────────┘
```

## Plugin Review Agents

The PR Review Toolkit plugin provides six specialized review agents:

### 1. Comment Analyzer
```json
{
  "findings": [
    {
      "file": "src/services/payment.ts",
      "line": 42,
      "severity": "medium",
      "category": "comment-accuracy",
      "issue": "Comment doesn't match implementation",
      "explanation": "Comment says 'validates user' but code validates payment",
      "suggestion": "Update comment to reflect actual behavior"
    }
  ],
  "summary": "Comment quality assessment",
  "comment_rot_detected": true,
  "documentation_gaps": ["Missing API documentation"]
}
```

### 2. PR Test Analyzer
```json
{
  "findings": [
    {
      "file": "src/api/users.ts",
      "line": 15,
      "severity": "high",
      "category": "test-coverage",
      "issue": "Critical path not tested",
      "explanation": "Error handling path has no test coverage",
      "suggestion": "Add test case for error scenario",
      "test_type": "behavioral"
    }
  ],
  "summary": "Test coverage assessment",
  "critical_gaps": 2,
  "coverage_quality": "medium"
}
```

### 3. Silent Failure Hunter
```json
{
  "findings": [
    {
      "file": "src/utils/parser.ts",
      "line": 28,
      "severity": "critical",
      "category": "silent-failure",
      "issue": "Exception caught but not logged",
      "explanation": "Catch block swallows error without logging",
      "suggestion": "Add logger.error() in catch block"
    }
  ],
  "summary": "Error handling assessment",
  "silent_failures_found": 3,
  "risk_level": "high"
}
```

### 4. Type Design Analyzer
```json
{
  "findings": [
    {
      "file": "src/models/User.ts",
      "line": 10,
      "severity": "medium",
      "category": "type-design",
      "issue": "Type doesn't enforce invariants",
      "explanation": "Email field allows invalid formats",
      "suggestion": "Use branded type or validation in constructor",
      "design_principle": "encapsulation"
    }
  ],
  "summary": "Type design quality assessment",
  "design_rating": 7,
  "encapsulation_score": "good"
}
```

### 5. Code Reviewer (General Quality)
```json
{
  "findings": [
    {
      "file": "src/components/Form.tsx",
      "line": 55,
      "severity": "medium",
      "category": "code-quality",
      "issue": "Function violates single responsibility",
      "explanation": "Function handles validation, formatting, and submission",
      "suggestion": "Extract validation and formatting to separate functions"
    }
  ],
  "summary": "General code quality assessment",
  "claude_md_compliance": true,
  "positive_notes": ["Good TypeScript types", "Clear naming"]
}
```

### 6. Code Simplifier (Polish & Refine)
```json
{
  "findings": [
    {
      "file": "src/utils/helpers.ts",
      "line": 20,
      "severity": "low",
      "category": "simplification",
      "issue": "Complex nested conditionals",
      "explanation": "Logic can be simplified with early returns",
      "suggestion": "Use guard clauses to reduce nesting",
      "before": "if (x) { if (y) { return z; } }",
      "after": "if (!x) return; if (!y) return; return z;"
    }
  ],
  "summary": "Code simplification opportunities",
  "complexity_reduced": true
}
```

## Benefits

1. **Plugin-Based Architecture**: Modular, reusable review agents via plugin system
2. **Intelligent Delegation**: Only uses agents that are relevant to the changes
3. **Comprehensive Coverage**: Six specialized agents cover all aspects of code quality
4. **Efficient Reviews**: Small PRs get quick reviews, complex PRs get thorough analysis
5. **Focused Expertise**: Each agent specializes in their domain
6. **Structured Output**: JSON format enables easy parsing and prioritization
7. **Consistent Quality**: Same thorough review process for similar changes
8. **Actionable Feedback**: Specific suggestions with code examples
9. **Cost Effective**: Doesn't waste tokens on unnecessary reviews
10. **GitHub MCP Integration**: Direct access to PR data via GitHub's official MCP server

## Plugin System

The PR Review Toolkit is loaded as a plugin:

```python
# In worker.py
options = ClaudeAgentOptions(
    plugins=[{"type": "local", "path": "/app/plugins/pr-review-toolkit"}],
    allowed_tools=["Task", "mcp__github__*"],
    # ... other options
)
```

The plugin provides:
- **Command**: `/pr-review-toolkit:review-pr` - Main review orchestrator
- **Agents**: 6 specialized review agents (comment-analyzer, pr-test-analyzer, etc.)
- **GitHub MCP Tools**: Direct integration with GitHub API

## Customization

Adjust review behavior via repository's `CLAUDE.md`:

```markdown
# Agent Instructions

For code reviews:
- Prioritize security findings above all else
- Be extra strict on error handling in payment module
- Ignore style issues in legacy code (src/legacy/)
- Always check for proper TypeScript types
```

## Performance

- **Plugin load time**: ~100ms (one-time per session)
- **Agent spawn time**: ~2-5 seconds per agent
- **Parallel execution**: All applicable agents run simultaneously
- **Analysis time**: Depends on PR size (1-5 minutes typical)
- **Total review time**: 2-8 minutes for most PRs

## Available Review Aspects

You can request specific review aspects:

```bash
# Full review (default)
/pr-review-toolkit:review-pr owner/repo 123 all

# Specific aspects
/pr-review-toolkit:review-pr owner/repo 123 comments tests
/pr-review-toolkit:review-pr owner/repo 123 errors types
/pr-review-toolkit:review-pr owner/repo 123 simplify
```

Aspects:
- `comments` - Comment accuracy and documentation
- `tests` - Test coverage and quality
- `errors` - Error handling and silent failures
- `types` - Type design and invariants
- `code` - General code quality (always runs)
- `simplify` - Code simplification (polish phase)
- `all` - Run all applicable reviews (default)

## Observability

When Langfuse is enabled, you can see:
- Plugin loading and initialization
- Each review agent execution as a nested span
- Time spent in each agent
- Findings from each agent
- Coordinator's synthesis logic
- GitHub MCP tool calls
- Final review output

Access at: http://localhost:7500

## Plugin Structure

```
plugins/pr-review-toolkit/
├── .claude-plugin/
│   └── plugin.json          # Plugin manifest
├── commands/
│   └── review-pr.md         # Main review command
├── agents/
│   ├── comment-analyzer.md
│   ├── pr-test-analyzer.md
│   ├── silent-failure-hunter.md
│   ├── type-design-analyzer.md
│   ├── code-reviewer.md
│   └── code-simplifier.md
├── LICENSE
└── README.md
```

## See Also

- [Plugin Documentation](../plugins/pr-review-toolkit/README.md) - Detailed plugin usage
- [PLUGINS.md](PLUGINS.md) - General plugin system documentation
- [SUBAGENTS.md](SUBAGENTS.md) - Legacy subagent system (deprecated)
