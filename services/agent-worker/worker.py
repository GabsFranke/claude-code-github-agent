"""Claude Code worker that processes GitHub requests from message queue."""
import os
import sys
import logging
import subprocess
import json
import time
from pathlib import Path
from langfuse import Langfuse
import jwt
import requests

# Add parent directory to path for shared imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from shared.queue import get_queue

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize Langfuse client
langfuse = None
if os.getenv('LANGFUSE_PUBLIC_KEY') and os.getenv('LANGFUSE_SECRET_KEY'):
    langfuse = Langfuse(
        public_key=os.getenv('LANGFUSE_PUBLIC_KEY'),
        secret_key=os.getenv('LANGFUSE_SECRET_KEY'),
        host=os.getenv('LANGFUSE_HOST', 'https://cloud.langfuse.com')
    )
    logger.info("Langfuse observability enabled")
else:
    logger.info("Langfuse not configured - skipping observability")


# Cache for GitHub App token
_github_token_cache = {'token': None, 'expires_at': 0}


def get_github_app_token():
    """Generate a GitHub App installation access token (cached for 10 minutes)"""
    # Check cache first
    if _github_token_cache['token'] and time.time() < _github_token_cache['expires_at']:
        return _github_token_cache['token']
    
    app_id = os.getenv('GITHUB_APP_ID')
    private_key = os.getenv('GITHUB_PRIVATE_KEY')
    installation_id = os.getenv('GITHUB_INSTALLATION_ID')
    
    if not all([app_id, private_key, installation_id]):
        logger.info("GitHub App credentials not found, falling back to PAT")
        return os.getenv('GITHUB_PAT')
    
    try:
        # Generate JWT
        now = int(time.time())
        payload = {
            'iat': now,
            'exp': now + 600,  # 10 minutes
            'iss': app_id
        }
        
        jwt_token = jwt.encode(payload, private_key, algorithm='RS256')
        
        # Get installation access token
        headers = {
            'Authorization': f'Bearer {jwt_token}',
            'Accept': 'application/vnd.github.v3+json'
        }
        
        response = requests.post(
            f'https://api.github.com/app/installations/{installation_id}/access_tokens',
            headers=headers,
            timeout=10
        )
        
        if response.status_code != 201:
            logger.error(f"Failed to get installation token: {response.status_code} {response.text}")
            logger.info("Falling back to PAT")
            return os.getenv('GITHUB_PAT')
        
        token = response.json()['token']
        
        # Cache token for 9 minutes (expires in 10, but refresh early)
        _github_token_cache['token'] = token
        _github_token_cache['expires_at'] = time.time() + 540
        
        logger.info("Successfully generated GitHub App installation token")
        return token
        
    except Exception as e:
        logger.error(f"Error generating GitHub App token: {e}")
        logger.info("Falling back to PAT")
        return os.getenv('GITHUB_PAT')


def setup_claude_code_settings():
    """Configure Claude Code settings from environment variables"""
    import json
    from pathlib import Path
    
    # Claude Code settings file location
    settings_file = Path.home() / '.claude' / 'settings.json'
    
    # Create .claude directory if it doesn't exist
    settings_file.parent.mkdir(parents=True, exist_ok=True)
    
    # Load existing settings or create new
    settings = {}
    if settings_file.exists():
        try:
            with open(settings_file, 'r') as f:
                settings = json.load(f)
        except Exception as e:
            logger.warning(f"Could not read existing settings: {e}")
    
    # Configure permissions to allow MCP tools
    settings['permissions'] = {
        "allow": [
            "Read",
            "Write",
            "Edit",
            "Bash",
            "Glob",
            "Grep",
            "Task",
            "mcp__github"  # Allow GitHub MCP tools
        ],
        "deny": [],  # Must have deny array
        "ask": []  # Empty ask means auto-approve allowed tools
    }
    
    # Enable all project MCP servers
    settings['enableAllProjectMcpServers'] = True
    
    # Configure Langfuse Stop hook
    langfuse_public_key = os.getenv('LANGFUSE_PUBLIC_KEY')
    langfuse_secret_key = os.getenv('LANGFUSE_SECRET_KEY')
    
    if langfuse_public_key and langfuse_secret_key:
        settings['hooks'] = {
            "Stop": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "python3 /app/langfuse_hook.py",
                            "timeout": 30
                        }
                    ]
                }
            ]
        }
        logger.info("Configured Langfuse Stop hook for Claude Code")
    
    # Check if we have custom env vars to apply
    custom_env = {}
    
    if os.getenv('ANTHROPIC_BASE_URL'):
        custom_env['ANTHROPIC_BASE_URL'] = os.getenv('ANTHROPIC_BASE_URL')
    
    if os.getenv('ANTHROPIC_DEFAULT_HAIKU_MODEL'):
        custom_env['ANTHROPIC_DEFAULT_HAIKU_MODEL'] = os.getenv('ANTHROPIC_DEFAULT_HAIKU_MODEL')
    
    if os.getenv('ANTHROPIC_DEFAULT_SONNET_MODEL'):
        custom_env['ANTHROPIC_DEFAULT_SONNET_MODEL'] = os.getenv('ANTHROPIC_DEFAULT_SONNET_MODEL')
    
    if os.getenv('ANTHROPIC_DEFAULT_OPUS_MODEL'):
        custom_env['ANTHROPIC_DEFAULT_OPUS_MODEL'] = os.getenv('ANTHROPIC_DEFAULT_OPUS_MODEL')
    
    # Add Langfuse env vars for the hook
    if langfuse_public_key and langfuse_secret_key:
        custom_env['TRACE_TO_LANGFUSE'] = 'true'
        custom_env['LANGFUSE_PUBLIC_KEY'] = langfuse_public_key
        custom_env['LANGFUSE_SECRET_KEY'] = langfuse_secret_key
        custom_env['LANGFUSE_HOST'] = os.getenv('LANGFUSE_HOST', 'http://langfuse:3000')
        custom_env['LANGFUSE_BASE_URL'] = os.getenv('LANGFUSE_HOST', 'http://langfuse:3000')
    
    # Update env section if we have custom vars
    if custom_env:
        if 'env' not in settings:
            settings['env'] = {}
        settings['env'].update(custom_env)
    
    # Write settings
    try:
        with open(settings_file, 'w') as f:
            json.dump(settings, f, indent=2)
        logger.info(f"Updated Claude Code settings with permissions and custom env vars")
    except Exception as e:
        logger.error(f"Failed to write Claude Code settings: {e}")
        raise


