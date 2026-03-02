# Using Claude Code Plugins

This guide explains how to add and customize Claude Code plugins for use with GitHub MCP tools.

## What Are Plugins?

Plugins bundle multiple components into a single package:
- **Agents** - Specialized subagents for specific tasks
- **Commands** - Slash commands (like `/review-pr`)
- **Skills** - Reusable workflows
- **MCP Servers** - External tool integrations
- **Hooks** - Event-driven automation

## Adding Official Plugins

### Step 1: Clone the Official Plugin Repository

```bash
# Clone Anthropic's official plugin repository
git clone https://github.com/anthropics/claude-plugins-official.git

# Navigate to the plugins directory
cd claude-plugins-official/plugins
```

### Step 2: Copy Plugin to Your Project

```bash
# From your project root
cp -r /path/to/claude-plugins-official/plugins/pr-review-toolkit ./plugins/

# Example structure:
# plugins/
# └── pr-review-toolkit/
#     ├── .claude-plugin/
#     │   └── plugin.json
#     ├── agents/
#     │   ├── comment-analyzer.md
#     │   ├── pr-test-analyzer.md
#     │   ├── silent-failure-hunter.md
#     │   ├── type-design-analyzer.md
#     │   ├── code-reviewer.md
#     │   └── code-simplifier.md
#     ├── commands/
#     │   └── review-pr.md
#     ├── LICENSE
#     └── README.md
```

### Step 3: Customize for GitHub MCP

Official plugins are designed for local git workflows. To use them with GitHub MCP:

**Edit `commands/review-pr.md`:**

1. Change the argument format to include repository:
```markdown
argument-hint: "[owner/repo] [pr-number] [review-aspects]"
```

2. Update allowed tools:
```markdown
allowed-tools: ["Task", "mcp__github__*"]
```

3. Replace git commands with GitHub MCP tools:
- `git diff` → `get_pull_request_diff`
- `gh pr view` → `get_pull_request`
- Local file reads → `get_pull_request_files`

4. Add GitHub posting logic:
- Use `add_issue_comment` for summary
- Use `pull_request_review_write` for inline comments

**Example customization:**
```markdown
## Review Workflow:

1. **Parse Arguments**
   - Extract repository (owner/repo)
   - Extract PR number
   - Parse review aspects

2. **Read PR via GitHub MCP**
   - Use `get_pull_request` to get PR details
   - Use `list_pull_request_files` to see changed files
   - Use `get_pull_request_diff` to analyze changes

3. **Launch Review Agents**
   - Use Task tool to delegate to specialized agents
   - Each agent analyzes from their perspective

4. **Post Results to GitHub**
   - Summary comment via `add_issue_comment`
   - Inline comments via `pull_request_review_write`
```

### Step 4: Configure Worker

The worker is configured to load plugins using the Claude Agent SDK plugin system:

```python
# In services/agent-worker/worker.py
options = ClaudeAgentOptions(
    agents=AGENTS,  # Custom Python agents
    plugins=[{"type": "local", "path": "/app/plugins/pr-review-toolkit"}],  # Plugin path
    allowed_tools=["Task", "mcp__github__*"],
    # ... other options
)
```

**To add multiple plugins:**

```python
options = ClaudeAgentOptions(
    agents=AGENTS,
    plugins=[
        {"type": "local", "path": "/app/plugins/pr-review-toolkit"},
        {"type": "local", "path": "/app/plugins/another-plugin"},
    ],
    allowed_tools=["Task", "mcp__github__*"],
)
```

**Key points:**
- Each plugin needs its own entry in the `plugins` list
- Path must point to the plugin root directory (containing `.claude-plugin/`)
- Use absolute paths in Docker (`/app/plugins/...`)
- Plugins are loaded at SDK initialization time

### Step 5: Use the Plugin

When auto-review is triggered, the bot runs:

```python
prompt = f"/pr-review-toolkit:review-pr {repo} {issue_number} all"
```

The command:
1. Reads the PR via GitHub MCP
2. Launches appropriate specialized agents
3. Aggregates findings
4. Posts review to GitHub

## Plugin Structure

A valid plugin must have:

```
plugin-name/
├── .claude-plugin/
│   └── plugin.json          # Required: Plugin metadata
├── agents/                  # Optional: Subagent definitions
│   └── *.md
├── commands/                # Optional: Slash commands
│   └── *.md
├── skills/                  # Optional: Reusable workflows
│   └── */SKILL.md
├── .mcp.json               # Optional: MCP server configs
└── hooks/                  # Optional: Event hooks
    └── hooks.json
```

## Adding More Plugins

To add another plugin:

1. **Copy plugin to `./plugins/`:**
```bash
cp -r /path/to/another-plugin ./plugins/
```

2. **Update worker.py to load the new plugin:**
```python
# In services/agent-worker/worker.py, find the ClaudeAgentOptions section
options = ClaudeAgentOptions(
    agents=AGENTS,
    plugins=[
        {"type": "local", "path": "/app/plugins/pr-review-toolkit"},
        {"type": "local", "path": "/app/plugins/another-plugin"},  # Add new plugin
    ],
    allowed_tools=["Task", "mcp__github__*"],
    # ... other options
)
```

3. **Customize for GitHub MCP if needed** (see Step 3 above)

4. **Rebuild and restart worker:**
```bash
docker-compose build worker
docker-compose up -d worker
```

5. **Verify plugin loaded:**
Check worker logs for plugin initialization:
```bash
docker-compose logs worker | grep -i plugin
# Should see: "Loaded plugins: [{'name': 'pr-review-toolkit', ...}]"
```

**Note:** Unlike some systems, plugins are NOT auto-discovered from the directory. You must explicitly add each plugin to the `plugins` list in `worker.py`.

## Custom Agents vs Plugin Agents

You can use both:

**Custom Python Agents** (`subagents/`):
- Defined programmatically in Python
- Passed via `agents=AGENTS`
- Good for complex logic

**Plugin Agents** (`plugins/*/agents/`):
- Defined in markdown files
- Auto-discovered from plugins
- Easy to share and update

Both are available via the Task tool.

## Troubleshooting

**Plugin not loading:**
- Check `./plugins/plugin-name/.claude-plugin/plugin.json` exists
- Verify plugin is added to `plugins` list in `worker.py`
- Ensure path points to plugin root directory (containing `.claude-plugin/`)
- Check worker logs: `docker-compose logs worker | grep -i plugin`
- Look for init message: "Loaded plugins: [...]"

**Command not working:**
- Ensure command file is in `commands/` directory
- Check `allowed-tools` includes necessary tools
- Verify argument format matches usage
- Use namespaced format: `/plugin-name:command-name`

**Agents not found:**
- Agent files must be in `agents/` directory
- Files must be markdown (`.md`)
- Check agent names in Task tool invocations
- Verify agents appear in init message: "Loaded X custom agents: [...]"

**Path issues:**
- Use absolute Docker paths: `/app/plugins/plugin-name`
- Not relative paths like `./plugins/plugin-name`
- Path must exist in Docker container (check volume mounts)

**Plugin changes not reflected:**
- Rebuild worker: `docker-compose build worker`
- Restart: `docker-compose up -d worker`
- Plugins are loaded at initialization, not dynamically

## See Also

- [Official Plugin Repository](https://github.com/anthropics/claude-plugins-official)
- [Claude Code Plugin Docs](https://docs.claude.com/en/docs/claude-code/plugins)
- [Plugin Reference](https://code.claude.com/docs/en/plugins-reference)
