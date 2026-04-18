"""Generic payload extraction registry for GitHub webhook events.

Maps event types to declarative field extraction rules so new GitHub events
can be supported by adding a registry entry instead of writing custom code.
"""

import logging
from typing import Any

from pydantic import BaseModel, Field

from shared.utils import resolve_path

logger = logging.getLogger(__name__)


class ExtractionRule(BaseModel):
    """A single field extraction rule mapping a semantic name to a payload path."""

    path: str
    required: bool = False
    default: Any = None


class EventExtractionConfig(BaseModel):
    """Extraction configuration for a specific event type."""

    issue_number: ExtractionRule | None = None
    ref: ExtractionRule | None = None
    user: ExtractionRule = ExtractionRule(path="sender.login")
    extra: dict[str, ExtractionRule] = Field(default_factory=dict)


class ExtractedFields(BaseModel):
    """Result of extracting fields from a webhook payload."""

    issue_number: int | None = None
    ref: str = "main"
    user: str = "unknown"
    extra: dict[str, Any] = Field(default_factory=dict)


# Lazy-loaded from the rules module to avoid circular imports at definition time.
_DEFAULT_RULES: dict[str, EventExtractionConfig] | None = None


def _load_default_rules() -> dict[str, EventExtractionConfig]:
    """Lazy-load extraction rules from the rules module."""
    global _DEFAULT_RULES
    if _DEFAULT_RULES is None:
        try:
            from extraction_rules import EXTRACTION_RULES
        except ImportError:
            from services.webhook.extraction_rules import EXTRACTION_RULES

        _DEFAULT_RULES = EXTRACTION_RULES
    return _DEFAULT_RULES


class PayloadExtractor:
    """Extracts standardized fields from GitHub webhook payloads.

    Uses a registry of EventExtractionConfig entries to resolve dot-paths
    into the common fields needed by the job pipeline (issue_number, ref,
    user, extra context). Supports action-qualified overrides: if a config
    exists for "workflow_job.completed" it takes priority over "workflow_job".
    """

    _REF_STRATEGIES: dict[str, str] = {
        # PR events → refs/pull/N/head
        "pull_request": "pr",
        "pull_request_review": "pr",
        "pull_request_review_comment": "pr",
        "pull_request_review_thread": "pr",
        # CI events → refs/heads/{ref}
        "workflow_job": "heads",
        "workflow_run": "heads",
        "check_run": "heads",
        "check_suite": "heads",
        "deployment": "heads",
        "deployment_status": "heads",
        "repository_dispatch": "heads",
        # Passthrough (already fully qualified)
        "push": "passthrough",
        "merge_group": "passthrough",
        "workflow_dispatch": "passthrough",
        # Special: needs data["ref_type"]
        "create": "create_delete",
        "delete": "create_delete",
        # Hardcoded
        "issue_comment": "main",
    }

    def __init__(self, rules: dict[str, EventExtractionConfig] | None = None):
        self._rules = rules

    @property
    def rules(self) -> dict[str, EventExtractionConfig]:
        """Lazily resolve rules (allows override for testing)."""
        if self._rules is not None:
            return self._rules
        return _load_default_rules()

    def _find_config(
        self, event_type: str, action: str | None
    ) -> EventExtractionConfig | None:
        """Look up extraction config with action-qualified fallback."""
        if action:
            key = f"{event_type}.{action}"
            if key in self.rules:
                return self.rules[key]
        return self.rules.get(event_type)

    def extract(
        self, event_type: str, action: str | None, data: dict
    ) -> ExtractedFields:
        """Extract standardized fields from a webhook payload.

        Args:
            event_type: GitHub event type (e.g. "pull_request").
            action: Event action (e.g. "opened").
            data: The parsed webhook payload dict.

        Returns:
            ExtractedFields with issue_number, ref, user, and extra data.

        Raises:
            ValueError: If a required field is missing from the payload.
        """
        config = self._find_config(event_type, action)

        if config is None:
            logger.debug(
                "No extraction rules for event type '%s', using defaults",
                event_type,
            )
            user = resolve_path(data, "sender.login") or "unknown"
            return ExtractedFields(user=user)

        issue_number = self._extract_field(config.issue_number, data)
        ref = self._resolve_ref(event_type, config.ref, data, issue_number)
        user = self._extract_field(config.user, data) or "unknown"

        extra: dict[str, Any] = {}
        for name, rule in config.extra.items():
            extra[name] = self._extract_field(rule, data)

        return ExtractedFields(
            issue_number=issue_number,
            ref=ref,
            user=user,
            extra=extra,
        )

    def _extract_field(self, rule: ExtractionRule | None, data: dict) -> Any:
        """Resolve a single extraction rule against a payload."""
        if rule is None:
            return None
        value = resolve_path(data, rule.path)
        if value is None:
            if rule.required:
                raise ValueError(
                    f"Required field at path '{rule.path}' is missing " f"from payload"
                )
            return rule.default
        return value

    def _resolve_ref(
        self,
        event_type: str,
        ref_rule: ExtractionRule | None,
        data: dict,
        issue_number: int | None,
    ) -> str:
        """Compute the git ref, applying smart prefix logic.

        - PR-related events get refs/pull/N/head
        - workflow_job / check events get refs/heads/branch
        - release events keep the tag name as-is
        - push events keep the raw ref; create / delete events resolve to
          fully-qualified refs/heads/ or refs/tags/
        - Everything else defaults to "main"
        """
        if ref_rule is None:
            return "main"

        raw_ref: Any = self._extract_field(ref_rule, data)
        if raw_ref is None:
            logger.debug(
                "Ref rule path '%s' resolved to None for event '%s', "
                "falling back to 'main'",
                ref_rule.path,
                event_type,
            )
            return "main"

        strategy = self._REF_STRATEGIES.get(event_type, "passthrough")

        if strategy == "pr":
            return f"refs/pull/{raw_ref}/head"
        if strategy == "heads":
            return f"refs/heads/{raw_ref}"
        if strategy == "main":
            return "main"
        if strategy == "create_delete":
            ref_type = data.get("ref_type", "branch")
            if ref_type == "tag":
                return f"refs/tags/{raw_ref}"
            return f"refs/heads/{raw_ref}"
        # "passthrough" and default
        return str(raw_ref)
