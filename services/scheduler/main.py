"""FastAPI entry point for the Scheduler service."""

import asyncio
import hmac
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from shared.logging_utils import setup_logging

from .config import get_scheduler_config
from .scheduler import WorkflowScheduler

# Load configuration
config = get_scheduler_config()

# Configure logging
setup_logging(level=config.log_level)
logger = logging.getLogger(__name__)

# Upper bound on how far ahead a one-shot may be scheduled. The jobstore is
# in memory, so an unbounded horizon is unbounded heap with no back-pressure.
MAX_SCHEDULE_HORIZON_DAYS = 365

# Initialize scheduler
workflow_scheduler = WorkflowScheduler()


async def watch_workflows_config(scheduler: WorkflowScheduler, file_path: Path):
    """Background task to watch workflows.yaml and hot-reload schedules.

    This implements config hot-reloading without external library dependencies,
    making it extremely lightweight and portable.
    """
    if not file_path.exists():
        logger.warning(f"Workflows config file not found for watching: {file_path}")
        return

    try:
        last_mtime = file_path.stat().st_mtime
        logger.info(
            f"Started config file watcher for: {file_path} (mtime: {last_mtime})"
        )
    except OSError as e:
        logger.error(f"Failed to stat config file: {e}")
        return

    while scheduler._running:
        await asyncio.sleep(5)
        try:
            if file_path.exists():
                current_mtime = file_path.stat().st_mtime
                if current_mtime != last_mtime:
                    logger.info(
                        "workflows.yaml change detected! Hot-reloading schedules..."
                    )
                    await scheduler.load_schedules()
                    last_mtime = current_mtime
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error checking workflows.yaml status: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for starting and stopping the scheduler gracefully."""
    # Start scheduler
    await workflow_scheduler.start()

    # Start config hot-reload file watcher
    workflows_path = Path(__file__).parent.parent.parent / "workflows.yaml"
    watcher_task = asyncio.create_task(
        watch_workflows_config(workflow_scheduler, workflows_path)
    )

    # Write initial healthy state to health check file
    try:
        health_path = Path(config.health_check_file)
        health_path.parent.mkdir(parents=True, exist_ok=True)
        health_path.write_text(
            "healthy=1\nservice=scheduler\nmessage=Scheduler is running\n",
            encoding="utf-8",
        )
    except OSError as e:
        logger.warning(f"Failed to write initial health file: {e}")

    yield

    # Stop file watcher
    watcher_task.cancel()
    try:
        await watcher_task
    except asyncio.CancelledError:
        pass

    # Stop scheduler
    await workflow_scheduler.stop()

    # Write unhealthy/stopped state to health check file
    try:
        health_path = Path(config.health_check_file)
        if health_path.exists():
            health_path.write_text(
                "healthy=0\nservice=scheduler\nmessage=Scheduler has stopped\n",
                encoding="utf-8",
            )
    except OSError as e:
        logger.warning(f"Failed to write final health file: {e}")


app = FastAPI(title="ClaudeCodeGitHubAgent Scheduler Service", lifespan=lifespan)


@app.get("/")
async def root():
    """Root endpoint."""
    return {"status": "ClaudeCodeGitHubAgent scheduler service is running"}


@app.get("/health")
async def health():
    """Health check endpoint."""
    # Determine if scheduler is running
    is_running = workflow_scheduler._running
    status = "healthy" if is_running else "unhealthy"

    # Get active jobs
    jobs = []
    if is_running:
        for job in workflow_scheduler.scheduler.get_jobs():
            jobs.append(
                {
                    "id": job.id,
                    "next_run_time": (
                        job.next_run_time.isoformat() if job.next_run_time else None
                    ),
                }
            )

    # urlopen in the compose healthcheck only raises on a non-2xx status, so
    # returning a plain dict here reported the container healthy while the
    # scheduler was dead and firing nothing at all.
    #
    # Note this does NOT by itself restart the container: plain Compose acts on
    # process exit, not on healthcheck state (restart-on-unhealthy is a Swarm
    # behaviour). What it buys is an accurate signal for `docker ps`, external
    # monitoring and any supervisor that reads health — the previous 200
    # actively lied to all three.
    if not is_running:
        return JSONResponse(
            status_code=503,
            content={
                "status": status,
                "service": "scheduler",
                "scheduler_running": is_running,
                "active_schedules": jobs,
            },
        )

    return {
        "status": status,
        "service": "scheduler",
        "scheduler_running": is_running,
        "active_schedules": jobs,
    }


class OneShotScheduleRequest(BaseModel):
    workflow_name: str = Field(..., description="Name of the workflow to trigger")
    repo: str = Field(..., description="Repository full name (owner/repo)")
    run_at: datetime = Field(
        ...,
        description=(
            "Timezone-aware datetime when the workflow should execute. Must be "
            f"in the future and within {MAX_SCHEDULE_HORIZON_DAYS} days."
        ),
    )
    issue_number: int | None = Field(
        default=None, description="Optional issue or PR number"
    )
    ref: str = Field(default="main", description="Target git ref/branch")
    user: str = Field(
        default="scheduler", description="User who triggered this schedule"
    )
    user_query: str = Field(
        default="", description="Optional query instruction for the workflow"
    )

    @field_validator("run_at")
    @classmethod
    def validate_run_at(cls, v: datetime) -> datetime:
        """Require a tz-aware run_at inside a bounded future window.

        A naive value would be read as UTC by the scheduler's timezone, which
        silently differs from the container-local reading callers may expect.
        A past run_at inside misfire_grace_time fires immediately rather than
        being dropped, and an unbounded horizon lets a looping agent grow the
        in-memory jobstore with no back-pressure.
        """
        if v.tzinfo is None or v.tzinfo.utcoffset(v) is None:
            raise ValueError(
                "run_at must be timezone-aware (e.g. 2026-06-01T12:00:00Z)"
            )
        now = datetime.now(UTC)
        if v <= now:
            raise ValueError("run_at must be in the future")
        if v - now > timedelta(days=MAX_SCHEDULE_HORIZON_DAYS):
            raise ValueError(f"run_at must be within {MAX_SCHEDULE_HORIZON_DAYS} days")
        return v


# Bound once at import rather than in the argument default: a call in a
# default is evaluated at definition time, which flake8-bugbear flags as
# B008. The alias is spelled out so the header name does not rely on
# FastAPI's underscore-to-hyphen conversion of the parameter name.
_INTERNAL_TOKEN_HEADER = Header(default="", alias="X-Internal-Token")


def require_internal_token(
    x_internal_token: str = _INTERNAL_TOKEN_HEADER,
) -> None:
    """Reject one-shot scheduling requests without the shared internal secret.

    The scheduler publishes no ports, but every container on the compose
    network can reach it — including sandbox agents whose prompts come from
    attacker-authored issue and PR text. Without this an injected
    "schedule workflow X on repo Y" instruction becomes a real job on a repo
    the commenter has no access to, stamped with the App's own installation
    id. Fail closed: an unset secret denies rather than allows.
    """
    expected = config.scheduler_internal_token
    if not expected:
        logger.error(
            "SCHEDULER_INTERNAL_TOKEN is not configured — refusing one-shot "
            "scheduling. Set it to enable the endpoint."
        )
        raise HTTPException(status_code=503, detail="one-shot scheduling disabled")
    # Compare as bytes: compare_digest rejects str with any non-ASCII
    # character, and uvicorn decodes headers as latin-1, so a header byte
    # above 0x7F would raise TypeError and surface as a 500 instead of 401.
    if not hmac.compare_digest(
        x_internal_token.encode("utf-8"), expected.encode("utf-8")
    ):
        raise HTTPException(status_code=401, detail="unauthorized")


async def _validate_one_shot_target(request: "OneShotScheduleRequest") -> None:
    """Reject unknown workflows and repos outside the installation.

    Authentication alone bounds *who* may schedule; this bounds *what* they
    may schedule, so a caller holding the token still cannot reach a repo
    the GitHub App was never installed on.
    """
    # Direct attribute access, not getattr with a default: WorkflowEngine
    # always defines `workflows`, and a default would both hide its absence
    # (every request would 400) and erase the type for static analysis.
    known = workflow_scheduler.workflow_engine.workflows
    if request.workflow_name not in known:
        raise HTTPException(
            status_code=400, detail=f"unknown workflow: {request.workflow_name}"
        )

    # Same resolution the cron path uses for repos: ["*"], behind a short
    # TTL so an admission check does not spend App API quota per request.
    installed = await workflow_scheduler.installation_repositories()
    if not installed:
        # Credentials missing or the API call failed — we cannot prove the
        # repo is in scope, so we do not enqueue.
        raise HTTPException(
            status_code=503, detail="cannot resolve installation repositories"
        )
    if request.repo not in installed:
        raise HTTPException(
            status_code=403, detail=f"repo not in installation: {request.repo}"
        )


@app.post("/schedule/one-shot", dependencies=[Depends(require_internal_token)])
async def schedule_one_shot(request: OneShotScheduleRequest):
    """Schedule a workflow execution for a single specific time in the future."""
    await _validate_one_shot_target(request)

    try:
        job_id = await workflow_scheduler.schedule_one_shot(
            workflow_name=request.workflow_name,
            repo=request.repo,
            run_at=request.run_at,
            issue_number=request.issue_number,
            ref=request.ref,
            user=request.user,
            user_query=request.user_query,
        )
        return {
            "status": "success",
            "message": "One-shot workflow scheduled successfully",
            "job_id": job_id,
            "run_at": request.run_at.isoformat(),
        }
    except Exception as e:
        logger.error(f"Failed to schedule one-shot workflow: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=config.port)
