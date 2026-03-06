"""YAML-driven workflow engine."""

import logging
import string
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger(__name__)


class PromptConfig(BaseModel):
    """Prompt configuration for a workflow."""

    template: str = Field(..., description="Prompt template with placeholders")
    system_context: str | None = Field(
        None, description="System context (inline or .md file reference)"
    )


class TriggersConfig(BaseModel):
    """Trigger configuration for a workflow."""

    events: list[str] = Field(default_factory=list, description="GitHub event triggers")
    commands: list[str] = Field(default_factory=list, description="Command triggers")


class WorkflowConfig(BaseModel):
    """Configuration for a single workflow."""

    triggers: TriggersConfig = Field(..., description="Event and command triggers")
    prompt: PromptConfig = Field(..., description="Prompt configuration")
    description: str = Field(default="", description="Workflow description")


class WorkflowsConfig(BaseModel):
    """Root configuration containing all workflows."""

    workflows: dict[str, WorkflowConfig] = Field(
        ..., description="Map of workflow names to configurations"
    )


class WorkflowEngine:
    """Loads workflows from YAML and routes events/commands to prompts."""

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

        self.workflows = {
            name: workflow.model_dump()
            for name, workflow in validated_config.workflows.items()
        }

        # Build lookup tables for fast routing
        self._event_map: dict[str, str] = {}
        self._command_map: dict[str, str] = {}

        for workflow_name, workflow in self.workflows.items():
            triggers = workflow.get("triggers", {})

            # Map events to workflows
            for event in triggers.get("events", []):
                self._event_map[event] = workflow_name
                logger.debug(f"Mapped event '{event}' -> workflow '{workflow_name}'")

            # Map commands to workflows
            for command in triggers.get("commands", []):
                self._command_map[command] = workflow_name
                logger.debug(
                    f"Mapped command '{command}' -> workflow '{workflow_name}'"
                )

        logger.info(
            f"Loaded {len(self.workflows)} workflows: {list(self.workflows.keys())}"
        )

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

    def get_workflow_for_command(self, command: str) -> str | None:
        """Get workflow name for a user command.

        Args:
            command: Command string (e.g., "/review")

        Returns:
            Workflow name or None if command not recognized
        """
        return self._command_map.get(command)

    def build_prompt(
        self,
        workflow_name: str,
        repo: str,
        issue_number: int | None = None,
        user_query: str = "",
        **kwargs: Any,
    ) -> str:
        """Build the final prompt for Claude Agent SDK.

        Args:
            workflow_name: Name of workflow to execute
            repo: Repository full name (owner/repo)
            issue_number: Issue or PR number
            user_query: User-provided query/context
            **kwargs: Additional template variables

        Returns:
            Complete prompt string for client.query()
        """
        if workflow_name not in self.workflows:
            raise ValueError(f"Unknown workflow: {workflow_name}")

        workflow = self.workflows[workflow_name]
        prompt_config = workflow["prompt"]

        # Validate template placeholders to prevent injection
        template = prompt_config["template"]
        try:
            # Get all field names from template
            field_names = [
                field_name
                for _, field_name, _, _ in string.Formatter().parse(template)
                if field_name is not None
            ]

            # Build safe variables dict
            safe_vars = {
                "repo": repo,
                "issue_number": issue_number or "",
                "user_query": user_query,
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

        # Add system context if defined
        system_context = prompt_config.get("system_context")
        if system_context:
            # Check if it's a file reference (ends with .md)
            if system_context.endswith(".md"):
                context_file = Path(__file__).parent.parent / "prompts" / system_context
                try:
                    system_context = context_file.read_text(encoding="utf-8").strip()
                    logger.debug(f"Loaded system context from {context_file}")
                except FileNotFoundError:
                    logger.warning(f"System context file not found: {context_file}")
                    system_context = ""
                except Exception as e:
                    logger.error(
                        f"Error reading system context file {context_file}: {e}"
                    )
                    system_context = ""

            # Fill system context with variables if it's not empty
            if system_context:
                try:
                    system_context = system_context.format(
                        repo=repo,
                        issue_number=issue_number or "",
                        **kwargs,
                    )
                except (KeyError, ValueError) as e:
                    logger.warning(
                        f"Error formatting system context in workflow '{workflow_name}': {e}"
                    )
                    # Continue without formatted system context
                    pass

                # Combine: prompt + system_context + user_query
                if user_query:
                    return f"{prompt} {system_context}. {user_query}"
                return f"{prompt} {system_context}"

        return str(prompt)

    def list_workflows(self) -> dict[str, str]:
        """List all available workflows.

        Returns:
            Dict mapping workflow names to descriptions
        """
        return {
            name: workflow.get("description", "No description")
            for name, workflow in self.workflows.items()
        }
