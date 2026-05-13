# Subagents

Subagents are specialized Claude instances the main agent can delegate focused tasks to. They run with their own system prompts and return structured results.

## How They Work

The main agent decides when to delegate based on the subagent's `description`. Each subagent runs with a focused prompt, produces results, and the main agent synthesizes them.

## Creating a Subagent

### 1. Create a Python file in `subagents/`

```python
# subagents/my_specialist.py
from claude_agent_sdk import AgentDefinition

MY_SPECIALIST = AgentDefinition(
    description="What this agent does and when to use it. The main agent reads this to decide when to delegate.",
    prompt="""You are a specialist in [domain].

Your role is to [specific task].

Return your findings as JSON:
```json
{
  "findings": [{"file": "...", "issue": "...", "suggestion": "..."}],
  "summary": "Overall assessment"
}
```""",
    model="inherit",  # Use same model as parent
)
```

### 2. Export in `subagents/__init__.py`

```python
from .my_specialist import MY_SPECIALIST

AGENTS = {
    "my-specialist": MY_SPECIALIST,
    # ... other agents
}
```

### 3. Rebuild

```bash
docker-compose build sandbox_worker
docker-compose up -d sandbox_worker
```

## Key Fields

| Field | Description |
|-------|-------------|
| `description` | Tells the main agent when to use this subagent. Be specific about triggers |
| `prompt` | System prompt — define the role, process, and output format (JSON recommended) |
| `model` | `"inherit"` uses the parent's model. Can also specify a specific model |
| `tools` | Omit to inherit all parent tools. Or set `tools=["tool1", "tool2"]` to restrict |

## Built-in Subagents

### architecture-reviewer

Evaluates design patterns, SOLID principles, and architectural consistency in PRs. Prioritizes architecturally significant files over exhaustive exploration.

```python
# subagents/architecture_reviewer.py
ARCHITECTURE_REVIEWER = AgentDefinition(
    description="Expert in reviewing architectural decisions, design patterns, and system design. Use proactively when reviewing pull requests or significant code changes to evaluate SOLID principles, coupling, and architectural consistency.",
    prompt="""...""",  # 4-step review process with JSON output
    model="inherit",
)
```

### memory-extractor

Runs automatically after each session (via the Memory Worker). Extracts persistent knowledge from transcripts — architecture decisions, known issues, conventions — and organizes them into memory files.

```python
# subagents/memory_extractor.py
MEMORY_EXTRACTOR = AgentDefinition(
    description="Extracts memorable facts from agent session transcripts to build repository knowledge.",
    prompt="""...""",  # Reads index.md first, then extracts and organizes new facts
    model="inherit",
)
```

Uses Haiku (`ANTHROPIC_DEFAULT_HAIKU_MODEL`) for cost efficiency. Tools restricted to `Read`, `Write`, `Edit`, `List`, `mcp__memory__*`.

## See Also

- [Plugins](PLUGINS.md) - Plugin agents (12 built in across 5 plugins)
- [Architecture](ARCHITECTURE.md) - System design
