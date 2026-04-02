FROM python:3.11-slim

# Create non-root user matching the agent-memory volume ownership
RUN useradd -m -u 1000 -s /bin/bash bot

WORKDIR /app

# Minimal OS deps (no git/build tools needed — just Python and file I/O)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Shared utilities and subagent definitions
COPY shared/ ./shared/
COPY subagents/ ./subagents/

# Copy MCP servers (needed by memory extractor subagent)
COPY mcp_servers/ ./mcp_servers/

# Hook scripts for Langfuse observability
COPY hooks/langfuse_hook.py /app/hooks/langfuse_hook.py
RUN chmod +x /app/hooks/langfuse_hook.py

# Worker source
COPY services/memory_worker/ /app/services/memory_worker/

# Install Python dependencies (re-uses the sdk + shared deps)
COPY services/sandbox_executor/requirements.txt ./requirements.txt
RUN pip3 install --no-cache-dir -r requirements.txt

# Create persistent memory directory and set ownership
RUN mkdir -p /home/bot/.claude/projects && chown -R bot:bot /home/bot/.claude/projects

# Create agent memory directory for reading transcripts and writing MEMORY.md
RUN mkdir -p /home/bot/agent-memory && chown -R bot:bot /home/bot/agent-memory

# Set ownership
RUN chown -R bot:bot /app /home/bot

ENV USER=bot

USER bot

CMD ["python", "-m", "services.memory_worker.memory_worker"]
