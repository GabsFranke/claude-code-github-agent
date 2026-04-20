#!/bin/bash
# Seed plugins and skills into bind-mounted ~/.claude/
# Uses cp -rn (no overwrite) to preserve existing host files.
# Plugins are baked into /app/ in the image and copied on first run.

CLAUDE_DIR="/home/bot/.claude"
PLUGINS_SRC="/app/plugins"
SKILLS_SRC="/app/skills"

mkdir -p "$CLAUDE_DIR/plugins" "$CLAUDE_DIR/skills" "$CLAUDE_DIR/projects"

if [ -d "$PLUGINS_SRC" ]; then
    cp -rn "$PLUGINS_SRC"/* "$CLAUDE_DIR/plugins/" 2>/dev/null || true
fi

if [ -d "$SKILLS_SRC" ]; then
    cp -rn "$SKILLS_SRC"/* "$CLAUDE_DIR/skills/" 2>/dev/null || true
fi

exec "$@"
