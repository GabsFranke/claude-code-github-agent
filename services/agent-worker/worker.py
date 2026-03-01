"""Claude Code worker that processes GitHub requests from message queue."""

import os
import sys
import logging
import json
import time
import threading
import subprocess
import asyncio
from pathlib import Path
from langfuse import Langfuse
import jwt
import requests
import anyio
from claude_agent_sdk import (
    ClaudeSDKClient,
    ClaudeAgentOptions,
    AssistantMessage,
    TextBlock,
    HookMatcher,
)

# Add parent directory to path for shared imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from shared.queue import get_queue

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize Langfuse client
langfuse = None
if os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY"):
    langfuse = Langfuse(
        public_key=os.getenv("LANGFUSE_PUBLIC_KEY"),
        secret_key=os.getenv("LANGFUSE_SECRET_KEY"),
        host=os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com"),
    )
    logger.info("Langfuse observability enabled")
else:
    logger.info("Langfuse not configured - skipping observability")


# Cache for GitHub App token with thread-safe locking
_github_token_cache = {"token": None, "expires_at": 0}
_github_token_lock = threading.Lock()


def validate_github_private_key(private_key: str) -> bool:
    """Validate that the private key is in valid PEM format"""
    if not private_key:
        return False

    # Check for PEM header/footer
    if not ("-----BEGIN" in private_key and "-----END" in private_key):
        return False

    # Check for RSA or PRIVATE KEY markers
    valid_markers = ["RSA PRIVATE KEY", "PRIVATE KEY", "EC PRIVATE KEY"]
    if not any(marker in private_key for marker in valid_markers):
        return False

    return True


def get_github_app_token():
    """Generate a GitHub App installation access token (cached for 10 minutes)"""
    # Check cache first (with lock to prevent race conditions)
    with _github_token_lock:
        if (
            _github_token_cache["token"]
            and time.time() < _github_token_cache["expires_at"]
        ):
            return _github_token_cache["token"]

    app_id = os.getenv("GITHUB_APP_ID")
    private_key = os.getenv("GITHUB_PRIVATE_KEY")
    installation_id = os.getenv("GITHUB_INSTALLATION_ID")

    if not all([app_id, private_key, installation_id]):
        raise ValueError(
            "GitHub App credentials required: GITHUB_APP_ID, GITHUB_INSTALLATION_ID, and GITHUB_PRIVATE_KEY must be set"
        )

    # Validate private key format
    if not validate_github_private_key(private_key):
        logger.error("GITHUB_PRIVATE_KEY is not in valid PEM format")
        logger.error(
            "Expected format: -----BEGIN RSA PRIVATE KEY----- ... -----END RSA PRIVATE KEY-----"
        )
        logger.error(
            "Make sure the key includes header, footer, and is properly formatted"
        )
        raise ValueError("Invalid GITHUB_PRIVATE_KEY format")

    try:
        # Generate JWT
        now = int(time.time())
        payload = {"iat": now, "exp": now + 600, "iss": app_id}  # 10 minutes

        jwt_token = jwt.encode(payload, private_key, algorithm="RS256")

        # Get installation access token with retry logic
        headers = {
            "Authorization": f"Bearer {jwt_token}",
            "Accept": "application/vnd.github.v3+json",
        }

        max_retries = 3
        for attempt in range(max_retries):
            try:
                response = requests.post(
                    f"https://api.github.com/app/installations/{installation_id}/access_tokens",
                    headers=headers,
                    timeout=10,
                )

                if response.status_code != 201:
                    logger.error(
                        f"Failed to get installation token: {response.status_code} {response.text}"
                    )
                    if attempt < max_retries - 1:
                        time.sleep(2**attempt)  # Exponential backoff
                        continue
                    raise RuntimeError(
                        f"Failed to get GitHub App installation token: {response.status_code}"
                    )

                token = response.json()["token"]

                # Cache token for 9 minutes (expires in 10, but refresh early)
                with _github_token_lock:
                    _github_token_cache["token"] = token
                    _github_token_cache["expires_at"] = time.time() + 540

                logger.info("Successfully generated GitHub App installation token")
                return token

            except requests.exceptions.RequestException as e:
                logger.warning(
                    f"GitHub API request failed (attempt {attempt + 1}/{max_retries}): {e}"
                )
                if attempt < max_retries - 1:
                    time.sleep(2**attempt)  # Exponential backoff
                else:
                    raise

    except Exception as e:
        logger.error(f"Error generating GitHub App token: {e}")
        raise


