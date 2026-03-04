FROM python:3.11-slim

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
    && rm -rf /var/lib/apt/lists/*

# The sandbox needs the shared agent definitions and shared utilities
COPY subagents/ ./subagents/
COPY shared/ ./shared/

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

WORKDIR /app
EXPOSE 8000

# Run uvicorn using the top-level module path so Python can find both 'services' and 'subagents'
CMD ["uvicorn", "services.sandbox_executor.main:app", "--host", "0.0.0.0", "--port", "8000"]
