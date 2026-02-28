FROM node:18-slim

WORKDIR /app

# Install Python
RUN apt-get update && apt-get install -y \
    python3 \
    python3-pip \
    && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI
RUN npm install -g @anthropic-ai/claude-code

# Copy shared module
COPY shared/ ./shared/

# Copy agent-worker
COPY services/agent-worker/requirements.txt ./services/agent-worker/
RUN pip3 install --no-cache-dir -r services/agent-worker/requirements.txt --break-system-packages

COPY services/agent-worker/worker.py ./services/agent-worker/
COPY hooks/langfuse_hook.py /app/hooks/langfuse_hook.py
RUN chmod +x /app/hooks/langfuse_hook.py

# Copy subagents to Claude Code's agents directory (Markdown format)
RUN mkdir -p /root/.claude/agents
COPY subagents/*.md /root/.claude/agents/

WORKDIR /app/services/agent-worker

CMD ["python3", "worker.py"]
