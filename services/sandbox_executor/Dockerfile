FROM python:3.11-slim

# Create non-root user early
RUN useradd -m -u 1000 -s /bin/bash bot

WORKDIR /app

# Install common bash tools that agents use
RUN apt-get update && apt-get install -y --no-install-recommends \
    jq \
    curl \
    git \
    vim \
    nano \
    tree \
    wget \
    unzip \
    zip \
    sudo \
    && rm -rf /var/lib/apt/lists/*

# The sandbox needs the shared agent definitions and shared utilities
COPY subagents/ ./subagents/
COPY shared/ ./shared/

# Copy repo_setup module for dependency installation
COPY repo_setup/ ./repo_setup/

# Copy repo-setup.yaml configuration
COPY repo-setup.yaml ./repo-setup.yaml

# Hook scripts for Langfuse
COPY hooks/langfuse_hook.py /app/hooks/langfuse_hook.py
RUN chmod +x /app/hooks/langfuse_hook.py

COPY services/sandbox_executor/requirements.txt ./
RUN pip3 install --no-cache-dir -r requirements.txt

# Try to install claude-agent-sdk directly as it might be required
# If it's a private repo, we might need a requirements.txt with the correct index
RUN pip3 install uvicorn fastapi pydantic httpx langfuse

# Sandbox Executor source code
COPY services/sandbox_executor/ /app/services/sandbox_executor/

# Copy plugins and skills to bot user's .claude directory
RUN mkdir -p /home/bot/.claude/plugins /home/bot/.claude/skills
COPY plugins/ /home/bot/.claude/plugins/
COPY skills/ /home/bot/.claude/skills/

# Create repo cache directory and set ownership to bot user
RUN mkdir -p /var/cache/repos && chown -R bot:bot /var/cache/repos

# Set ownership to bot user
RUN chown -R bot:bot /app /home/bot/.claude

# Give bot user passwordless sudo for installing language runtimes
RUN echo "bot ALL=(ALL) NOPASSWD: ALL" >> /etc/sudoers

# Set USER environment variable for the bot user
ENV USER=bot

# Switch to non-root user
USER bot

WORKDIR /app
EXPOSE 8000

# Run uvicorn using the top-level module path so Python can find both 'services' and 'subagents'
CMD ["uvicorn", "services.sandbox_executor.main:app", "--host", "0.0.0.0", "--port", "8000"]
