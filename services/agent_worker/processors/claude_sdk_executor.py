"""Claude SDK executor for handling SDK operations."""

import asyncio
import json
import logging
import os
import tempfile
from typing import TYPE_CHECKING, Optional

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
)

if TYPE_CHECKING:
    from shared import MultiRateLimiter

logger = logging.getLogger(__name__)


class ClaudeSDKExecutor:
    """Handles Claude Agent SDK execution and message processing."""

    def __init__(
        self,
        shutdown_event: asyncio.Event,
        rate_limiters: Optional["MultiRateLimiter"] = None,
    ):
        self.shutdown_event = shutdown_event
        self.rate_limiters = rate_limiters
        self.temp_files_to_cleanup: list[str] = []

    def setup_anthropic_environment(self) -> None:
        """Setup Anthropic API environment variables."""
        anthropic_key = os.getenv("ANTHROPIC_API_KEY") or os.getenv(
            "ANTHROPIC_AUTH_TOKEN"
        )
        if anthropic_key:
            os.environ["ANTHROPIC_API_KEY"] = anthropic_key

        base_url = os.getenv("ANTHROPIC_BASE_URL")
        if base_url:
            os.environ["ANTHROPIC_BASE_URL"] = base_url

    async def setup_vertex_ai_credentials(self) -> str | None:
        """Setup Vertex AI credentials if configured."""
        from shared.exceptions import ConfigurationError

        project_id = os.getenv("ANTHROPIC_VERTEX_PROJECT_ID")
        if not project_id:
            return None

        os.environ["ANTHROPIC_VERTEX_PROJECT_ID"] = project_id

        region = os.getenv("ANTHROPIC_VERTEX_REGION")
        if region:
            os.environ["ANTHROPIC_VERTEX_REGION"] = region

        credentials_json = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON")
        if not credentials_json:
            return None

        try:
            credentials_data = json.loads(credentials_json)

            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", delete=False
            ) as credentials_file:
                credentials_file_path = credentials_file.name
                json.dump(credentials_data, credentials_file)

            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = credentials_file_path
            self.temp_files_to_cleanup.append(credentials_file_path)
            logger.info("Configured Vertex AI credentials")
            return credentials_file_path

        except json.JSONDecodeError as e:
            logger.error(f"Invalid GOOGLE_APPLICATION_CREDENTIALS_JSON: {e}")
            raise ConfigurationError(
                "GOOGLE_APPLICATION_CREDENTIALS_JSON must be valid JSON"
            ) from e

    async def execute_sdk(self, prompt: str, options) -> str:
        """Execute Claude Agent SDK."""
        from shared.exceptions import SDKInitializationError, SDKTimeoutError

        # Apply rate limiting for Anthropic API
        if self.rate_limiters:
            logger.info("Acquiring Anthropic API rate limit...")
            await self.rate_limiters.acquire("anthropic", timeout=60.0)

        # Setup environment
        self.setup_anthropic_environment()

        # Configure Vertex AI if specified
        credentials_file_path = await self.setup_vertex_ai_credentials()

        try:
            logger.info("Executing Claude Agent SDK...")

            response_parts: list[str] = []

            try:
                async with asyncio.timeout(1800):  # 30 minutes
                    async with ClaudeSDKClient(options=options) as client:
                        await client.query(prompt)

                        async for message in client.receive_messages():
                            should_stop = self._process_sdk_message(
                                message, response_parts
                            )
                            if should_stop:
                                break

            except TimeoutError as e:
                raise SDKTimeoutError(
                    "Claude Agent SDK execution timed out after 30 minutes"
                ) from e
            except Exception as e:
                raise SDKInitializationError(
                    f"Failed to execute Claude Agent SDK: {e}"
                ) from e

            response = "\n".join(response_parts)

            if not response or not response.strip():
                raise SDKInitializationError("Claude Agent SDK returned empty response")

            logger.info("Claude Agent SDK completed successfully")
            return response

        finally:
            # Cleanup temporary credentials file
            if credentials_file_path:
                self.cleanup_temp_file(credentials_file_path)

    def _process_sdk_message(self, message, response_parts: list[str]) -> bool:
        """
        Process a single SDK message.

        Returns:
            True if processing should stop, False to continue
        """
        if self.shutdown_event.is_set():
            logger.warning("Shutdown requested, stopping SDK execution")
            return True

        if isinstance(message, SystemMessage):
            self._handle_system_message(message)

        elif isinstance(message, AssistantMessage):
            self._handle_assistant_message(message, response_parts)

        elif isinstance(message, ResultMessage):
            self._handle_result_message(message)
            return True

        return False

    def _handle_system_message(self, message: SystemMessage) -> None:
        """Handle system messages from SDK."""
        if message.subtype == "init" and hasattr(message, "data"):
            init_data = message.data
            if "plugins" in init_data:
                plugins = init_data.get("plugins", [])
                logger.info(f"Loaded {len(plugins)} plugins: {plugins}")

    def _handle_assistant_message(
        self, message: AssistantMessage, response_parts: list[str]
    ) -> None:
        """Handle assistant messages from SDK."""
        for block in message.content:
            if isinstance(block, TextBlock):
                response_parts.append(block.text)
                logger.info(f"Received text block: {block.text[:200]}...")
            elif isinstance(block, ToolUseBlock):
                logger.info(f"Tool use: {block.name} (id: {block.id})")

    def _handle_result_message(self, message: ResultMessage) -> None:
        """Handle result messages from SDK."""
        logger.info(
            f"Response complete - {message.num_turns} turns, "
            f"{message.duration_ms}ms"
        )
        if message.total_cost_usd:
            logger.info(f"Cost: ${message.total_cost_usd:.4f}")

    def cleanup_temp_file(self, file_path: str) -> None:
        """Cleanup a temporary file."""
        try:
            os.unlink(file_path)
            self.temp_files_to_cleanup.remove(file_path)
            logger.debug(f"Cleaned up temporary file: {file_path}")
        except FileNotFoundError:
            logger.debug(f"Temporary file already removed: {file_path}")
        except ValueError:
            logger.debug(f"Temporary file not in cleanup list: {file_path}")
        except OSError as e:
            logger.warning(f"Failed to cleanup temporary file {file_path}: {e}")

    async def cleanup(self):
        """Cleanup resources."""
        for temp_file in self.temp_files_to_cleanup:
            try:
                os.unlink(temp_file)
                logger.debug(f"Cleaned up temporary file: {temp_file}")
            except OSError as e:
                logger.warning(f"Failed to cleanup {temp_file}: {e}")
        self.temp_files_to_cleanup.clear()
