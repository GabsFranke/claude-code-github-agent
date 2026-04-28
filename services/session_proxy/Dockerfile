# syntax=docker/dockerfile:1

# ─── Stage 1: Build React client ─────────────────────────────────────────────
FROM node:20-slim AS client-builder

WORKDIR /build/client
COPY services/session_proxy/client/package*.json ./
RUN npm install --silent

COPY services/session_proxy/client/ ./
RUN npm run build

# ─── Stage 2: Python FastAPI server ──────────────────────────────────────────
FROM python:3.12-slim

WORKDIR /app

# Install Python deps
COPY services/session_proxy/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy shared library (session_stream, streaming_session)
COPY shared/ ./shared/

# Copy session_proxy service (for module execution)
COPY services/session_proxy/ ./services/session_proxy/

# Copy built React assets from stage 1
COPY --from=client-builder /build/client/dist ./services/session_proxy/client/dist

EXPOSE 8080

CMD ["python", "-m", "services.session_proxy.main"]
