#!/bin/bash
# Seed plugins and skills into bind-mounted ~/.claude/
# Uses cp -rn (no overwrite) to preserve existing host files.
# Plugins are baked into /app/ in the image and copied on first run.

CLAUDE_DIR="/home/bot/.claude"
PLUGINS_SRC="/app/plugins"
SKILLS_SRC="/app/skills"

mkdir -p "$CLAUDE_DIR/plugins" "$CLAUDE_DIR/skills" "$CLAUDE_DIR/projects"

if [ "$ENABLE_BUILTIN_PLUGINS" = "true" ] && [ -d "$PLUGINS_SRC" ]; then
    cp -rn "$PLUGINS_SRC"/* "$CLAUDE_DIR/plugins/" 2>/dev/null || true
fi

if [ -d "$SKILLS_SRC" ]; then
    cp -rn "$SKILLS_SRC"/* "$CLAUDE_DIR/skills/" 2>/dev/null || true
fi

# Forward localhost:11434 to host.docker.internal:11434 so local Ollama configuration works inside Docker
socat TCP-LISTEN:11434,fork TCP:host.docker.internal:11434 >/dev/null 2>&1 &

# Forward custom API port if SOCAT_API_FORWARD is set (for oc-go-cc, OpenCode, or other local API providers)
# Set in .env: SOCAT_API_FORWARD=8080 to forward localhost:8080 → host.docker.internal:8080
if [ -n "${SOCAT_API_FORWARD}" ]; then
    echo "Setting up socat forward for API port ${SOCAT_API_FORWARD}: localhost:${SOCAT_API_FORWARD} → host.docker.internal:${SOCAT_API_FORWARD}"
    socat TCP-LISTEN:${SOCAT_API_FORWARD},fork TCP:host.docker.internal:${SOCAT_API_FORWARD} >/dev/null 2>&1 &
fi

exec "$@"