def get_fresh_github_token():
    """Get a valid GitHub token, refreshing if needed (for long-running sessions)"""
    with _github_token_lock:
        # Refresh if token expires in less than 1 minute
        if (
            not _github_token_cache["token"]
            or time.time() >= _github_token_cache["expires_at"] - 60
        ):
            logger.info("Refreshing GitHub token (expired or expiring soon)")
            _github_token_cache["token"] = None  # Force refresh

    return get_github_app_token()


def setup_claude_code_settings():
    """Configure Claude Code settings from environment variables"""
    import json
    from pathlib import Path

    # Claude Code settings file location
    settings_file = Path.home() / ".claude" / "settings.json"

    # Create .claude directory if it doesn't exist
    settings_file.parent.mkdir(parents=True, exist_ok=True)

    # Load existing settings or create new
    settings = {}
    if settings_file.exists():
        try:
            with open(settings_file, "r") as f:
                settings = json.load(f)
        except Exception as e:
            logger.warning(f"Could not read existing settings: {e}")

    # Configure permissions to allow MCP tools
    settings["permissions"] = {
        "allow": [
            "Task",  # Required for subagent delegation
            "mcp__github",  # Allow GitHub MCP tools
        ],
        "deny": [],  # Must have deny array
        "ask": [],  # Empty ask means auto-approve allowed tools
    }

    # Enable all project MCP servers
    settings["enableAllProjectMcpServers"] = True

    # Check if we have custom env vars to apply
    custom_env = {}

    if os.getenv("ANTHROPIC_BASE_URL"):
        custom_env["ANTHROPIC_BASE_URL"] = os.getenv("ANTHROPIC_BASE_URL")

    if os.getenv("ANTHROPIC_DEFAULT_HAIKU_MODEL"):
        custom_env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = os.getenv(
            "ANTHROPIC_DEFAULT_HAIKU_MODEL"
        )

    if os.getenv("ANTHROPIC_DEFAULT_SONNET_MODEL"):
        custom_env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = os.getenv(
            "ANTHROPIC_DEFAULT_SONNET_MODEL"
        )

    if os.getenv("ANTHROPIC_DEFAULT_OPUS_MODEL"):
        custom_env["ANTHROPIC_DEFAULT_OPUS_MODEL"] = os.getenv(
            "ANTHROPIC_DEFAULT_OPUS_MODEL"
        )

    # Add Langfuse env vars for the hook script
    langfuse_public_key = os.getenv("LANGFUSE_PUBLIC_KEY")
    langfuse_secret_key = os.getenv("LANGFUSE_SECRET_KEY")

    if langfuse_public_key and langfuse_secret_key:
        custom_env["TRACE_TO_LANGFUSE"] = "true"
        custom_env["LANGFUSE_PUBLIC_KEY"] = langfuse_public_key
        custom_env["LANGFUSE_SECRET_KEY"] = langfuse_secret_key
        custom_env["LANGFUSE_HOST"] = os.getenv("LANGFUSE_HOST", "http://langfuse:3000")
        custom_env["LANGFUSE_BASE_URL"] = os.getenv(
            "LANGFUSE_HOST", "http://langfuse:3000"
        )
        custom_env["CC_LANGFUSE_DEBUG"] = "true"  # Enable debug logging
        logger.info("Configured Langfuse environment variables for hook script")

    # Update env section if we have custom vars
    if custom_env:
        if "env" not in settings:
            settings["env"] = {}
        settings["env"].update(custom_env)

    # Write settings
    try:
        with open(settings_file, "w") as f:
            json.dump(settings, f, indent=2)
        logger.info(
            f"Updated Claude Code settings with permissions and custom env vars"
        )
    except Exception as e:
        logger.error(f"Failed to write Claude Code settings: {e}")
        raise


