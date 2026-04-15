"""Declarative extraction rules for GitHub webhook event payloads.

Maps event types to field extraction rules so new GitHub events can be
supported by adding a registry entry instead of writing custom code.

Action-qualified keys (e.g. "workflow_job.completed") take priority over
bare event type keys (e.g. "workflow_job") during lookup.

To add support for a new GitHub event type, add an EventExtractionConfig
entry here -- no code changes needed elsewhere.
"""

try:
    from payload_extractor import EventExtractionConfig, ExtractionRule
except ImportError:
    from services.webhook.payload_extractor import EventExtractionConfig, ExtractionRule

EXTRACTION_RULES: dict[str, EventExtractionConfig] = {
    # --- Pull request events ---
    "pull_request": EventExtractionConfig(
        issue_number=ExtractionRule(path="pull_request.number", required=True),
        ref=ExtractionRule(path="pull_request.number"),
        user=ExtractionRule(path="pull_request.user.login"),
    ),
    "pull_request_review": EventExtractionConfig(
        issue_number=ExtractionRule(path="pull_request.number", required=True),
        ref=ExtractionRule(path="pull_request.number"),
        user=ExtractionRule(path="review.user.login"),
    ),
    "pull_request_review_comment": EventExtractionConfig(
        issue_number=ExtractionRule(path="pull_request.number", required=True),
        ref=ExtractionRule(path="pull_request.number"),
        user=ExtractionRule(path="comment.user.login"),
    ),
    "pull_request_review_thread": EventExtractionConfig(
        issue_number=ExtractionRule(path="pull_request.number", required=True),
        ref=ExtractionRule(path="pull_request.number"),
        user=ExtractionRule(path="sender.login"),
    ),
    # --- Issue events ---
    "issues": EventExtractionConfig(
        issue_number=ExtractionRule(path="issue.number", required=True),
        user=ExtractionRule(path="issue.user.login"),
    ),
    "issue_comment": EventExtractionConfig(
        issue_number=ExtractionRule(path="issue.number", required=True),
        user=ExtractionRule(path="comment.user.login"),
    ),
    "sub_issues": EventExtractionConfig(
        issue_number=ExtractionRule(path="sub_issue.number"),
        user=ExtractionRule(path="sender.login"),
        extra={
            "parent_issue_number": ExtractionRule(path="parent_issue.number"),
        },
    ),
    # --- Discussion events ---
    "discussion": EventExtractionConfig(
        issue_number=ExtractionRule(path="discussion.number"),
        user=ExtractionRule(path="discussion.user.login"),
    ),
    "discussion_comment": EventExtractionConfig(
        issue_number=ExtractionRule(path="discussion.number"),
        user=ExtractionRule(path="comment.user.login"),
    ),
    # --- CI/CD events ---
    "workflow_job": EventExtractionConfig(
        issue_number=ExtractionRule(path="workflow_job.run_id"),
        ref=ExtractionRule(path="workflow_job.head_branch"),
        user=ExtractionRule(path="sender.login"),
        extra={
            "run_id": ExtractionRule(path="workflow_job.run_id"),
            "workflow_name_gh": ExtractionRule(path="workflow_job.workflow_name"),
            "job_name": ExtractionRule(path="workflow_job.name"),
            "conclusion": ExtractionRule(path="workflow_job.conclusion"),
            "head_branch": ExtractionRule(path="workflow_job.head_branch"),
        },
    ),
    "workflow_run": EventExtractionConfig(
        issue_number=ExtractionRule(path="workflow_run.id"),
        ref=ExtractionRule(path="workflow_run.head_branch"),
        user=ExtractionRule(path="sender.login"),
        extra={
            "run_id": ExtractionRule(path="workflow_run.id"),
            "workflow_name_gh": ExtractionRule(path="workflow_run.name"),
            "conclusion": ExtractionRule(path="workflow_run.conclusion"),
            "head_branch": ExtractionRule(path="workflow_run.head_branch"),
        },
    ),
    "workflow_dispatch": EventExtractionConfig(
        ref=ExtractionRule(path="ref"),
        user=ExtractionRule(path="sender.login"),
        extra={
            "workflow": ExtractionRule(path="workflow"),
            "inputs": ExtractionRule(path="inputs"),
        },
    ),
    "check_run": EventExtractionConfig(
        issue_number=ExtractionRule(path="check_run.check_suite.id"),
        ref=ExtractionRule(path="check_run.check_suite.head_branch"),
        user=ExtractionRule(path="sender.login"),
        extra={
            "run_id": ExtractionRule(path="check_run.id"),
            "conclusion": ExtractionRule(path="check_run.conclusion"),
            "head_branch": ExtractionRule(path="check_run.check_suite.head_branch"),
        },
    ),
    "check_suite": EventExtractionConfig(
        ref=ExtractionRule(path="check_suite.head_branch"),
        user=ExtractionRule(path="sender.login"),
        extra={
            "head_branch": ExtractionRule(path="check_suite.head_branch"),
            "conclusion": ExtractionRule(path="check_suite.conclusion"),
        },
    ),
    "status": EventExtractionConfig(
        user=ExtractionRule(path="sender.login"),
        extra={
            "state": ExtractionRule(path="state"),
            "sha": ExtractionRule(path="sha"),
            "context": ExtractionRule(path="context"),
            "description": ExtractionRule(path="description"),
        },
    ),
    # --- Repository events ---
    "release": EventExtractionConfig(
        ref=ExtractionRule(path="release.tag_name"),
        user=ExtractionRule(path="sender.login"),
        extra={
            "tag_name": ExtractionRule(path="release.tag_name"),
            "release_name": ExtractionRule(path="release.name"),
            "release_body": ExtractionRule(path="release.body"),
        },
    ),
    "push": EventExtractionConfig(
        ref=ExtractionRule(path="ref"),
        user=ExtractionRule(path="sender.login"),
    ),
    "create": EventExtractionConfig(
        ref=ExtractionRule(path="ref"),
        user=ExtractionRule(path="sender.login"),
        extra={
            "ref_type": ExtractionRule(path="ref_type"),
        },
    ),
    "delete": EventExtractionConfig(
        ref=ExtractionRule(path="ref"),
        user=ExtractionRule(path="sender.login"),
        extra={
            "ref_type": ExtractionRule(path="ref_type"),
        },
    ),
    "commit_comment": EventExtractionConfig(
        user=ExtractionRule(path="comment.user.login"),
        extra={
            "commit_id": ExtractionRule(path="comment.commit_id"),
        },
    ),
    "repository_dispatch": EventExtractionConfig(
        ref=ExtractionRule(path="branch"),
        user=ExtractionRule(path="sender.login"),
        extra={
            "client_payload": ExtractionRule(path="client_payload"),
        },
    ),
    "deployment": EventExtractionConfig(
        ref=ExtractionRule(path="deployment.ref"),
        user=ExtractionRule(path="sender.login"),
        extra={
            "environment": ExtractionRule(path="deployment.environment"),
        },
    ),
    "deployment_status": EventExtractionConfig(
        ref=ExtractionRule(path="deployment.ref"),
        user=ExtractionRule(path="sender.login"),
        extra={
            "environment": ExtractionRule(path="deployment.environment"),
            "state": ExtractionRule(path="deployment_status.state"),
        },
    ),
    # --- Repo management events ---
    "label": EventExtractionConfig(
        user=ExtractionRule(path="sender.login"),
        extra={
            "label_name": ExtractionRule(path="label.name"),
            "label_color": ExtractionRule(path="label.color"),
        },
    ),
    "milestone": EventExtractionConfig(
        user=ExtractionRule(path="sender.login"),
        extra={
            "milestone_title": ExtractionRule(path="milestone.title"),
        },
    ),
    "star": EventExtractionConfig(
        user=ExtractionRule(path="sender.login"),
    ),
    "watch": EventExtractionConfig(
        user=ExtractionRule(path="sender.login"),
    ),
    "fork": EventExtractionConfig(
        user=ExtractionRule(path="sender.login"),
        extra={
            "fork_full_name": ExtractionRule(path="forkee.full_name"),
        },
    ),
    "gollum": EventExtractionConfig(
        user=ExtractionRule(path="sender.login"),
        extra={
            "pages": ExtractionRule(path="pages"),
        },
    ),
    "repository": EventExtractionConfig(
        user=ExtractionRule(path="sender.login"),
    ),
    "branch_protection_configuration": EventExtractionConfig(
        user=ExtractionRule(path="sender.login"),
    ),
    "branch_protection_rule": EventExtractionConfig(
        user=ExtractionRule(path="sender.login"),
        extra={
            "rule_name": ExtractionRule(path="rule.name"),
        },
    ),
    "member": EventExtractionConfig(
        user=ExtractionRule(path="sender.login"),
        extra={
            "member_login": ExtractionRule(path="member.login"),
        },
    ),
    "team": EventExtractionConfig(
        user=ExtractionRule(path="sender.login"),
    ),
    "organization": EventExtractionConfig(
        user=ExtractionRule(path="sender.login"),
    ),
    "installation": EventExtractionConfig(
        user=ExtractionRule(path="sender.login"),
    ),
    "installation_repositories": EventExtractionConfig(
        user=ExtractionRule(path="sender.login"),
    ),
    "ping": EventExtractionConfig(
        user=ExtractionRule(path="sender.login"),
    ),
    "package": EventExtractionConfig(
        user=ExtractionRule(path="sender.login"),
    ),
    "merge_group": EventExtractionConfig(
        ref=ExtractionRule(path="merge_group.head_ref"),
        user=ExtractionRule(path="sender.login"),
        extra={
            "head_sha": ExtractionRule(path="merge_group.head_sha"),
            "base_ref": ExtractionRule(path="merge_group.base_ref"),
        },
    ),
    # --- Security events ---
    "secret_scanning_alert": EventExtractionConfig(
        user=ExtractionRule(path="sender.login"),
        extra={
            "alert_number": ExtractionRule(path="alert.number"),
        },
    ),
    "dependabot_alert": EventExtractionConfig(
        user=ExtractionRule(path="sender.login"),
        extra={
            "alert_number": ExtractionRule(path="alert.number"),
        },
    ),
    "code_scanning_alert": EventExtractionConfig(
        user=ExtractionRule(path="sender.login"),
        extra={
            "alert_number": ExtractionRule(path="alert.number"),
        },
    ),
}
