FROM python:3.11-slim

WORKDIR /app

# Copy shared module
COPY shared/ ./shared/

# Copy agent-worker requirements and install
COPY services/agent-worker/requirements.txt ./
RUN pip3 install --no-cache-dir -r requirements.txt

# Copy agent-worker code
COPY services/agent-worker/worker.py ./services/agent-worker/

# Copy subagents package
COPY subagents/ ./subagents/

# Copy hooks
COPY hooks/langfuse_hook.py /app/hooks/langfuse_hook.py
RUN chmod +x /app/hooks/langfuse_hook.py

WORKDIR /app/services/agent-worker

CMD ["python3", "worker.py"]