def login_claude_code():
    """Setup authentication for Claude Agent SDK"""
    anthropic_key = os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")
    if not anthropic_key:
        raise ValueError(
            "ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN environment variable not set"
        )

    # Set environment variable for SDK to use
    os.environ["ANTHROPIC_API_KEY"] = anthropic_key
    logger.info("Claude Agent SDK authentication configured")


def setup_github_mcp():
    """Configure Claude Code to use GitHub's official MCP server (one-time setup)"""
    github_token = get_github_app_token()

    try:
        # Create MCP config file directly at user level
        import json
        from pathlib import Path

        mcp_config_file = Path.home() / ".claude" / "mcp.json"
        mcp_config_file.parent.mkdir(parents=True, exist_ok=True)

        # Create or update MCP configuration
        mcp_config = {
            "mcpServers": {
                "github": {
                    "type": "http",
                    "url": "https://api.githubcopilot.com/mcp",
                    "headers": {"Authorization": f"Bearer {github_token}"},
                }
            }
        }

        with open(mcp_config_file, "w") as f:
            json.dump(mcp_config, f, indent=2)

        logger.info(f"Created MCP config at {mcp_config_file}")
        logger.info("GitHub MCP server configured successfully with auto-approve")

    except Exception as e:
        logger.error(f"Unexpected error configuring MCP: {e}")
        raise


async def run_claude_code(
    repo: str,
    issue_number: int,
    command: str,
    auto_review: bool = False,
    auto_triage: bool = False,
) -> str:
    """Run Claude Agent SDK to process the GitHub issue"""

    if auto_review:
        # Flexible prompt that lets Claude decide which agents to use
        prompt = f"""Review PR #{issue_number} in {repo} using specialized agents as needed.

You have access to these specialized subagents via the Task tool:
- architecture-reviewer: Design patterns, SOLID principles, API design
- security-reviewer: Vulnerabilities, auth issues, data exposure
- bug-hunter: Potential bugs, edge cases, error handling
- code-quality-reviewer: Style, readability, maintainability

Workflow:

1. Read the PR (use GitHub MCP tools)
2. Decide which agents to use based on changes:
   - Docs only → code-quality-reviewer or none
   - Auth/API changes → security-reviewer, bug-hunter, architecture-reviewer
   - Bug fixes → bug-hunter, code-quality-reviewer
   - Major refactor → all agents
3. Delegate to chosen agents using Task tool: "agent-name, review PR #{issue_number} in {repo}"
4. Post summary comment (add_issue_comment):
   - Overall assessment
   - Which agents used and why
   - Findings by category with severity counts
   - Recommendation (approve/request changes)
5. Add inline comments if issues found:
   a) Create pending review (pull_request_review_write, method="create", no event param)
   b) Add comments SEQUENTIALLY (add_comment_to_pending_review, top 15-20 issues)
   c) Submit review (pull_request_review_write, method="submit_pending", event="COMMENT"/"REQUEST_CHANGES"/"APPROVE")

Start by reading the PR."""
    elif auto_triage:
        # Specific prompt for automatic issue triage
        prompt = f"""You are triaging issue #{issue_number} in {repo}.

Analyze the issue and:
1. Add appropriate labels (bug, enhancement, documentation, question, etc.)
2. Assess priority and complexity
3. Suggest next steps or ask clarifying questions if needed
4. Post a comment with your analysis

Use the GitHub MCP tools to read the issue details and add labels."""
    else:
        # Minimal, flexible prompt for manual commands
        prompt = f"""You are a helpful coding assistant with access to the {repo} repository via GitHub MCP tools.

Issue #{issue_number}: {command}

Help the user with their request. You can:
- Answer questions about the code
- Review and analyze code
- Create branches and PRs
- Make code changes
- Provide explanations

Always respond by commenting on the issue with your findings or actions taken."""

    logger.info(
        f"Running Claude Agent SDK for {repo} issue #{issue_number} (auto_review={auto_review}, auto_triage={auto_triage})"
    )

    # Check if repo has CLAUDE.md and include it
    claude_md_content = get_claude_md(repo)
    if claude_md_content:
        logger.info(f"Found CLAUDE.md in {repo}, including in context")
        prompt = f"{claude_md_content}\n\n---\n\n{prompt}"

    # Create Langfuse generation span for Claude SDK execution
    if langfuse:
        # Determine model name from env vars or use default
        model_name = (
            os.getenv("ANTHROPIC_DEFAULT_SONNET_MODEL")
            or os.getenv("ANTHROPIC_DEFAULT_OPUS_MODEL")
            or "claude-3-5-sonnet-20241022"
        )

        with langfuse.start_as_current_observation(
            name="claude_sdk_execution",
            as_type="generation",
            model=model_name,
            model_parameters={"sdk_version": "agent-sdk"},
        ) as generation:
            generation.update(
                input=prompt,
                metadata={
                    "repo": repo,
                    "issue_number": issue_number,
                    "auto_review": auto_review,
                    "auto_triage": auto_triage,
                    "command": command,
                    "base_url": os.getenv("ANTHROPIC_BASE_URL", "default"),
                    "model_override": os.getenv(
                        "ANTHROPIC_DEFAULT_SONNET_MODEL", "none"
                    ),
                },
            )

            try:
                response = await _execute_claude_sdk(prompt, repo)

                generation.update(output=response, level="DEFAULT")

                logger.info("Claude Agent SDK completed successfully")
                return response

            except Exception as e:
                logger.error(f"Error running Claude Agent SDK: {e}", exc_info=True)
                generation.update(level="ERROR", status_message=str(e))
                raise
    else:
        # No Langfuse, just execute
        return await _execute_claude_sdk(prompt, repo)


