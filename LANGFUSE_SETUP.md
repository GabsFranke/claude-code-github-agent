# Langfuse Integration for Agent Observability

This project includes a self-hosted Langfuse v3 instance to trace and debug your Claude Code agent executions.

## What You'll See in Langfuse

For each GitHub issue/PR processed, you'll see detailed traces with:

1. **High-level trace** (`github_agent_request`)
   - Repository and issue number
   - Command type (manual, auto-review, auto-triage)
   - Input parameters and final output
   - Success/failure status

2. **Claude Code turns** (`Claude Code - Turn X`)
   - User prompts and assistant responses
   - Tool calls with inputs and outputs
   - Step-by-step reasoning
   - Individual tool execution details

This gives you complete visibility into what Claude Code is doing, which GitHub API calls it makes, and how it reasons through problems.

## Setup

Langfuse is pre-configured in `docker-compose.yml` with all required services.

### Start the Services

```bash
docker-compose up -d
```

This starts:
- PostgreSQL (metadata storage)
- ClickHouse (trace/observation storage)
- MinIO (S3-compatible blob storage)
- Langfuse worker (async processing)
- Langfuse web UI (http://localhost:7500)

### Login to Langfuse

1. Open http://localhost:7500
2. Login with:
   - Email: `admin@example.com`
   - Password: `admin123`
3. You'll see the auto-created project: "GitHub Agent Project"

### API Keys (Pre-configured)

The API keys are already set in `.env`:

```bash
LANGFUSE_PUBLIC_KEY=lf_pk_github_agent_public
LANGFUSE_SECRET_KEY=lf_sk_github_agent_secret
```

## How It Works

The integration uses two mechanisms:

1. **Python SDK** (in `worker.py`)
   - Creates high-level traces for each GitHub request
   - Tracks overall execution time and status

2. **Claude Code Stop Hook** (in `langfuse_hook.py`)
   - Runs after each Claude Code response
   - Parses the conversation transcript
   - Extracts tool calls, inputs, outputs, and reasoning
   - Sends detailed turn-by-turn traces to Langfuse

The hook is configured automatically in `~/.claude/settings.json` when the worker starts.

## Viewing Traces

1. Go to http://localhost:7500
2. Navigate to "Traces"
3. Look for:
   - `github_agent_request` - High-level request traces
   - `Claude Code - Turn X` - Detailed turn-by-turn execution
4. Click on a trace to see:
   - Full conversation history
   - Tool calls (GitHub API operations)
   - Tool inputs and outputs
   - Timing information


## Data Persistence

All Langfuse data is stored in Docker volumes:
- `langfuse-db-data` - PostgreSQL metadata
- `langfuse-clickhouse-data` - Trace/observation data
- `langfuse-minio-data` - Blob storage

To reset all data:

```bash
docker-compose down -v  # Warning: deletes all traces
docker-compose up -d
```

## Security Notes

For production deployment:

1. Change `NEXTAUTH_SECRET`:
   ```bash
   openssl rand -base64 32
   ```

2. Change `SALT`:
   ```bash
   openssl rand -base64 32
   ```

3. Change `ENCRYPTION_KEY` (must be 64 hex characters):
   ```bash
   openssl rand -hex 32
   ```

4. Update database passwords in docker-compose.yml
5. Add authentication/firewall rules if exposing ports

## Troubleshooting

**No traces appearing**
- Verify API keys in `.env` are correct
- Check worker logs: `docker-compose logs -f worker`
- Verify `TRACE_TO_LANGFUSE=true` is set in worker environment
- Check hook logs: `docker-compose exec worker cat /root/.claude/state/langfuse_hook.log`

**Langfuse not starting**
- Check logs: `docker-compose logs langfuse`
- Verify all required services are running: `docker-compose ps`
- Check MinIO bucket was created: `docker-compose logs langfuse-minio`

**Database connection issues**
- Check PostgreSQL: `docker-compose logs langfuse-db`
- Check ClickHouse: `docker-compose logs langfuse-clickhouse`

**Port 7500 already in use**
- Change the port mapping in docker-compose.yml: `"7501:3000"`

## Disabling Langfuse

To run without observability:

1. Remove `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` from `.env`
2. Set `TRACE_TO_LANGFUSE=false` in `.env`
3. Restart: `docker-compose restart worker`

The Langfuse services will continue running but won't receive any traces.