def login_claude_code():
    """Login to Claude Code using API key"""
    anthropic_key = os.getenv('ANTHROPIC_API_KEY') or os.getenv('ANTHROPIC_AUTH_TOKEN')
    if not anthropic_key:
        raise ValueError("ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN environment variable not set")
    
    try:
        # Check if already logged in
        result = subprocess.run(
            ['claude', '--version'],
            capture_output=True,
            text=True,
            timeout=10,
            env={
                **os.environ,
                'ANTHROPIC_API_KEY': anthropic_key,
                'ANTHROPIC_AUTH_TOKEN': anthropic_key
            }
        )
        
        # Try a simple command to check auth
        result = subprocess.run(
            ['claude', 'mcp', 'list'],
            capture_output=True,
            text=True,
            timeout=10,
            env={
                **os.environ,
                'ANTHROPIC_API_KEY': anthropic_key,
                'ANTHROPIC_AUTH_TOKEN': anthropic_key
            }
        )
        
        if 'Not logged in' in result.stdout or 'Not logged in' in result.stderr:
            logger.info("Claude Code not logged in, attempting login...")
            # Run login command
            login_result = subprocess.run(
                ['claude', 'login', '--api-key', anthropic_key],
                capture_output=True,
                text=True,
                timeout=30,
                env={
                    **os.environ,
                    'ANTHROPIC_API_KEY': anthropic_key,
                    'ANTHROPIC_AUTH_TOKEN': anthropic_key
                }
            )
            
            if login_result.returncode != 0:
                logger.error(f"Login failed: {login_result.stdout} {login_result.stderr}")
                raise RuntimeError(f"Claude Code login failed: {login_result.stderr or login_result.stdout}")
            
            logger.info("Claude Code login successful")
        else:
            logger.info("Claude Code already authenticated")
        
    except subprocess.TimeoutExpired:
        logger.error("Timeout while checking Claude Code login")
        raise
    except Exception as e:
        logger.error(f"Error checking Claude Code login: {e}")
        raise


