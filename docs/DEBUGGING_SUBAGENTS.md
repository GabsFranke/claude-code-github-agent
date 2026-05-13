# Debugging Subagents and Plugin Agents

This guide helps you debug subagent and plugin agent issues and verify they're working correctly.

## Agent Architecture

Agents come in two categories:

1. **Core Subagents** (in `subagents/`): Built into the system image, always available.
   - `architecture-reviewer` — Design patterns and SOLID principles review
   - `memory-extractor` — Extracts facts from session transcripts to build repository knowledge (runs on Haiku for cost efficiency)

2. **Plugin Agents** (in `plugins/*/agents/`): Loaded dynamically from plugin directories.
   - **pr-review-toolkit** (7 agents): code-reviewer, code-architecture-reviewer, code-simplifier, comment-analyzer, pr-test-analyzer, silent-failure-hunter, type-design-analyzer
   - **ci-failure-toolkit** (4 agents): build-failure-analyzer, deploy-failure-analyzer, lint-failure-analyzer, test-failure-analyzer
   - **test-toolkit** (1 agent): generic-worker

All agents run inside the `sandbox_worker` container via the Claude Agent SDK `Task` tool.

## Quick Checks

### 1. Verify plugin agents are installed

```bash
docker-compose exec sandbox_worker ls -la /root/.claude/plugins/
```

You should see plugin directories for each installed plugin.

### 2. Verify core subagents are available

```bash
docker-compose exec sandbox_worker ls -la /root/.claude/agents/
```

You should see:
```
architecture-reviewer.md
memory-extractor.md
```

### 3. Check container was rebuilt

After adding or modifying agents or plugins, you MUST rebuild:

```bash
docker-compose build sandbox_worker
docker-compose up -d sandbox_worker
```

## Common Issues

### Agents not being used

**Symptom**: Claude doesn't delegate to agents, does the work itself

**Causes**:

1. **Container not rebuilt** — Agents and plugins are only copied during build
   - Solution: `docker-compose build sandbox_worker && docker-compose up -d sandbox_worker`

2. **Description not clear enough** — Claude doesn't know when to use them
   - Solution: Add "Use proactively when..." to the agent's `description` field in its `.md` file
   - Example: `description: "Use proactively when reviewing pull requests..."`

3. **Prompt doesn't encourage delegation** — Main prompt is too prescriptive
   - Solution: Mention agents in the system context and encourage their use

4. **Agent files in wrong location** — Core subagents must be in `subagents/`, plugin agents in `plugins/*/agents/`
   - Solution: Check that `.md` files are in the correct directory

**Debug steps**:

```bash
# 1. Check plugin directories exist
docker-compose exec sandbox_worker ls -la /root/.claude/plugins/

# 2. Check core subagent files
docker-compose exec sandbox_worker ls -la /root/.claude/agents/

# 3. Check sandbox worker logs
docker-compose logs -f sandbox_worker

# 4. Check Langfuse hook logs (inside container only)
docker-compose exec sandbox_worker cat /root/.claude/state/langfuse_hook.log

# 5. View last 50 lines of hook logs
docker-compose exec sandbox_worker tail -n 50 /root/.claude/state/langfuse_hook.log
```

**Note**: Hook logs are only available inside the container at `/root/.claude/state/langfuse_hook.log`. They are NOT captured by `docker-compose logs`.

### Agents fail with permission errors

**Symptom**: Agent starts but fails to access tools

**Causes**:

1. **Tools not listed in frontmatter** — Agent doesn't have permission
   - Solution: Add required tools to `allowed-tools:` field in the `.md` frontmatter
   - Example: `allowed-tools: ["Read", "Glob", "Grep", "mcp__github__*"]`

2. **MCP tools not available** — GitHub MCP not configured
   - Solution: Check MCP setup in sandbox worker logs

**Debug steps**:

```bash
# Check agent configuration
docker-compose exec sandbox_worker cat /root/.claude/plugins/pr-review-toolkit/agents/code-reviewer.md

# Look for permission errors in logs
docker-compose logs sandbox_worker | grep -i "permission\|denied\|error"
```

### Agents return wrong format

**Symptom**: Agent completes but the coordinator can't parse results

**Causes**:

1. **System prompt doesn't specify JSON format** — Agent returns free-form text
   - Solution: Include a JSON schema or output format in the agent's `.md` body

2. **Coordinator doesn't know how to parse** — Main prompt unclear
   - Solution: Update the coordinator prompt to expect the agent's output format

## Verification Steps

### Test a plugin agent manually

You can test an agent directly inside the sandbox container:

```bash
docker-compose exec sandbox_worker claude --agent code-reviewer -p "Review the design patterns in this codebase"
```

### Check Langfuse traces

