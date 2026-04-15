FROM python:3.12-slim

WORKDIR /app

# Copy shared module
COPY shared/ ./shared/

# Copy workflows and prompts (NEW - needed for WorkflowEngine)
COPY workflows/ ./workflows/
COPY workflows.yaml ./workflows.yaml
COPY prompts/ ./prompts/

# Copy agent_worker requirements and install
COPY services/agent_worker/requirements.txt ./
RUN pip3 install --no-cache-dir -r requirements.txt

# Copy agent_worker code
COPY services/agent_worker/ ./services/agent_worker/

# Copy subagents package
COPY subagents/ ./subagents/

# Copy hooks
COPY hooks/langfuse_hook.py /app/hooks/langfuse_hook.py
RUN chmod +x /app/hooks/langfuse_hook.py

WORKDIR /app

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
  CMD test -f /tmp/worker_health && \
      [ $(( $(date +%s) - $(stat -c %Y /tmp/worker_health 2>/dev/null || echo 0) )) -lt 120 ] || exit 1

CMD ["python3", "-m", "services.agent_worker.worker"]
