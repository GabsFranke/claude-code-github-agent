FROM python:3.12-slim

WORKDIR /app

# Copy shared module
COPY shared/ ./shared/

# Copy workflows module
COPY workflows/ ./workflows/
# The glob matches the tracked workflows.example.yaml plus the operator's
# workflows.yaml when it exists. workflows.yaml is gitignored, so the
# fallback is what keeps this image buildable from a clean clone.
COPY workflows*.yaml ./
RUN [ -f workflows.yaml ] || cp workflows.example.yaml workflows.yaml
COPY prompts/ ./prompts/

# Copy webhook service
COPY services/webhook/requirements.txt ./services/webhook/
RUN pip install --no-cache-dir -r services/webhook/requirements.txt

COPY services/webhook/ ./services/webhook/

WORKDIR /app/services/webhook

CMD ["python", "main.py"]
