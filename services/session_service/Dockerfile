# --- Stage 1: build the React client -----------------------------------------
# The client is served by the Python service as static files, so it has to be
# compiled into the image. Building here keeps the image reproducible from a
# clean clone: nothing depends on a dist/ directory that only exists on a
# developer's machine (it is git-ignored).
FROM node:20-alpine AS client

WORKDIR /client
COPY services/session_service/client/package.json services/session_service/client/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY services/session_service/client/ ./
RUN npm run build

# --- Stage 2: the service ----------------------------------------------------
FROM python:3.12-slim

WORKDIR /app

# Copy and install dependencies
COPY services/session_service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code preserving the package path. main.py imports its
# siblings relatively (``from .config import ...``), so it must be imported as
# ``services.session_service.main``, the same way the tests import it.
# Flattening these into /app makes those imports fail at startup with
# "attempted relative import with no known parent package".
COPY services/__init__.py ./services/__init__.py
COPY services/session_service/ ./services/session_service/
COPY shared/ ./shared/

# The compiled client, where main.py's CLIENT_DIST expects it
COPY --from=client /client/dist ./services/session_service/client/dist

ENV PYTHONPATH=/app

# Expose session service port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

# Run with uvicorn
CMD ["uvicorn", "services.session_service.main:app", "--host", "0.0.0.0", "--port", "8000"]
