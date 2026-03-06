import logging
import os

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from shared.logging_utils import setup_logging

from .executor import execute_sandbox_request

setup_logging(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

app = FastAPI(title="Claude Agent SDK Sandbox Executor")


class ExecutionRequest(BaseModel):
    prompt: str
    github_token: str
    repo: str
    issue_number: int
    user: str
    auto_review: bool = False
    auto_triage: bool = False


@app.post("/execute")
async def execute(request: ExecutionRequest):
    """Execute the Claude Agent SDK in a sterile sandbox environment."""
    logger.info(
        f"Sandbox executing request for {request.repo} issue #{request.issue_number}"
    )

    try:
        response = await execute_sandbox_request(
            prompt=request.prompt,
            github_token=request.github_token,
            repo=request.repo,
            issue_number=request.issue_number,
            user=request.user,
            auto_review=request.auto_review,
            auto_triage=request.auto_triage,
        )
        return {"response": response, "status": "success"}
    except Exception as e:
        logger.error(f"Sandbox execution failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/health")
def health():
    return {"status": "healthy"}
