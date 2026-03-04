FROM python:3.11-slim

WORKDIR /app

# Install git for repo syncing
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

COPY services/repo_sync/requirements.txt ./
RUN pip3 install --no-cache-dir -r requirements.txt

# The sync worker needs shared utilities
COPY shared/ ./shared/

# Sync worker source code
COPY services/repo_sync/ /app/services/repo_sync/

WORKDIR /app
CMD ["python", "-m", "services.repo_sync.sync_worker"]