async def _execute_claude_sdk(prompt: str, repo: str) -> str:
    """Execute Claude Agent SDK"""

    # Ensure ANTHROPIC_API_KEY is set before initializing SDK
    anthropic_key = os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")
    if anthropic_key:
        os.environ["ANTHROPIC_API_KEY"] = anthropic_key

    # Add optional Anthropic overrides
    if os.getenv("ANTHROPIC_BASE_URL"):
        os.environ["ANTHROPIC_BASE_URL"] = os.getenv("ANTHROPIC_BASE_URL")

    # Use fresh token to handle long-running sessions
    github_token = get_fresh_github_token()

    # Validate MCP server URL (security check)
    mcp_url = "https://api.githubcopilot.com/mcp"
    allowed_mcp_hosts = ["api.githubcopilot.com", "api.github.com"]

    from urllib.parse import urlparse

    parsed_url = urlparse(mcp_url)
    if parsed_url.hostname not in allowed_mcp_hosts:
        raise ValueError(f"MCP server URL not in allowlist: {parsed_url.hostname}")
    if parsed_url.scheme != "https":
        raise ValueError(f"MCP server must use HTTPS, got: {parsed_url.scheme}")

    # Setup MCP server configuration (HTTP transport)
    mcp_servers = {
        "github": {
            "type": "http",  # Explicitly specify HTTP transport
            "url": mcp_url,
            "headers": {"Authorization": f"Bearer {github_token}"},
        }
    }

    # Setup Langfuse hooks if configured
    hooks = {}
    langfuse_public_key = os.getenv("LANGFUSE_PUBLIC_KEY")
    langfuse_secret_key = os.getenv("LANGFUSE_SECRET_KEY")

    if langfuse_public_key and langfuse_secret_key:
        # Create async version of the hook
        async def langfuse_stop_hook_async(input_data, tool_use_id, context):
            """Async hook that runs after agent stops to send data to Langfuse"""
            try:
                hook_payload = json.dumps(input_data)

                # Use asyncio subprocess to avoid blocking event loop
                process = await asyncio.create_subprocess_exec(
                    "python3",
                    "/app/hooks/langfuse_hook.py",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env={
                        # Only pass required env vars (security fix)
                        "TRACE_TO_LANGFUSE": "true",
                        "LANGFUSE_PUBLIC_KEY": langfuse_public_key,
                        "LANGFUSE_SECRET_KEY": langfuse_secret_key,
                        "LANGFUSE_HOST": os.getenv(
                            "LANGFUSE_HOST", "http://langfuse:3000"
                        ),
                        "LANGFUSE_BASE_URL": os.getenv(
                            "LANGFUSE_HOST", "http://langfuse:3000"
                        ),
                        "CC_LANGFUSE_DEBUG": "true",
                        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                        "HOME": os.environ.get("HOME", "/root"),
                    },
                )

                # Wait for completion with timeout
                try:
                    stdout, stderr = await asyncio.wait_for(
                        process.communicate(input=hook_payload.encode()), timeout=30.0
                    )

                    if process.returncode != 0:
                        logger.warning(f"Langfuse hook failed: {stderr.decode()}")
                    else:
                        logger.debug("Langfuse hook completed successfully")

                except asyncio.TimeoutError:
                    logger.warning("Langfuse hook timed out after 30s")
                    process.kill()
                    await process.wait()

            except Exception as e:
                logger.warning(f"Error running Langfuse hook: {e}")

            return {}

        # Configure hooks for Stop and SubagentStop events
        hooks = {
            "Stop": [HookMatcher(matcher="*", hooks=[langfuse_stop_hook_async])],
            "SubagentStop": [
                HookMatcher(matcher="*", hooks=[langfuse_stop_hook_async])
            ],
        }
        logger.info(
            "Configured Langfuse Stop and SubagentStop hooks for Claude Agent SDK"
        )

    # Configure agent options with MCP servers and filesystem agents
    options = ClaudeAgentOptions(
        allowed_tools=[
            "Task",
            "mcp__github__*",
        ],  # Task for subagents, all GitHub MCP tools
        permission_mode="acceptEdits",  # Auto-accept file edits
        mcp_servers=mcp_servers,
        hooks=hooks,
        max_turns=50,  # Allow multiple turns for complex tasks
        setting_sources=["user"]
    )

    logger.info(
        "Executing Claude Agent SDK (this may take several minutes for large PRs)..."
    )

    # Collect response
    response_parts = []

    try:
        # Add timeout to prevent infinite loops (30 minutes max)
        async with asyncio.timeout(1800):  # 30 minutes
            async with ClaudeSDKClient(options=options) as client:
                await client.query(prompt)

                async for message in client.receive_response():
                    # Log init message to see loaded agents
                    if (
                        hasattr(message, "type")
                        and message.type == "system"
                        and hasattr(message, "subtype")
                        and message.subtype == "init"
                    ):
                        if hasattr(message, "data") and "agents" in message.data:
                            agents = message.data.get("agents", [])
                            logger.info(
                                f"Loaded {len(agents)} custom agents: {[a.get('name') for a in agents]}"
                            )
                        else:
                            logger.warning("No custom agents found in init message")

                    if isinstance(message, AssistantMessage):
                        for block in message.content:
                            if isinstance(block, TextBlock):
                                response_parts.append(block.text)
                                logger.debug(f"Received text: {block.text[:100]}...")

        response = "\n".join(response_parts)

        # Validate response is not empty
        if not response or not response.strip():
            error_msg = "Claude Agent SDK returned empty response"
            logger.error(error_msg)
            raise RuntimeError(error_msg)

        logger.info("Claude Agent SDK completed successfully")
        return response

    except asyncio.TimeoutError:
        error_msg = "Claude Agent SDK execution timed out after 30 minutes"
        logger.error(error_msg)
        raise RuntimeError(error_msg)

    except Exception as e:
        error_msg = f"Claude Agent SDK execution failed: {str(e)}"
        logger.error(error_msg)
        logger.error(f"Error details: {e}", exc_info=True)
        raise RuntimeError(error_msg)


