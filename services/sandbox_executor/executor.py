import asyncio
import json
import logging
import os

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    ResultMessage,
    TextBlock,
)

from shared import SDKError, SDKTimeoutError
from subagents import AGENTS

logger = logging.getLogger(__name__)


# We recreate the ObservabilityManager logic here inside the sandbox
def setup_langfuse_hooks() -> dict:
    span_id = os.getenv("CURRENT_SPAN_ID")
    langfuse_public_key = os.getenv("LANGFUSE_PUBLIC_KEY")
    langfuse_secret_key = os.getenv("LANGFUSE_SECRET_KEY")

    if not (langfuse_public_key and langfuse_secret_key):
        return {}

    async def langfuse_stop_hook_async(input_data, _tool_use_id, _context):
        error_msg = None
        process = None
        try:
            hook_payload = json.dumps(input_data)
            process = await asyncio.create_subprocess_exec(
                "python3",
                "/app/hooks/langfuse_hook.py",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={
                    "TRACE_TO_LANGFUSE": "true",
                    "LANGFUSE_PUBLIC_KEY": langfuse_public_key,
                    "LANGFUSE_SECRET_KEY": langfuse_secret_key,
                    "LANGFUSE_HOST": os.getenv("LANGFUSE_HOST", "http://langfuse:3000"),
                    "LANGFUSE_BASE_URL": os.getenv(
                        "LANGFUSE_HOST", "http://langfuse:3000"
                    ),
                    "CC_LANGFUSE_DEBUG": "true",
                    "PARENT_SPAN_ID": span_id or "",
                    "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                    "HOME": os.environ.get("HOME", "/root"),
                },
            )

            try:
                _stdout, stderr = await asyncio.wait_for(
                    process.communicate(input=hook_payload.encode()), timeout=30.0
                )
                if process.returncode != 0:
                    logger.warning(f"Langfuse hook failed: {stderr.decode()}")
                else:
                    return {"success": True}
            except TimeoutError:
                logger.warning("Langfuse hook timed out after 30s")
                process.kill()
                await process.wait()

        except Exception as e:
            logger.warning(f"Error running Langfuse hook: {e}")
            error_msg = str(e)
        finally:
            # Ensure process is cleaned up if it exists and hasn't been waited on
            if process and process.returncode is None:
                try:
                    process.kill()
                    await process.wait()
                except ProcessLookupError:
                    pass  # Expected - process already terminated
                except OSError as e:
                    logger.warning(f"Failed to cleanup Langfuse hook process: {e}")
                except Exception as e:
                    logger.error(
                        f"Unexpected error cleaning up Langfuse hook process: {e}",
                        exc_info=True,
                    )

        return {"success": False, "error": error_msg}

    return {
        "Stop": [HookMatcher(matcher="*", hooks=[langfuse_stop_hook_async])],
        "SubagentStop": [HookMatcher(matcher="*", hooks=[langfuse_stop_hook_async])],
    }


async def execute_sandbox_request(
    prompt: str,
    github_token: str,
    repo: str,
    issue_number: int,
    user: str,
    auto_review: bool,
    auto_triage: bool,
) -> str:
    """Execute the Claude Agent SDK inside the sandbox"""

    # 1. Setup Environment
    # The sandbox executor container is given ANTHROPIC_API_KEY globally in docker-compose.
    # We don't need to manually configure it as the SDK picks it up.

    # Set working directory to current directory to keep temp files accessible
    working_dir = os.getcwd()
    os.environ["CLAUDE_TEMP_DIR"] = working_dir
    os.environ["TMPDIR"] = working_dir

    # 2. Build Options (Equivalent of MCPConfigurationBuilder)
    mcp_servers = {
        "github": {
            "type": "http",
            "url": "https://api.githubcopilot.com/mcp",
            "headers": {"Authorization": f"Bearer {github_token}"},
        }
    }

    hooks = setup_langfuse_hooks()

    # Sandbox container expects plugins mapped to /app/plugins
    options = ClaudeAgentOptions(
        allowed_tools=["Task", "Bash", "mcp__github__*"],
        permission_mode="acceptEdits",
        mcp_servers=mcp_servers,  # type: ignore[arg-type]
        agents=AGENTS,
        plugins=[{"type": "local", "path": "/app/plugins/pr-review-toolkit"}],
        hooks=hooks,
        max_turns=50,
        cwd=working_dir,  # Set working directory for SDK operations
    )

    # 3. Execute
    logger.info("Starting sandbox SDK execution...")
    response_parts = []

    try:
        async with asyncio.timeout(1800):  # 30 minutes
            async with ClaudeSDKClient(options=options) as client:
                await client.query(prompt)

                async for message in client.receive_messages():
                    if isinstance(message, AssistantMessage):
                        for block in message.content:
                            if isinstance(block, TextBlock):
                                response_parts.append(block.text)
                    elif isinstance(message, ResultMessage):
                        break

    except TimeoutError as e:
        raise SDKTimeoutError(
            "Claude Agent SDK execution timed out after 30 minutes in sandbox"
        ) from e
    except Exception as e:
        raise SDKError(f"Failed to execute Claude Agent SDK in sandbox: {e}") from e

    response = "\n".join(response_parts)
    if not response or not response.strip():
        raise SDKError("Claude Agent SDK returned empty response in sandbox")

    logger.info("Sandbox SDK completed successfully")
    return response
