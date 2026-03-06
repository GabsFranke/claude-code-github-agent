FROM python:3.11-slim

WORKDIR /app

# Copy shared module
COPY shared/ ./shared/

# Copy workflows module
COPY workflows/ ./workflows/
COPY workflows.yaml ./workflows.yaml
COPY prompts/ ./prompts/

# Copy webhook service
COPY services/webhook/requirements.txt ./services/webhook/
RUN pip install --no-cache-dir -r services/webhook/requirements.txt

COPY services/webhook/ ./services/webhook/

WORKDIR /app/services/webhook

CMD ["python", "main.py"]
