# Debugging Subagents

This guide helps you debug subagent issues and verify they're working correctly.

## Quick Checks

### 1. Verify subagents are installed in the container

```bash
docker-compose exec worker ls -la /root/.claude/agents/
```

You should see:
```
architecture-reviewer.md
bug-hunter.md
code-quality-reviewer.md
security-reviewer.md
```

### 2. List available agents

```bash
docker-compose exec worker claude agents
```

You should see your custom agents listed along with built-in ones.

### 3. Check container was rebuilt

After adding or modifying subagents, you MUST rebuild:

```bash
docker-compose build worker
docker-compose up -d
```

## Common Issues

### Subagents not being used

**Symptom**: Claude doesn't delegate to subagents, does the work itself

**Causes**:
1. **Container not rebuilt** - Subagents only copied during build
   - Solution: `docker-compose build worker && docker-compose up -d`

2. **Description not clear enough** - Claude doesn't know when to use them
   - Solution: Add "Use proactively when..." to description field
   - Example: `description: Use proactively when reviewing pull requests...`

3. **Prompt doesn't encourage delegation** - Main prompt is too prescriptive
   - Solution: Mention subagents in the prompt and encourage their use

4. **Subagent files in wrong location** - Must be in `/root/.claude/agents/`
   - Solution: Check Dockerfile copies to correct location

**Debug steps**:
```bash
# 1. Check files exist
docker-compose exec worker ls -la /root/.claude/agents/

# 2. Check file content
docker-compose exec worker cat /root/.claude/agents/architecture-reviewer.md

# 3. List agents
docker-compose exec worker claude agents

# 4. Check logs for subagent invocations
docker-compose logs -f worker
```

### Subagents fail with permission errors

**Symptom**: Subagent starts but fails to access tools

**Causes**:
1. **Tools not listed in frontmatter** - Subagent doesn't have permission
   - Solution: Add required tools to `tools:` field
   - Example: `tools: Read, Glob, Grep, mcp__github`

2. **MCP tools not available** - GitHub MCP not configured
   - Solution: Check MCP setup in worker logs

**Debug steps**:
```bash
# Check subagent configuration
docker-compose exec worker cat /root/.claude/agents/security-reviewer.md

# Look for permission errors in logs
docker-compose logs worker | grep -i "permission\|denied\|error"
```

### Subagents return wrong format

**Symptom**: Subagent completes but coordinator can't parse results

**Causes**:
1. **System prompt doesn't specify JSON format** - Subagent returns text
   - Solution: Include JSON schema in subagent's markdown body

2. **Coordinator doesn't know how to parse** - Main prompt unclear
   - Solution: Update coordinator prompt to expect JSON

**Debug steps**:
- Check Langfuse traces to see subagent output format
- Review subagent system prompt for JSON instructions

## Verification Steps

### Test subagent manually

You can test a subagent directly:

```bash
docker-compose exec worker claude --agent architecture-reviewer -p "Review the design patterns in this codebase"
```

This runs the subagent in isolation to verify it works.

### Check Langfuse traces

If Langfuse is enabled (http://localhost:7500):

1. Find your PR review trace
2. Look for nested spans with subagent names
3. Check subagent inputs and outputs
4. Verify JSON format is correct

**Note**: The system has two hooks configured:
- `Stop` hook: Logs when the main agent completes
- `SubagentStop` hook: Logs when each subagent completes

Each subagent (architecture-reviewer, security-reviewer, bug-hunter, code-quality-reviewer) will create its own trace entry in Langfuse when it finishes.

### Enable debug mode

Run Claude Code with debug logging:

```bash
docker-compose exec worker claude --debug -p "Test prompt"
```

This shows detailed execution including subagent invocations.

## Expected Behavior

### Successful PR Review Flow

1. **Main agent starts** - Receives auto_review prompt
2. **Delegates to 4 subagents** - You should see in logs:
   ```
   Spawning subagent: architecture-reviewer
   Spawning subagent: security-reviewer
   Spawning subagent: bug-hunter
   Spawning subagent: code-quality-reviewer
   ```
3. **Each subagent analyzes** - Returns JSON with findings
4. **Coordinator synthesizes** - Combines all findings
5. **Posts review** - Summary comment + inline comments

### Langfuse Trace Structure

```
Claude Code - Turn 1
├─ Claude Response
├─ Tool: mcp__github__pull_request_read
├─ Agent: architecture-reviewer
│  ├─ Tool: Read
│  └─ Tool: mcp__github__get_file
├─ Agent: security-reviewer
│  ├─ Tool: Read
│  └─ Tool: Grep
├─ Agent: bug-hunter
│  └─ Tool: Read
├─ Agent: code-quality-reviewer
│  └─ Tool: Read
├─ Tool: add_issue_comment
└─ Tool: pull_request_review_write
```

## Troubleshooting Commands

```bash
# Rebuild and restart
docker-compose build worker && docker-compose up -d

# Check agent files
docker-compose exec worker ls -la /root/.claude/agents/

# List available agents
docker-compose exec worker claude agents

# View worker logs
docker-compose logs -f worker

# Test subagent directly
docker-compose exec worker claude --agent bug-hunter -p "Find bugs in this code"

# Check Claude Code version
docker-compose exec worker claude --version

# Verify MCP configuration
docker-compose exec worker claude mcp list
```

## Getting Help

If subagents still aren't working:

1. **Check this guide** - Follow all verification steps
2. **Review logs** - Look for error messages
3. **Test manually** - Run subagent directly to isolate issue
4. **Check Langfuse** - See what Claude is actually doing
5. **Verify files** - Ensure .md files are in correct location

## Advanced Debugging

### Enable verbose logging

Set environment variable in docker-compose.yml:

```yaml
environment:
  - CLAUDE_CODE_DEBUG=1
```

### Check subagent context

Subagent transcripts are saved to:
```
~/.claude/projects/{project}/{sessionId}/subagents/agent-{agentId}.jsonl
```

You can read these to see exactly what the subagent saw and did.

### Test with minimal prompt

Create a simple test to verify subagents work:

```bash
docker-compose exec worker claude -p "Use the architecture-reviewer subagent to analyze this codebase"
```

If this works, the issue is with the auto_review prompt, not the subagents themselves.
