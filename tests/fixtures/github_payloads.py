"""GitHub webhook payload fixtures for testing."""


def get_pr_opened_payload(
    pr_number: int = 123,
    repo: str = "owner/repo",
    branch: str = "feature-branch",
    title: str = "Test PR",
) -> dict:
    """Generate a PR opened webhook payload."""
    owner, repo_name = repo.split("/")
    return {
        "action": "opened",
        "number": pr_number,
        "pull_request": {
            "number": pr_number,
            "title": title,
            "body": "Test PR description",
            "state": "open",
            "user": {"login": "testuser", "id": 12345},
            "head": {
                "ref": branch,
                "sha": "abc123def456",
                "repo": {"full_name": repo},
            },
            "base": {
                "ref": "main",
                "sha": "def456abc123",
                "repo": {"full_name": repo},
            },
            "created_at": "2024-01-01T00:00:00Z",
            "updated_at": "2024-01-01T00:00:00Z",
        },
        "repository": {
            "full_name": repo,
            "name": repo_name,
            "owner": {"login": owner},
            "private": False,
        },
        "installation": {"id": 12345},
        "sender": {"login": "testuser"},
    }


def get_issue_comment_payload(
    issue_number: int = 456,
    repo: str = "owner/repo",
    comment_body: str = "/agent review",
    is_pr: bool = True,
) -> dict:
    """Generate an issue comment webhook payload."""
    owner, repo_name = repo.split("/")
    payload = {
        "action": "created",
        "issue": {
            "number": issue_number,
            "title": "Test Issue",
            "body": "Issue description",
            "state": "open",
            "user": {"login": "testuser"},
        },
        "comment": {
            "id": 789,
            "body": comment_body,
            "user": {"login": "commenter"},
            "created_at": "2024-01-01T00:00:00Z",
        },
        "repository": {
            "full_name": repo,
            "name": repo_name,
            "owner": {"login": owner},
        },
        "installation": {"id": 12345},
        "sender": {"login": "commenter"},
    }

    if is_pr:
        payload["issue"]["pull_request"] = {
            "url": f"https://api.github.com/repos/{repo}/pulls/{issue_number}"
        }

    return payload


def get_pr_review_comment_payload(
    pr_number: int = 123, repo: str = "owner/repo", comment_body: str = "LGTM"
) -> dict:
    """Generate a PR review comment webhook payload."""
    owner, repo_name = repo.split("/")
    return {
        "action": "created",
        "pull_request": {
            "number": pr_number,
            "title": "Test PR",
        },
        "comment": {
            "id": 999,
            "body": comment_body,
            "user": {"login": "reviewer"},
            "path": "src/main.py",
            "line": 42,
        },
        "repository": {
            "full_name": repo,
            "name": repo_name,
            "owner": {"login": owner},
        },
        "installation": {"id": 12345},
    }