def get_claude_md(repo: str) -> str:
    """Fetch CLAUDE.md from repository if it exists"""
    try:
        import requests

        github_token = get_github_app_token()

        # Try to fetch CLAUDE.md from repo
        url = f"https://api.github.com/repos/{repo}/contents/CLAUDE.md"
        headers = {
            "Authorization": f"Bearer {github_token}",
            "Accept": "application/vnd.github.v3.raw",
        }

        response = requests.get(url, headers=headers, timeout=10)

        if response.status_code == 200:
            return response.text

        return ""

    except Exception as e:
        logger.debug(f"Could not fetch CLAUDE.md: {e}")
        return ""


async def process_request(
    repo: str,
    issue_number: int,
    command: str,
    auto_review: bool = False,
    auto_triage: bool = False,
):
    """Process a single agent request"""
    logger.info(f"Processing request for {repo} issue #{issue_number}")
    logger.info(f"Command: {command}")

    # Create Langfuse trace using context manager
    if langfuse:
        with langfuse.start_as_current_span(name="github_agent_request") as trace:
            trace.update(
                input={
                    "repo": repo,
                    "issue_number": issue_number,
                    "command": command,
                    "auto_review": auto_review,
                    "auto_triage": auto_triage,
                },
                metadata={"repo": repo, "issue_number": issue_number},
            )

            try:
                # Run Claude Agent SDK
                response = await run_claude_code(
                    repo, issue_number, command, auto_review, auto_triage
                )

                logger.info(
                    f"Claude Agent SDK response: {response[:200]}..."
                    if len(response) > 200
                    else f"Claude Agent SDK response: {response}"
                )
                logger.info("Request processed successfully")

                # Update trace with success
                trace.update(
                    output={"response": response[:500]},  # Truncate for storage
                    metadata={"status": "success", "response_length": len(response)},
                )

                return response

            except Exception as e:
                logger.error(f"Error processing request: {e}", exc_info=True)

                # Update trace with error
                trace.update(
                    output={"error": str(e)},
                    metadata={"status": "error"},
                    level="ERROR",
                )

                raise
            finally:
                # Proper Langfuse shutdown (flush + shutdown)
                if langfuse:
                    langfuse.flush()
                    langfuse.shutdown()
    else:
        # No Langfuse, just run the request
        try:
            response = await run_claude_code(
                repo, issue_number, command, auto_review, auto_triage
            )
            logger.info(f"Claude Agent SDK response: {response[:200]}...")
            logger.info("Request processed successfully")
            return response
        except Exception as e:
            logger.error(f"Error processing request: {e}", exc_info=True)
            raise


