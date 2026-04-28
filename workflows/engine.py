"""YAML-driven workflow engine."""

import logging
import string
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import]
from pydantic import BaseModel, Field, ValidationError

from shared.session_store import ConversationConfig as ConversationConfigModel
from shared.thread_history import ThreadHistoryConfig
from shared.utils import resolve_path

logger = logging.getLogger(__name__)


class PromptConfig(BaseModel):
    """Prompt configuration for a workflow."""

    template: str = Field(..., description="Prompt template with placeholders")
    system_context: str | None = Field(
        None, description="System context (inline or .md file reference)"
    )


class EventTrigger(BaseModel):
    """A single event trigger with optional per-event filters."""

    event: str = Field(
        ..., description="GitHub event trigger (e.g., 'pull_request.opened')"
    )
    filters: dict[str, Any] = Field(
        default_factory=dict,
        description="Payload field filters specific to this event. "
        "Overrides workflow-level filters when present.",
    )


class TriggersConfig(BaseModel):
    """Trigger configuration for a workflow."""

    events: list[str | EventTrigger] = Field(
        default_factory=list, description="GitHub event triggers"
    )
    commands: list[str] = Field(default_factory=list, description="Command triggers")
    filters: dict[str, Any] = Field(
        default_factory=dict,
        description="Payload field filters (dot-path: expected_value). "
        "Applied to events without per-event filters. "
        "All filters must match for the workflow to trigger.",
    )


class ContextProfile(BaseModel):
    """Context configuration for structural context generation."""

    repomap_budget: int = Field(
        default=2048, description="Token budget for the repomap"
    )
    personalized: bool = Field(
        default=False,
        description="Whether to personalize repomap toward mentioned files",
    )
    include_test_files: bool = Field(
        default=True, description="Whether to include test files in personalization"
    )
    priority_focus: list[str] = Field(
        default_factory=list,
        description="Focus areas for repomap ranking (e.g., ['build_system', 'test_structure'])",
    )
    thread_history: ThreadHistoryConfig = Field(
        default_factory=lambda: ThreadHistoryConfig(),  # type: ignore[call-arg]
        description="Thread history injection configuration",
    )


class StreamingConfig(BaseModel):
    """Configuration for real-time session streaming (remote control).

    When enabled, the sandbox worker publishes SDK messages to Redis pub/sub
    and posts a live-view URL in the GitHub comment. The session_proxy service
    bridges those messages to a browser via WebSocket.

    Example workflows.yaml:
        workflows:
          review-pr:
            streaming:
              enabled: true
    """

    enabled: bool = Field(
        default=False,
        description="Enable real-time streaming for this workflow",
    )


class WorkflowConfig(BaseModel):
    """Configuration for a single workflow."""

    triggers: TriggersConfig = Field(..., description="Event and command triggers")
    prompt: PromptConfig = Field(..., description="Prompt configuration")
    description: str = Field(default="", description="Workflow description")
    skip_self: bool = Field(
        default=True,
        description="Skip events triggered by the bot itself (default: true)",
    )
    context: ContextProfile = Field(
        default_factory=ContextProfile,
        description="Context profile for structural context generation",
    )
    conversation: ConversationConfigModel = Field(
        default_factory=ConversationConfigModel,
        description="Conversation persistence settings",
    )
    streaming: StreamingConfig = Field(
        default_factory=StreamingConfig,
        description="Real-time session streaming settings",
    )





class WorkflowsConfig(BaseModel):
    """Root configuration containing all workflows."""

    workflows: dict[str, WorkflowConfig] = Field(
        ..., description="Map of workflow names to configurations"
    )


