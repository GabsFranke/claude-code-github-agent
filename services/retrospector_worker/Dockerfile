FROM python:3.12-slim

# Create non-root user matching the agent-memory volume ownership
RUN useradd -m -u 1000 -s /bin/bash bot

WORKDIR /app

# git is required for worktree operations and branch creation
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    socat \
    && rm -rf /var/lib/apt/lists/*

# Shared utilities
COPY shared/ ./shared/

# The retrospector plugin (command definitions)
COPY plugins/retrospector/ /app/plugins/retrospector/

# Hook scripts for Langfuse observability (reused if needed)
COPY hooks/langfuse_hook.py /app/hooks/langfuse_hook.py
RUN chmod +x /app/hooks/langfuse_hook.py

# Worker source
COPY services/retrospector_worker/ /app/services/retrospector_worker/

# Install Python dependencies (reuses sdk + shared deps)
COPY services/sandbox_executor/requirements.txt ./requirements.txt
RUN pip3 install --no-cache-dir -r requirements.txt

# Entrypoint seeds plugins/skills into ~/.claude/ on first run
COPY services/sandbox_executor/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Claude home directory (bind-mounted from host ~/.claude/)
RUN mkdir -p /home/bot/.claude && chown -R bot:bot /home/bot/.claude

# Set ownership
RUN chown -R bot:bot /app /home/bot

ENV USER=bot

USER bot

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python", "-m", "services.retrospector_worker.retrospector_worker"]