def main():
    """Main worker loop - subscribes to queue and processes messages"""
    logger.info("Starting Claude Agent SDK worker (queue mode)")

    # Setup authentication
    try:
        login_claude_code()
    except Exception as e:
        logger.error(f"Failed to setup Claude Agent SDK authentication: {e}")
        raise

    # Setup Claude Code settings from environment variables
    try:
        setup_claude_code_settings()
    except Exception as e:
        logger.error(f"Failed to setup Claude Code settings: {e}")
        logger.info("Continuing with default settings")

    # Setup GitHub MCP server configuration
    try:
        setup_github_mcp()
    except Exception as e:
        logger.error(f"Failed to setup GitHub MCP: {e}")
        logger.info("Continuing anyway - will retry on first request")

    # Verify custom agents are available
    agents_dir = Path.home() / ".claude" / "agents"
    if agents_dir.exists():
        agent_files = list(agents_dir.glob("*.md"))
        if agent_files:
            agent_names = [f.stem for f in agent_files]
            logger.info(f"Found {len(agent_files)} custom agents: {agent_names}")
        else:
            logger.error(f"No agent files found in {agents_dir}")
            raise RuntimeError(
                f"Subagent discovery failed: No .md files in {agents_dir}"
            )
    else:
        logger.error(f"Agents directory not found: {agents_dir}")
        raise RuntimeError(
            f"Subagent discovery failed: Directory {agents_dir} does not exist"
        )

    # Initialize queue
    queue = get_queue()

    # Subscribe and process messages
    async def callback(message: dict):
        try:
            repo = message.get("repository")
            issue_number = message.get("issue_number")
            command = message.get("command")
            auto_review = message.get("auto_review", False)
            auto_triage = message.get("auto_triage", False)

            if not all([repo, issue_number, command]):
                logger.error(f"Invalid message format: {message}")
                return

            await process_request(repo, issue_number, command, auto_review, auto_triage)

        except Exception as e:
            logger.error(f"Error in callback: {e}", exc_info=True)

    # Start listening (blocking)
    import asyncio

    asyncio.run(queue.subscribe(callback))


if __name__ == "__main__":
    main()
