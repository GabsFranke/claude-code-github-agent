# Subagent Support

This project includes specialized subagents that Claude Code can delegate tasks to for more focused and efficient work.

## What are Subagents?

Subagents are specialized Claude Code instances with:
- Focused system prompts for specific tasks
- Restricted permissions appropriate to their role
- Isolated execution contexts
- Results that flow back to the main agent

## Available Subagents

| Subagent | Purpose | Permissions | Use Case |
|----------|---------|-------------|----------|
| **architecture-reviewer** | Review architectural decisions and design patterns | Read-only | Evaluating system design, SOLID principles, coupling |
| **security-reviewer** | Identify security vulnerabilities and risks | Read-only | Finding SQL injection, XSS, auth issues, data exposure |
| **bug-hunter** | Find potential bugs and edge cases | Read-only | Null checks, race conditions, error handling |
| **code-quality-reviewer** | Review code quality and maintainability | Read-only | Style, readability, documentation, complexity |
| **context-gatherer** | Explore codebase and identify relevant files | Read-only | Starting work on unfamiliar code, investigating issues |
| **bug-investigator** | Trace bugs to root causes | Read-only | Debugging, error investigation, root cause analysis |
| **test-writer** | Write comprehensive test cases | Read + Write | Adding test coverage, testing new features |

## How Subagents Work in PR Reviews

When a PR is opened, the main agent acts as a coordinator:

1. **Delegates** to 4 specialized subagents in parallel:
   - `architecture-reviewer` - Checks design patterns and system architecture
   - `security-reviewer` - Scans for security vulnerabilities
   - `bug-hunter` - Identifies potential bugs and edge cases
   - `code-quality-reviewer` - Reviews code style and maintainability

2. **Synthesizes** results from all subagents into a unified view

3. **Posts** a comprehensive summary comment with findings by category

4. **Adds** inline comments for the most critical issues

This multi-agent approach provides thorough, specialized reviews covering all aspects of code quality.

### Automatic Usage

For PR reviews, the main agent automatically delegates to specialized subagents:
- **architecture-reviewer** evaluates design decisions
- **security-reviewer** scans for vulnerabilities  
- **bug-hunter** finds potential bugs
- **code-quality-reviewer** checks code style

The coordinator synthesizes all findings into a comprehensive review.

For other tasks, the agent may delegate when appropriate.

### Manual Invocation

You can explicitly request subagents in your commands:

```
/agent use context-gatherer to find all authentication-related files
/agent have security-reviewer check this code for vulnerabilities
/agent ask bug-investigator why the API crashes on large uploads
/agent use test-writer to add tests for the payment module
/agent have architecture-reviewer evaluate the new service design
```

### In CLAUDE.md

You can configure subagent usage in your repository's CLAUDE.md:

```markdown
# Agent Instructions

For code reviews:
- Always run all four review subagents (architecture, security, bugs, code-quality)
- Prioritize security findings above all else
- Be extra strict on error handling in the payment module

For bug investigations, delegate to bug-investigator first.
```

## Adding Custom Subagents

1. Create a new JSON file in `subagents/`:

```json
{
  "name": "security-auditor",
  "description": "Specialized security audit agent",
  "systemPrompt": "You are a security auditor...",
  "permissions": {
    "allow": ["Read", "Grep", "mcp__github"],
    "deny": ["Write", "Edit", "Bash"],
    "ask": []
  }
}
```

2. Rebuild the Docker container:

```bash
docker-compose build worker
docker-compose up -d
```

3. The subagent is now available to the main agent

## Benefits

- **Focused expertise**: Each subagent is optimized for specific tasks
- **Better context**: Subagents work with focused prompts and context
- **Safety**: Restricted permissions reduce risk of unintended changes
- **Efficiency**: Parallel execution of independent subtasks
- **Clarity**: Clear separation of concerns in complex workflows

## Architecture

```
Main Agent (Claude Code)
    ↓ delegates task
Subagent (Specialized Claude Code instance)
    ↓ executes with focused context
    ↓ returns result
Main Agent (synthesizes and continues)
```

Subagents run in the same Docker container but with isolated contexts and their own system prompts.

## Observability

When using Langfuse, subagent executions are tracked as nested spans within the main agent's trace, allowing you to see:
- Which subagents were invoked
- What tasks they performed
- How long each took
- What results they returned

The system is configured with two Langfuse hooks:
- **Stop hook**: Logs main agent completion
- **SubagentStop hook**: Logs each subagent completion

This means every subagent execution (architecture-reviewer, security-reviewer, bug-hunter, code-quality-reviewer) will be logged to Langfuse automatically when it completes.

## Best Practices

1. **Use subagents for focused tasks**: Don't delegate everything
2. **Choose the right subagent**: Match the task to the subagent's expertise
3. **Provide clear instructions**: Be specific about what you want the subagent to do
4. **Review subagent results**: The main agent should validate and synthesize results
5. **Monitor performance**: Use Langfuse to track subagent effectiveness

## Troubleshooting

**Subagent not found:**
- Ensure the JSON file is in `subagents/` directory
- Rebuild the Docker container
- Check the subagent name matches the filename (without .json)

**Permission errors:**
- Review the subagent's permissions in its JSON file
- Ensure required tools are in the "allow" list
- Check that the task doesn't require denied permissions

**Poor results:**
- Review and refine the subagent's system prompt
- Provide more context in your delegation request
- Consider if a different subagent would be more appropriate

## See Also

- [Claude Code Subagents Documentation](https://code.claude.com/docs/en/sub-agents)
- [subagents/README.md](subagents/README.md) - Detailed subagent documentation
- [ARCHITECTURE.md](ARCHITECTURE.md) - System architecture overview
