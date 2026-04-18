---
description: "Run a generic task via test-toolkit to verify tool/skill inheritance between plugin and subagents"
argument-hint: "<task description — any freeform request>"
---

# Run Task

Execute a generic user request. This is a test plugin for verifying that tools and configuration properly flow from the plugin to any subagents it spawns.

**Arguments:** "$ARGUMENTS"

## Instructions

1. Read the user's request from `$ARGUMENTS`.
2. If the request asks to **delegate** or **use a subagent**:
   - Use the `Agent` tool with `subagent_type: "test-toolkit:generic-worker"` to spawn the `generic-worker` agent.
   - Pass the full task description as the `prompt`.
   - Return the agent's result to the user.
3. Otherwise, execute the task directly using available tools:
   - **Read a file** → use `Read`
   - **Run a command** → use `Bash`
   - **Search code** → use `Grep` or `Glob`
   - **Write/edit files** → use `Write` or `Edit`
4. Report results back to the user.

## What This Tests

- Tool availability: all customt tools should be accessible.
- Agent spawning: the `generic-worker` agent should be invocable and inherit the same tool set.
- Skill-to-agent inheritance: when the skill delegates to the agent, the agent should have access to the same tools and context.

## Usage Examples

**Direct task:**
```
/test read the plugin.json file
```

**Delegate to subagent:**
```
/test delegate: list all Python files in the project
```

**Run a command:**
```
/test run git status
```
