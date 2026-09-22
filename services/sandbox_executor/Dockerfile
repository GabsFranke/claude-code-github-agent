FROM python:3.12-slim

# Create non-root user early
RUN useradd -m -u 1000 -s /bin/bash bot

WORKDIR /app

# Install common bash tools first (curl needed for NodeSource setup)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    jq \
    git \
    vim \
    nano \
    tree \
    wget \
    unzip \
    zip \
    sudo \
    ripgrep \
    socat \
    tmux \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js via NodeSource (includes npm) + codegraph CLI
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @colbymchenry/codegraph \
    && rm -rf /var/lib/apt/lists/*

# The sandbox needs the shared agent definitions and shared utilities
COPY subagents/ ./subagents/
COPY shared/ ./shared/

# Copy MCP servers
COPY mcp_servers/ ./mcp_servers/

# Copy repo_setup module for dependency installation
COPY repo_setup/ ./repo_setup/

# Copy repo-setup.yaml configuration
# Same clean-clone fallback as workflows.yaml above: repo-setup.yaml is
# gitignored, repo-setup.example.yaml is tracked.
COPY repo-setup*.yaml ./
RUN [ -f repo-setup.yaml ] || cp repo-setup.example.yaml repo-setup.yaml

# Hook scripts
COPY hooks/langfuse_hook.py /app/hooks/langfuse_hook.py
RUN chmod +x /app/hooks/langfuse_hook.py

COPY services/sandbox_executor/requirements.txt ./
RUN pip3 install --no-cache-dir -r requirements.txt

# Sandbox Executor source code
COPY services/sandbox_executor/ /app/services/sandbox_executor/

# Copy plugins and skills to /app/ for seeding into bind-mounted ~/.claude/
RUN mkdir -p /app/plugins /app/skills
COPY plugins/ /app/plugins/
COPY skills/ /app/skills/

# Entrypoint seeds plugins/skills into ~/.claude/ on first run
COPY services/sandbox_executor/entrypoint.sh /app/entrypoint.sh
RUN sed -i 's/\r$//' /app/entrypoint.sh && chmod +x /app/entrypoint.sh

# Create repo cache directory and set ownership to bot user
RUN mkdir -p /var/cache/repos && chown -R bot:bot /var/cache/repos

# Create agent memory directory structure and set ownership to bot user
RUN mkdir -p /home/bot/.claude/projects && chown -R bot:bot /home/bot/.claude/projects

# Set ownership to bot user
RUN chown -R bot:bot /app /home/bot/.claude

# Give bot user passwordless sudo for installing language runtimes
RUN echo "bot ALL=(ALL) NOPASSWD: ALL" >> /etc/sudoers

# Set USER environment variable for the bot user
ENV USER=bot

# Switch to non-root user
USER bot

WORKDIR /app

# Default command runs the sandbox worker (docker-compose overrides this)
CMD ["python", "-m", "services.sandbox_executor.sandbox_worker"]