class WorkflowEngine:
    """Loads workflows from YAML and routes events/commands to prompts."""

    def _validate_workflow_names(self) -> None:
        """Validate workflow names follow naming conventions.

        Raises:
            ValueError: If workflow name is invalid
        """
        import re

        for name in self.workflows.keys():
            # Check for valid characters (lowercase, numbers, hyphens)
            if not re.match(r"^[a-z0-9]+(-[a-z0-9]+)*$", name):
                raise ValueError(
                    f"Invalid workflow name '{name}'. "
                    "Workflow names must be lowercase with hyphens (e.g., 'review-pr', 'fix-ci')"
                )

            # Check length
            if len(name) > 50:
                raise ValueError(
                    f"Workflow name '{name}' is too long (max 50 characters)"
                )

            # Check for reserved names
            reserved = ["test", "debug", "admin", "system"]
            if name in reserved:
                raise ValueError(
                    f"Workflow name '{name}' is reserved. Please choose a different name."
                )

    def _validate_system_context_files(self) -> None:
        """Validate that all referenced system context files exist.

        Raises:
            FileNotFoundError: If a referenced .md file doesn't exist
        """
        prompts_dir = Path(__file__).parent.parent / "prompts"
        for name, workflow in self.workflows.items():
            system_context = workflow.prompt.system_context
            if system_context and system_context.endswith(".md"):
                context_file = prompts_dir / system_context
                if not context_file.exists():
                    raise FileNotFoundError(
                        f"Workflow '{name}' references non-existent system context file: {context_file}"
                    )

    def _validate_templates(self) -> None:
        """Validate that all workflow templates are valid.

        Raises:
            ValueError: If template has invalid placeholders or syntax
        """
        valid_placeholders = {"repo", "issue_number", "user_query"}

        for name, workflow in self.workflows.items():
            template = workflow.prompt.template

            if not template:
                raise ValueError(f"Workflow '{name}' has empty template")

            # Parse template to find all placeholders
            try:
                field_names = [
                    field_name
                    for _, field_name, _, _ in string.Formatter().parse(template)
                    if field_name is not None
                ]
            except (ValueError, KeyError) as e:
                raise ValueError(
                    f"Workflow '{name}' has invalid template syntax: {e}"
                ) from e

            # Check for unknown placeholders
            unknown = set(field_names) - valid_placeholders
            if unknown:
                raise ValueError(
                    f"Workflow '{name}' template uses unknown placeholders: {unknown}. "
                    f"Valid placeholders are: {valid_placeholders}"
                )

            # Validate system context template if it's inline
            system_context = workflow.prompt.system_context
            if system_context and not system_context.endswith(".md"):
                # It's inline context, validate it too
                try:
                    context_fields = [
                        field_name
                        for _, field_name, _, _ in string.Formatter().parse(
                            system_context
                        )
                        if field_name is not None
                    ]
                    unknown_context = set(context_fields) - valid_placeholders
                    if unknown_context:
                        raise ValueError(
                            f"Workflow '{name}' system_context uses unknown placeholders: {unknown_context}"
                        )
                except (ValueError, KeyError) as e:
                    raise ValueError(
                        f"Workflow '{name}' has invalid system_context syntax: {e}"
                    ) from e

    def __init__(self, config_path: str | Path | None = None):
        """Initialize workflow engine from YAML config.

        Args:
            config_path: Path to workflows.yaml file (defaults to workflows.yaml in project root)
        """
        if config_path is None:
            config_path = Path(__file__).parent.parent / "workflows.yaml"
        config_path = Path(config_path)
        if not config_path.exists():
            raise FileNotFoundError(f"Workflow config not found: {config_path}")

        with open(config_path, encoding="utf-8") as f:
            raw_config = yaml.safe_load(f)

        # Validate configuration with Pydantic
        try:
            validated_config = WorkflowsConfig(**raw_config)
        except ValidationError as e:
            logger.error(f"Invalid workflow configuration in {config_path}")
            logger.error("Validation errors:")
            for error in e.errors():
                loc = " -> ".join(str(x) for x in error["loc"])
                logger.error(f"  {loc}: {error['msg']}")
            raise ValueError(
                f"Invalid workflow configuration: {e.error_count()} validation error(s). "
                "See logs for details."
            ) from e

        # Keep Pydantic models for type safety
        self.workflows: dict[str, WorkflowConfig] = validated_config.workflows

        # Validate workflow names follow conventions
        self._validate_workflow_names()

        # Build lookup tables for fast routing
        self._event_map: dict[str, str] = {}
        self._command_map: dict[str, str] = {}
        self._event_filters: dict[tuple[str, str], dict[str, Any]] = {}

        for workflow_name, workflow in self.workflows.items():
            # Map events to workflows, normalizing to EventTrigger
            for entry in workflow.triggers.events:
                if isinstance(entry, str):
                    trigger = EventTrigger(event=entry)
                else:
                    trigger = entry
                self._event_map[trigger.event] = workflow_name
                if trigger.filters:
                    self._event_filters[(workflow_name, trigger.event)] = (
                        trigger.filters
                    )
                logger.debug(
                    f"Mapped event '{trigger.event}' -> workflow '{workflow_name}'"
                )

            # Map commands to workflows
            for command in workflow.triggers.commands:
                self._command_map[command] = workflow_name
                logger.debug(
                    f"Mapped command '{command}' -> workflow '{workflow_name}'"
                )

        logger.info(
            f"Loaded {len(self.workflows)} workflows: {list(self.workflows.keys())}"
        )

        # Validate system context files exist at initialization
        self._validate_system_context_files()
        logger.info("All system context files validated")

        # Validate templates at initialization
        self._validate_templates()
        logger.info("All workflow templates validated")

    def get_workflow_for_event(
        self, event_type: str, action: str | None = None
    ) -> str | None:
        """Get workflow name for a GitHub event.

        Args:
            event_type: GitHub event type (e.g., "pull_request")
            action: Event action (e.g., "opened")

        Returns:
            Workflow name or None if no workflow handles this event
        """
        # Try with action first (e.g., "pull_request.opened")
        if action:
            key = f"{event_type}.{action}"
            if key in self._event_map:
                return self._event_map[key]

        # Try without action (e.g., "pull_request")
        if event_type in self._event_map:
            return self._event_map[event_type]

        return None

    def should_skip_self(
        self, workflow_name: str, event_actor: str, bot_username: str
    ) -> bool:
        """Check if workflow should skip events from the bot itself.

        Args:
            workflow_name: Name of the workflow
            event_actor: GitHub username who triggered the event (from webhook sender)
            bot_username: The bot's GitHub username

        Returns:
            True if event should be skipped (actor is bot and skip_self=true), False otherwise
        """
        if workflow_name not in self.workflows:
            return True  # Default to skipping if workflow not found

        workflow = self.workflows[workflow_name]

        # Skip only if skip_self is enabled AND the event actor is the bot
        return workflow.skip_self and event_actor == bot_username

    def get_workflow_for_command(self, command: str) -> str | None:
        """Get workflow name for a user command.

        Args:
            command: Command string (e.g., "/review")

        Returns:
            Workflow name or None if command not recognized
        """
        return self._command_map.get(command)

    def check_filters(
        self,
        workflow_name: str,
        payload: dict,
        event_key: str | None = None,
    ) -> bool:
        """Check if a payload matches the workflow's declarative filters.

        Per-event filters (stored in _event_filters) take precedence over
        workflow-level triggers.filters. Each filter is a dot-path key and
        expected value. If the expected value is a list, the actual value
        must be in that list. Otherwise exact equality is checked.
        All filters must pass (AND logic).

        Args:
            workflow_name: Name of the workflow.
            payload: The parsed webhook payload dict.
            event_key: The matched event key (e.g., "pull_request.opened").
                When provided, per-event filters are checked first.

        Returns:
            True if all filters match (or no filters defined), False otherwise.
        """
        if workflow_name not in self.workflows:
            logger.warning(
                "check_filters called with unknown workflow '%s' -- "
                "config or routing bug?",
                workflow_name,
            )
            return False

        # Per-event filters take precedence
        if event_key and (workflow_name, event_key) in self._event_filters:
            filters = self._event_filters[(workflow_name, event_key)]
        else:
            filters = self.workflows[workflow_name].triggers.filters

        if not filters:
            return True

        for dot_path, expected in filters.items():
            actual = resolve_path(payload, dot_path)
            expected_list = expected if isinstance(expected, list) else [expected]
            if actual not in expected_list:
                logger.info(
                    "Filter '%s' mismatch: got %r, expected one of %s",
                    dot_path,
                    actual,
                    expected_list,
                )
                logger.debug(
                    "Full filter context: workflow=%s event_key=%s payload_keys=%s",
                    workflow_name,
                    event_key,
                    list(payload.keys()),
                )
                return False

        return True

    def build_prompt(
        self,
        workflow_name: str,
        repo: str,
        issue_number: int | None = None,
        user_query: str = "",
        **kwargs: Any,
    ) -> tuple[str, str | None]:
        """Build the final prompt for Claude Agent SDK.

        Args:
            workflow_name: Name of workflow to execute
            repo: Repository full name (owner/repo)
            issue_number: Issue or PR number
            user_query: User-provided query/context
            **kwargs: Additional template variables

        Returns:
            Tuple of (user_prompt, system_context) for client.query()
        """
        if workflow_name not in self.workflows:
            raise ValueError(f"Unknown workflow: {workflow_name}")

        workflow = self.workflows[workflow_name]

        # Validate template placeholders to prevent injection
        template = workflow.prompt.template
        try:
            # Get all field names from template
            field_names = [
                field_name
                for _, field_name, _, _ in string.Formatter().parse(template)
                if field_name is not None
            ]

            # Escape user-provided input to prevent template injection
            # User query might contain {braces} which could cause issues
            safe_user_query = user_query.replace("{", "{{").replace("}", "}}")

            # Build safe variables dict
            safe_vars = {
                "repo": repo,
                "issue_number": issue_number or "",
                "user_query": safe_user_query,
                **kwargs,
            }

            # Validate all required fields are present
            for field_name in field_names:
                if field_name not in safe_vars:
                    raise ValueError(
                        f"Template requires field '{field_name}' but it was not provided"
                    )

            # Fill template with validated variables
            prompt = template.format(**safe_vars)

        except (KeyError, ValueError) as e:
            logger.error(
                f"Template formatting error in workflow '{workflow_name}': {e}"
            )
            raise ValueError(
                f"Invalid template in workflow '{workflow_name}': {e}"
            ) from e

        # Load system context if defined
        system_context = workflow.prompt.system_context
        final_system_context = None

        if system_context:
            # Check if it's a file reference (ends with .md)
            if system_context.endswith(".md"):
                context_file = Path(__file__).parent.parent / "prompts" / system_context
                try:
                    system_context = context_file.read_text(encoding="utf-8").strip()
                    logger.debug(f"Loaded system context from {context_file}")
                except FileNotFoundError as e:
                    # This should not happen due to initialization validation
                    logger.error(
                        f"System context file not found: {context_file}. "
                        "This should have been caught during initialization."
                    )
                    raise FileNotFoundError(
                        f"System context file missing: {context_file}"
                    ) from e
                except PermissionError as e:
                    logger.error(
                        f"Permission denied reading system context file: {context_file}"
                    )
                    raise PermissionError(
                        f"Cannot read system context file: {context_file}"
                    ) from e
                except UnicodeDecodeError as e:
                    logger.error(
                        f"Invalid UTF-8 encoding in system context file: {context_file}"
                    )
                    raise ValueError(
                        f"System context file has invalid encoding: {context_file}"
                    ) from e
                except OSError as e:
                    logger.error(
                        f"OS error reading system context file {context_file}: {e}"
                    )
                    raise OSError(
                        f"Failed to read system context file: {context_file}"
                    ) from e

            # Fill system context with variables if it's not empty
            if system_context:
                try:
                    final_system_context = system_context.format(
                        repo=repo,
                        issue_number=issue_number or "",
                        **kwargs,
                    )
                except (KeyError, ValueError) as e:
                    logger.warning(
                        f"Error formatting system context in workflow '{workflow_name}': {e}"
                    )
                    # Continue without formatted system context
                    final_system_context = None

        # Combine prompt with user_query if provided and not already in template
        # Only append user_query if the template doesn't use it
        if user_query and "{user_query}" not in workflow.prompt.template:
            final_prompt = f"{prompt}. {user_query}"
        else:
            final_prompt = prompt

        return (final_prompt, final_system_context)

    def list_workflows(self) -> dict[str, str]:
        """List all available workflows.

        Returns:
            Dict mapping workflow names to descriptions
        """
        return {
            name: workflow.description or "No description"
            for name, workflow in self.workflows.items()
        }

    def get_context_profile(self, workflow_name: str) -> dict[str, Any]:
        """Get the context profile for a workflow.

        Args:
            workflow_name: Name of the workflow.

        Returns:
            Dict with context profile settings (repomap_budget, personalized, etc.)
        """
        if workflow_name not in self.workflows:
            return {}

        profile = self.workflows[workflow_name].context.model_dump()
        return {k: v for k, v in profile.items()}

    def get_conversation_config(self, workflow_name: str) -> ConversationConfigModel:
        """Get the conversation persistence config for a workflow.

        Args:
            workflow_name: Name of the workflow.

        Returns:
            ConversationConfigModel instance (defaults if workflow not found).
        """
        if workflow_name not in self.workflows:
            return ConversationConfigModel()
        return self.workflows[workflow_name].conversation


@lru_cache(maxsize=None)
def get_workflow_engine(config_path: str | None = None) -> WorkflowEngine:
    """Get cached WorkflowEngine instance (singleton per config path).

    The engine is cached to avoid repeatedly parsing and validating workflows.yaml.
    Since workflow configuration is static during runtime, caching provides
    significant performance benefits with no downsides.

    Args:
        config_path: Path to workflows.yaml file (defaults to workflows.yaml in project root)

    Returns:
        Cached WorkflowEngine instance

    Note:
        Changes to workflows.yaml require a process restart to take effect.
    """
    return WorkflowEngine(config_path)