def setup_github_mcp():
    """Configure Claude Code to use GitHub's official MCP server (one-time setup)"""
    github_token = get_github_app_token()
    if not github_token:
        raise ValueError("No GitHub authentication available (neither App nor PAT)")
    
    try:
        # Remove existing config if present
        subprocess.run(
            ['claude', 'mcp', 'remove', 'github'],
            capture_output=True,
            timeout=10
        )
        
        # Add GitHub MCP server using remote HTTP endpoint
        logger.info("Configuring GitHub MCP server with GitHub App token...")
        
        # Use the command-line approach with scope
        result = subprocess.run(
            [
                'claude', 'mcp', 'add', 
                '--scope', 'local',
                '--transport', 'http',
                'github',
                'https://api.githubcopilot.com/mcp',
                '-H', f'Authorization: Bearer {github_token}'
            ],
            capture_output=True,
            text=True,
            timeout=30
        )
        
        if result.returncode != 0:
            logger.error(f"Failed to add MCP server: {result.stderr}")
            raise RuntimeError(f"MCP configuration failed: {result.stderr}")
        
        logger.info("GitHub MCP server configured successfully")
        
        # Now manually edit the config to add autoApprove and disable parallel calls
        import json
        from pathlib import Path
        
        config_file = Path.home() / '.claude' / 'mcp.json'
        if config_file.exists():
            with open(config_file, 'r') as f:
                config = json.load(f)
            
            # Add autoApprove to github server
            if 'github' in config.get('mcpServers', {}):
                config['mcpServers']['github']['autoApprove'] = ['*']
                # Add settings to prevent parallel tool calls for review operations
                config['mcpServers']['github']['settings'] = {
                    'sequentialReviewComments': True
                }
                
                with open(config_file, 'w') as f:
                    json.dump(config, f, indent=2)
                
                logger.info("Added auto-approve and sequential review settings to GitHub MCP server config")
        
    except subprocess.TimeoutExpired:
        logger.error("Timeout while configuring GitHub MCP server")
        raise
    except Exception as e:
        logger.error(f"Unexpected error configuring MCP: {e}")
        raise


def run_claude_code(repo: str, issue_number: int, command: str, auto_review: bool = False, auto_triage: bool = False) -> str:
    """Run Claude Code CLI to process the GitHub issue"""
    
    if auto_review:
        # Specific prompt for automatic PR reviews - matches Gemini Code Assist pattern
        prompt = f"""You are reviewing PR #{issue_number} in {repo}.

Provide a thorough code review following Gemini Code Assist's two-step approach:

STEP 1: Post a summary comment
- Use add_issue_comment to post a "Code Review" summary in the conversation tab
- Include overall assessment and key findings
- This is a regular issue comment

STEP 2: Add inline review comments (ONLY if there are specific code issues)
Use the THREE-STEP review workflow:

a) Create pending review:
   - Tool: pull_request_review_write
   - method: "create"
   - Do NOT include "event" parameter (this keeps it pending)
   - Do NOT include "comments" array
   - body: Brief note like "Detailed review findings"

b) Add each inline comment:
   - Tool: add_comment_to_pending_review
   - Call this tool ONCE for each inline comment you want to add
   - Parameters:
     * path: file path (e.g., "backend/helloworld.ts")
     * line: the line number in the NEW version of the file
     * side: "RIGHT" (for new code)
     * body: your comment with severity (e.g., "**High Priority**: Add JSDoc comment")
   - Make these calls SEQUENTIALLY, not in parallel

c) Submit the review:
   - Tool: pull_request_review_write
   - method: "submit_pending"
   - event: "COMMENT" (or "REQUEST_CHANGES" if critical)
   - body: Final summary

Focus on:
- Code quality and best practices
- Potential bugs or issues
- Security concerns
- Performance considerations

If code looks good, just post the summary comment (Step 1) without inline review."""
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
    
    logger.info(f"Running Claude Code for {repo} issue #{issue_number} (auto_review={auto_review}, auto_triage={auto_triage})")
    
    # Create Langfuse generation span for Claude CLI execution
    if langfuse:
        # Determine model name from env vars or use default
        model_name = (
            os.getenv('ANTHROPIC_DEFAULT_SONNET_MODEL') or 
            os.getenv('ANTHROPIC_DEFAULT_OPUS_MODEL') or 
            "claude-3-5-sonnet-20241022"
        )
        
        with langfuse.start_as_current_observation(
            name="claude_cli_execution",
            as_type="generation",
            model=model_name,
            model_parameters={"command": "claude -p"}
        ) as generation:
            generation.update(
                input=prompt,
                metadata={
                    "repo": repo,
                    "issue_number": issue_number,
                    "auto_review": auto_review,
                    "auto_triage": auto_triage,
                    "command": command,
                    "base_url": os.getenv('ANTHROPIC_BASE_URL', 'default'),
                    "model_override": os.getenv('ANTHROPIC_DEFAULT_SONNET_MODEL', 'none')
                }
            )
            
            try:
                response = _execute_claude_cli(prompt, repo)
                
                generation.update(
                    output=response,
                    level="DEFAULT"
                )
                
                logger.info("Claude Code completed successfully")
                return response
                
            except Exception as e:
                logger.error(f"Error running Claude Code: {e}", exc_info=True)
                generation.update(
                    level="ERROR",
                    status_message=str(e)
                )
                raise
    else:
        # No Langfuse, just execute
        return _execute_claude_cli(prompt, repo)


