# Subagent Support

Subagents are specialized Claude instances that the main agent can delegate focused tasks to. They run with their own system prompts and can inherit or restrict tool access as needed.

## What are Subagents?

Subagents provide:
- Focused system prompts optimized for specific tasks
- Tool inheritance from the parent agent (or custom tool restrictions)
- Isolated execution contexts
- Results that flow back to the main agent for synthesis

Think of them as expert consultants the main agent can call upon for specialized analysis or work.

## How Subagents Work

```
Main Agent
    ↓ delegates task
Subagent (specialized Claude instance)
    ↓ executes with focused prompt
    ↓ returns structured result
Main Agent (synthesizes and continues)
```

The main agent decides when to use subagents based on the task at hand. For example, when reviewing a PR that touches authentication code, it might delegate to a security-reviewer subagent for vulnerability analysis.

## Creating a Subagent

Subagents are defined using the Claude Agent SDK's `AgentDefinition` class. Here's the anatomy:

### 1. Create a Python file in `subagents/`

```python
# subagents/my_specialist.py
"""My specialist subagent - does something specific."""

from claude_agent_sdk import AgentDefinition

MY_SPECIALIST = AgentDefinition(
    description="Brief description of what this agent does and when to use it proactively.",
    prompt="""You are a specialist in [domain].

Your role is to [specific task].

When analyzing code:
1. [Step 1]
2. [Step 2]
3. [Step 3]

Return your findings as JSON:
```json
{
  "findings": [
    {
      "file": "path/to/file",
      "line": 42,
      "severity": "high",
      "issue": "Brief description",
      "explanation": "Detailed explanation",
      "suggestion": "How to fix"
    }
  ],
  "summary": "Overall assessment"
}
```

Focus on [what matters most].""",
    # Omit tools field to inherit all tools from parent
    # Or specify tools=["tool1", "tool2"] to restrict
    model="inherit"  # Use same model as parent
)
```

### 2. Export it in `subagents/__init__.py`

```python
from .my_specialist import MY_SPECIALIST

AGENTS = {
    "my-specialist": MY_SPECIALIST,
    # ... other agents
}

__all__ = ["AGENTS", "MY_SPECIALIST"]
```

### 3. Rebuild and restart the worker

```bash
docker-compose up --build -d worker
```

The subagent is now available to the main agent.

## Key Components

### Description
The `description` field tells the main agent:
- What the subagent specializes in
- When to use it proactively
- What kind of analysis it provides

This is crucial because the main agent reads these descriptions to decide which subagents to delegate to.

### Prompt
The `prompt` is the system prompt for the subagent. It should:
- Define the role clearly
- Provide step-by-step instructions
- Specify output format (JSON is recommended for structured results)
- Focus on the specific domain

### Tools
By default, subagents inherit all tools from the parent agent except `Task` (subagents can't create tasks). You can:
- Omit the `tools` field to inherit everything
- Specify `tools=["tool1", "tool2"]` to restrict to specific tools
- Use `tools=["mcp__github__*"]` for pattern matching

### Model
Set `model="inherit"` to use the same model as the parent agent.

## Real Example: Bug Hunter

Here's a complete example from this project:

```python
# subagents/bug_hunter.py
from claude_agent_sdk import AgentDefinition

BUG_HUNTER = AgentDefinition(
    description="Specialist in finding potential bugs, edge cases, and error handling issues. Use proactively when reviewing pull requests to identify null checks, race conditions, and logic errors before they reach production.",
    prompt="""You are a bug hunter specializing in identifying potential bugs and edge cases.

IMPORTANT: You are reviewing a GitHub Pull Request. Use GitHub MCP tools to read the PR, NOT local filesystem tools.

When reviewing a PR:
1. Use mcp__github tools to read the PR diff and files
2. Look for null/undefined handling issues
3. Check for race conditions and concurrency problems
4. Identify missing error handling
5. Find edge cases and boundary conditions

Return your findings as JSON:
```json
{
  "findings": [
    {
      "file": "path/to/file.ts",
      "line": 42,
      "severity": "high",
      "category": "bug-risk",
      "issue": "Brief description",
      "explanation": "Why this could cause a bug",
      "suggestion": "How to fix it",
      "code_snippet": "Relevant code"
    }
  ],
  "summary": "Found X potential bugs, Y edge cases",
  "risk_assessment": "Overall risk level"
}
```

Prioritize by severity: critical bugs first, then high-risk edge cases.""",
    model="inherit"
)
```


## Best Practices

1. **Clear descriptions**: The main agent uses these to decide when to delegate
2. **Structured output**: JSON makes it easy for the main agent to parse results
3. **Focused prompts**: Don't try to do everything in one subagent
4. **Tool inheritance**: Let subagents inherit tools unless you need restrictions
5. **Severity levels**: Use consistent severity levels across subagents (critical, high, medium, low)


## See Also

- [Claude Agent SDK Documentation](https://github.com/anthropics/anthropic-sdk-python)
- [subagents/](../subagents/) - Source code for current subagents
- [ARCHITECTURE.md](ARCHITECTURE.md) - System architecture overview
