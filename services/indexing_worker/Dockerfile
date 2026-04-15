FROM python:3.12-slim

# Create non-root user matching the repo-cache volume ownership
RUN useradd -m -u 1000 -s /bin/bash bot

WORKDIR /app

# git is required for worktree operations from bare repo cache
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

# Shared utilities
COPY shared/ ./shared/

# Worker source
COPY services/indexing_worker/ /app/services/indexing_worker/

# Install Python dependencies
COPY services/indexing_worker/requirements.txt ./requirements.txt
RUN pip3 install --no-cache-dir -r requirements.txt

# Set ownership
RUN chown -R bot:bot /app /home/bot

ENV USER=bot

USER bot

CMD ["python", "-m", "services.indexing_worker.indexing_worker"]