def _execute_claude_cli(prompt: str, repo: str) -> str:
    """Execute Claude CLI command"""
    env = {
        **os.environ,
        'GITHUB_PAT': get_github_app_token(),  # Use GitHub App token
    }
    
    # Add Anthropic API key (try both env var names)
    anthropic_key = os.getenv('ANTHROPIC_API_KEY') or os.getenv('ANTHROPIC_AUTH_TOKEN')
    if anthropic_key:
        env['ANTHROPIC_API_KEY'] = anthropic_key
        env['ANTHROPIC_AUTH_TOKEN'] = anthropic_key
    
    # Add optional Anthropic overrides
    if os.getenv('ANTHROPIC_BASE_URL'):
        env['ANTHROPIC_BASE_URL'] = os.getenv('ANTHROPIC_BASE_URL')
    
    # Check if repo has CLAUDE.md and include it
    claude_md_content = get_claude_md(repo)
    if claude_md_content:
        logger.info(f"Found CLAUDE.md in {repo}, including in context")
        prompt = f"{claude_md_content}\n\n---\n\n{prompt}"
    
    logger.info("Executing Claude CLI command (this may take several minutes for large PRs)...")
    
    result = subprocess.run(
        ['claude', '-p', prompt],
        capture_output=True,
        text=True,
        timeout=600,  # 10 minute timeout
        env=env
    )
    
    if result.returncode != 0:
        error_msg = f"Claude Code execution failed: {result.stderr or result.stdout}"
        logger.error(f"Claude Code failed with return code {result.returncode}")
        logger.error(f"stdout: {result.stdout[:500]}")
        logger.error(f"stderr: {result.stderr[:500]}")
        
        # Check for specific MCP tool errors
        if 'mcp__github' in result.stderr or 'mcp__github' in result.stdout:
            logger.error("GitHub MCP tool error detected - check token permissions and review workflow")
        
        raise RuntimeError(error_msg)
    
    logger.info("Claude Code completed successfully")
    return result.stdout


def get_claude_md(repo: str) -> str:
    """Fetch CLAUDE.md from repository if it exists"""
    try:
        import requests
        
        github_token = get_github_app_token()
        if not github_token:
            return ""
        
        # Try to fetch CLAUDE.md from repo
        url = f"https://api.github.com/repos/{repo}/contents/CLAUDE.md"
        headers = {
            "Authorization": f"Bearer {github_token}",
            "Accept": "application/vnd.github.v3.raw"
        }
        
        response = requests.get(url, headers=headers, timeout=10)
        
        if response.status_code == 200:
            return response.text
        
        return ""
        
    except Exception as e:
        logger.debug(f"Could not fetch CLAUDE.md: {e}")
        return ""


def process_request(repo: str, issue_number: int, command: str, auto_review: bool = False, auto_triage: bool = False):
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
                    "auto_triage": auto_triage
                },
                metadata={
                    "repo": repo,
                    "issue_number": issue_number
                }
            )
            
            try:
                # Run Claude Code
                response = run_claude_code(repo, issue_number, command, auto_review, auto_triage)
                
                logger.info(f"Claude Code response: {response[:200]}..." if len(response) > 200 else f"Claude Code response: {response}")
                logger.info("Request processed successfully")
                
                # Update trace with success
                trace.update(
                    output={"response": response[:500]},  # Truncate for storage
                    metadata={
                        "status": "success",
                        "response_length": len(response)
                    }
                )
                
                return response
                
            except Exception as e:
                logger.error(f"Error processing request: {e}", exc_info=True)
                
                # Update trace with error
                trace.update(
                    output={"error": str(e)},
                    metadata={"status": "error"},
                    level="ERROR"
                )
                
                raise
            finally:
                # Flush Langfuse events
                langfuse.flush()
    else:
        # No Langfuse, just run the request
        try:
            response = run_claude_code(repo, issue_number, command, auto_review, auto_triage)
            logger.info(f"Claude Code response: {response[:200]}...")
            logger.info("Request processed successfully")
            return response
        except Exception as e:
            logger.error(f"Error processing request: {e}", exc_info=True)
            raise


def main():
    """Main worker loop - subscribes to queue and processes messages"""
    logger.info("Starting Claude Code worker (queue mode)")
    
    # Login to Claude Code
    try:
        login_claude_code()
    except Exception as e:
        logger.error(f"Failed to login to Claude Code: {e}")
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
    
    # Initialize queue
    queue = get_queue()
    
    # Subscribe and process messages
    async def callback(message: dict):
        try:
            repo = message.get('repository')
            issue_number = message.get('issue_number')
            command = message.get('command')
            auto_review = message.get('auto_review', False)
            auto_triage = message.get('auto_triage', False)
            
            if not all([repo, issue_number, command]):
                logger.error(f"Invalid message format: {message}")
                return
            
            process_request(repo, issue_number, command, auto_review, auto_triage)
            
        except Exception as e:
            logger.error(f"Error in callback: {e}", exc_info=True)
    
    # Start listening (blocking)
    import asyncio
    asyncio.run(queue.subscribe(callback))


if __name__ == "__main__":
    main()
