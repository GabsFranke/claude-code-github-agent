"""Centralized SDK execution for all workers.

All SDK invocations go through this module for consistency,
instrumentation, and observability.
"""

import asyncio
import logging
import os

from claude_agent_sdk import AssistantMessage, ClaudeSDKClient, ResultMessage, TextBlock

from shared import SDKError, SDKTimeoutError
from shared.sdk_factory import SDKOptionsBuilder

logger = logging.getLogger(__name__)

# Only enable SDK debug logging if SDK_DEBUG is set
sdk_debug = os.getenv("SDK_DEBUG", "false").lower() == "true"
if sdk_debug:
    logging.getLogger("claude_agent_sdk").setLevel(logging.DEBUG)
else:
    logging.getLogger("claude_agent_sdk").setLevel(logging.WARNING)


async def execute_sdk(
    prompt: str,
    options_builder: SDKOptionsBuilder,
    timeout: int | None = None,
    collect_text: bool = True,
) -> dict:
    """Execute Claude Agent SDK with given options.

    This is the single point of SDK execution for all workers (sandbox,
    retrospector, memory). Provides consistent error handling, timeout
    management, and observability.

    Args:
        prompt: User prompt to send to the agent
        options_builder: Pre-configured SDKOptionsBuilder instance
        timeout: Optional timeout in seconds (default: from env or 1800)
        collect_text: Whether to collect text blocks into response (default: True)

    Returns:
        dict with:
            - response: str (if collect_text=True, None otherwise)
            - num_turns: int
            - duration_ms: int
            - is_error: bool
            - messages: list (all messages received)

    Raises:
        SDKTimeoutError: If execution exceeds timeout
        SDKError: If SDK execution fails or returns empty response
    """
    options = options_builder.build()

    sdk_timeout = timeout or int(os.getenv("SDK_EXECUTION_TIMEOUT", "1800"))
    response_parts = []
    all_messages = []
    result_info = {
        "num_turns": 0,
        "duration_ms": 0,
        "is_error": False,
    }

    logger.info(f"Starting SDK execution (prompt: {len(prompt)} chars)...")
    logger.info(f"Model: {options.model}")

    # Only show detailed info if SDK_DEBUG is enabled
    if sdk_debug:
        logger.debug(f"Prompt preview: {prompt[:200]}...")
        logger.debug(f"Working directory: {options.cwd}")
        logger.debug(f"Setting sources: {options.setting_sources}")
        logger.debug(f"Allowed tools: {options.allowed_tools}")

        # Verify we can access the working directory
        try:
            files = os.listdir(options.cwd)
            logger.debug(f"Working directory contains {len(files)} items")
            logger.debug(f"First 10 items: {files[:10]}")
        except Exception as e:
            logger.error(f"Cannot access working directory: {e}")

    try:
        async with asyncio.timeout(sdk_timeout):
            async with ClaudeSDKClient(options=options) as client:
                logger.info("SDK client created, sending query...")
                await client.query(prompt)
                logger.info("Waiting for SDK response...")

                async for message in client.receive_messages():
                    all_messages.append(message)

                    if sdk_debug:
                        logger.debug(f"Received message type: {type(message).__name__}")

                    if isinstance(message, AssistantMessage):
                        logger.info(
                            f"Received response with {len(message.content)} blocks"
                        )
                        if collect_text:
                            for block in message.content:
                                if isinstance(block, TextBlock):
                                    response_parts.append(block.text)
                                    if sdk_debug:
                                        logger.debug(
                                            f"Text block content: {block.text[:200]}..."
                                        )

                    elif isinstance(message, ResultMessage):
                        result_info = {
                            "num_turns": message.num_turns,
                            "duration_ms": message.duration_ms,
                            "is_error": message.is_error,
                        }
                        logger.info(
                            f"SDK completed - {message.num_turns} turns, "
                            f"{message.duration_ms}ms, error={message.is_error}"
                        )
                        if sdk_debug:
                            logger.debug(
                                f"ResultMessage details: is_error={message.is_error}, "
                                f"subtype={message.subtype}"
                            )
                        break

                    elif sdk_debug:
                        # Log any other message types only in debug mode
                        logger.debug(
                            f"Received other message type: {type(message).__name__}"
                        )
                        if hasattr(message, "__dict__"):
                            logger.debug(f"Message content: {message.__dict__}")

    except TimeoutError as e:
        raise SDKTimeoutError(f"SDK execution timed out after {sdk_timeout}s") from e
    except Exception as e:
        raise SDKError(f"SDK execution failed: {e}") from e

    response = "\n".join(response_parts) if collect_text else None

    if collect_text and (not response or not response.strip()):
        raise SDKError("SDK returned empty response")

    logger.info(
        f"SDK execution complete - collected {len(response_parts)} response parts"
    )

    return {
        "response": response,
        "messages": all_messages,
        **result_info,
    }