If Langfuse is enabled (http://localhost:7500):

1. Find your PR review trace
2. Look for nested spans with agent names
3. Check agent inputs and outputs
4. Verify JSON format is correct

**Note**: The system has two hooks configured:
- `Stop` hook: Logs when the main agent completes
- `SubagentStop` hook: Logs when each subagent completes

Each agent (code-reviewer, architecture-reviewer, etc.) creates its own trace entry in Langfuse when it finishes.

### Enable debug mode

Run Claude Code with debug logging:

```bash
docker-compose exec sandbox_worker claude --debug -p "Test prompt"
```

This shows detailed execution including agent invocations.

## Expected Behavior

### Successful PR Review Flow

1. **Main agent starts** — Receives review prompt
2. **Invokes `/pr-review-toolkit:review-pr`** — The plugin's command orchestrates the review
3. **Plugin delegates to specialized agents** — You should see in logs:
   ```
   Spawning agent: code-reviewer
   Spawning agent: code-architecture-reviewer
   Spawning agent: code-simplifier
   Spawning agent: comment-analyzer
   Spawning agent: pr-test-analyzer
   Spawning agent: silent-failure-hunter
   Spawning agent: type-design-analyzer
   ```
4. **Each agent analyzes** — Returns structured findings
5. **Coordinator synthesizes** — Combines all findings
6. **Posts review** — Summary comment + inline comments via GitHub MCP

### Successful CI Failure Fix Flow

1. **Main agent starts** — Receives fix-ci prompt
2. **Invokes `/ci-failure-toolkit:fix-ci`** — The plugin's command analyzes the failure
3. **Plugin delegates to specialized agent** — One of: build-failure-analyzer, deploy-failure-analyzer, lint-failure-analyzer, or test-failure-analyzer (selected based on failure type)
4. **Agent analyzes CI logs** — Via GitHub Actions MCP tools
5. **Creates fix PR** — Pushes branch and opens pull request

### Langfuse Trace Structure

```
github_agent_request
├─ Claude Code - Turn 1
│  ├─ Claude Response
│  ├─ Tool: mcp__github__pull_request_read
│  ├─ Agent: code-reviewer
│  │  ├─ Tool: Read
│  │  └─ Tool: mcp__github__get_file
│  ├─ Agent: code-architecture-reviewer
│  │  ├─ Tool: Read
│  │  └─ Tool: Grep
│  ├─ Agent: silent-failure-hunter
│  │  └─ Tool: Read
│  ├─ Tool: mcp__github__add_issue_comment
│  └─ Tool: mcp__github__pull_request_review_write
```

## Troubleshooting Commands

```bash
# Rebuild and restart (sandbox_worker, not worker)
docker-compose build sandbox_worker && docker-compose up -d sandbox_worker

# Check plugin directories
docker-compose exec sandbox_worker ls -la /root/.claude/plugins/

# Check core subagent files
docker-compose exec sandbox_worker ls -la /root/.claude/agents/

# View all sandbox worker logs
docker-compose logs -f sandbox_worker

# View Langfuse hook logs (inside container only)
docker-compose exec sandbox_worker cat /root/.claude/state/langfuse_hook.log

# View recent hook logs
docker-compose exec sandbox_worker tail -n 50 /root/.claude/state/langfuse_hook.log

# Test a core subagent directly
docker-compose exec sandbox_worker claude --agent architecture-reviewer -p "Review the design patterns in this codebase"

# Test a plugin command
docker-compose exec sandbox_worker claude -p "/pr-review-toolkit:review-pr owner/repo 42"

# Check Claude Code version
docker-compose exec sandbox_worker claude --version

# Verify MCP configuration
docker-compose exec sandbox_worker claude mcp list
```

**Note**: Hook logs are stored inside the container and are NOT visible via `docker-compose logs`. You must use `docker-compose exec` to view them.

## Getting Help

If agents still aren't working:

1. **Check this guide** — Follow all verification steps
2. **Review logs** — Look for error messages
3. **Test manually** — Run agent directly to isolate the issue
4. **Check Langfuse** — See what Claude is actually doing
5. **Verify files** — Ensure `.md` files are in the correct plugin or subagent directory

## Advanced Debugging

### Enable verbose logging

Set environment variable in docker-compose.yml:

```yaml
environment:
  - SDK_DEBUG=true
```

### Check agent context

Agent transcripts are saved to:
```
~/.claude/projects/{project}/{sessionId}/subagents/agent-{agentId}.jsonl
```

You can read these to see exactly what the agent saw and did.

### Test with minimal prompt

Create a simple test to verify agents work:

```bash
docker-compose exec sandbox_worker claude -p "Use the architecture-reviewer agent to analyze this codebase"
```

If this works, the issue is with the workflow prompt, not the agents themselves.
