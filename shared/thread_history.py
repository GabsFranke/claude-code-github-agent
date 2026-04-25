"""Fetch and format issue/PR/discussion comment history for agent context injection.

Provides async functions to retrieve the full thread history (body plus all
comments) from the GitHub REST and GraphQL APIs and format it for injection
into the agent's system prompt. This gives each agent invocation the same
context a human would see when opening the thread.

Supported thread types:
  - issue: REST API (issue body + comments)
  - pr: REST API (PR body + issue comments + review comments)
  - discussion: GraphQL API (discussion body + comments + replies)
"""

import logging
from typing import Any

import httpx
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

MAX_COMMENT_PAGES = 5
MAX_TOTAL_COMMENTS = 500

GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"

_DISCUSSION_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $first: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    discussion(number: $number) {
      title
      body
      author { login }
      createdAt
      category { name }
      labels(first: 20) { nodes { name } }
      comments(first: $first, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          author { login }
          createdAt
          body
          replies(first: 10) {
            nodes {
              author { login }
              createdAt
              body
            }
          }
        }
      }
    }
  }
}
"""


class ThreadHistoryConfig(BaseModel):
    """Configuration for thread history injection."""

    enabled: bool = Field(
        default=True,
        description="Whether to inject thread history into the agent context",
    )
    max_comments: int = Field(
        default=100,
        description="Maximum number of comments to fetch from GitHub API",
    )
    include_pr_reviews: bool = Field(
        default=True,
        description="Whether to include PR review comments (inline code comments)",
    )
    include_issue_body: bool = Field(
        default=True,
        description="Whether to include the issue/PR body as the root of the history",
    )


async def _fetch_issue_body(
    repo: str,
    issue_number: int,
    token: str,
    client: httpx.AsyncClient,
    is_pr: bool = False,
) -> dict[str, Any] | None:
    """Fetch the issue or PR title and body."""
    endpoint = "pulls" if is_pr else "issues"
    url = f"https://api.github.com/repos/{repo}/{endpoint}/{issue_number}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
    }
    try:
        response = await client.get(url, headers=headers, timeout=15.0)
        if response.status_code == 200:
            data = response.json()
            return {
                "title": data.get("title", ""),
                "body": data.get("body", "") or "",
                "author": data.get("user", {}).get("login", "unknown"),
                "created_at": data.get("created_at", ""),
                "state": data.get("state", ""),
                "labels": [lbl.get("name", "") for lbl in data.get("labels", [])],
            }
        logger.debug(f"Failed to fetch issue body: HTTP {response.status_code}")
        return None
    except Exception as e:
        logger.warning(f"Error fetching issue body for {repo}#{issue_number}: {e}")
        return None


async def _fetch_comments(
    repo: str,
    issue_number: int,
    token: str,
    client: httpx.AsyncClient,
    max_comments: int = 100,
) -> list[dict[str, Any]]:
    """Fetch issue comments (applies to both issues and PRs).

    Returns comments sorted oldest-first.
    """
    comments: list[dict[str, Any]] = []
    url = f"https://api.github.com/repos/{repo}/issues/{issue_number}/comments"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
    }

    for page in range(MAX_COMMENT_PAGES):
        params = {"per_page": 100, "page": page + 1}
        try:
            response = await client.get(
                url, headers=headers, params=params, timeout=15.0
            )
            if response.status_code != 200:
                logger.debug(f"Comments API returned {response.status_code}")
                break
            page_comments = response.json()
            if not page_comments:
                break
            for c in page_comments:
                comments.append(
                    {
                        "author": c.get("user", {}).get("login", "unknown"),
                        "created_at": c.get("created_at", ""),
                        "body": c.get("body", "") or "",
                    }
                )
            if len(comments) >= max_comments:
                comments = comments[:max_comments]
                break
            if len(comments) >= MAX_TOTAL_COMMENTS:
                break
        except Exception as e:
            logger.warning(f"Error fetching comments page {page + 1}: {e}")
            break

    return comments


async def _fetch_pr_review_comments(
    repo: str,
    pr_number: int,
    token: str,
    client: httpx.AsyncClient,
    max_comments: int = 100,
) -> list[dict[str, Any]]:
    """Fetch PR review comments (inline code review comments).

    Returns comments sorted oldest-first.
    """
    comments: list[dict[str, Any]] = []
    url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}/comments"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
    }

    for page in range(MAX_COMMENT_PAGES):
        params = {"per_page": 100, "page": page + 1}
        try:
            response = await client.get(
                url, headers=headers, params=params, timeout=15.0
            )
            if response.status_code != 200:
                logger.debug(f"Review comments API returned {response.status_code}")
                break
            page_comments = response.json()
            if not page_comments:
                break
            for c in page_comments:
                comment: dict[str, Any] = {
                    "author": c.get("user", {}).get("login", "unknown"),
                    "created_at": c.get("created_at", ""),
                    "body": c.get("body", "") or "",
                }
                if c.get("path"):
                    comment["context"] = f"{c['path']}:{c.get('original_line', '?')}"
                comments.append(comment)
            if len(comments) >= max_comments:
                comments = comments[:max_comments]
                break
            if len(comments) >= MAX_TOTAL_COMMENTS:
                break
        except Exception as e:
            logger.warning(f"Error fetching review comments page {page + 1}: {e}")
            break

    return comments


async def _fetch_discussion(
    repo: str,
    discussion_number: int,
    token: str,
    client: httpx.AsyncClient,
    max_comments: int = 100,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Fetch a GitHub Discussion and its comments via the GraphQL API.

    Returns (discussion_body, comments) where discussion_body has
    title/body/author/created_at/labels and comments include nested replies.
    """
    owner, _, name = repo.partition("/")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    discussion_body: dict[str, Any] | None = None
    all_comments: list[dict[str, Any]] = []
    cursor: str | None = None

    for _ in range(MAX_COMMENT_PAGES):
        variables: dict[str, Any] = {
            "owner": owner,
            "name": name,
            "number": discussion_number,
            "first": min(max_comments, 100),
            "after": cursor,
        }
        try:
            response = await client.post(
                GITHUB_GRAPHQL_URL,
                headers=headers,
                json={"query": _DISCUSSION_QUERY, "variables": variables},
                timeout=15.0,
            )
            if response.status_code != 200:
                logger.debug(f"Discussion GraphQL API returned {response.status_code}")
                break

            data = response.json()
            if data.get("errors"):
                logger.debug(
                    f"Discussion GraphQL errors: {data['errors'][0].get('message', '')}"
                )
                break

            disc = data.get("data", {}).get("repository", {}).get("discussion")
            if not disc:
                logger.debug("Discussion not found in GraphQL response")
                break

            if discussion_body is None:
                labels = [
                    lbl["name"]
                    for lbl in disc.get("labels", {}).get("nodes", [])
                    if lbl.get("name")
                ]
                discussion_body = {
                    "title": disc.get("title", ""),
                    "body": disc.get("body", "") or "",
                    "author": (disc.get("author") or {}).get("login", "unknown"),
                    "created_at": disc.get("createdAt", ""),
                    "state": "open",
                    "labels": labels,
                    "category": (disc.get("category") or {}).get("name", ""),
                }

            comment_page = disc.get("comments", {})
            for node in comment_page.get("nodes", []):
                comment: dict[str, Any] = {
                    "author": (node.get("author") or {}).get("login", "unknown"),
                    "created_at": node.get("createdAt", ""),
                    "body": node.get("body", "") or "",
                }
                replies = node.get("replies", {}).get("nodes", [])
                if replies:
                    comment["replies"] = [
                        {
                            "author": (r.get("author") or {}).get("login", "unknown"),
                            "created_at": r.get("createdAt", ""),
                            "body": r.get("body", "") or "",
                        }
                        for r in replies
                    ]
                all_comments.append(comment)

            page_info = comment_page.get("pageInfo", {})
            if not page_info.get("hasNextPage"):
                break
            cursor = page_info.get("endCursor")

            if len(all_comments) >= max_comments:
                all_comments = all_comments[:max_comments]
                break
        except Exception as e:
            logger.warning(
                f"Error fetching discussion page for {repo}#{discussion_number}: {e}"
            )
            break

    return discussion_body, all_comments


def _format_thread_history(
    issue_body: dict[str, Any] | None,
    comments: list[dict[str, Any]],
    review_comments: list[dict[str, Any]] | None = None,
    is_pr: bool = False,
    is_discussion: bool = False,
    truncated: bool = False,
) -> str:
    """Format thread history into a structured text block for system prompt injection."""
    parts: list[str] = []

    if is_discussion:
        thread_label = "Discussion"
    elif is_pr:
        thread_label = "Pull Request"
    else:
        thread_label = "Issue"

    if issue_body:
        labels_str = ""
        if issue_body.get("labels"):
            labels_str = f" [Labels: {', '.join(issue_body['labels'])}]"
        category_str = ""
        if issue_body.get("category"):
            category_str = f" [Category: {issue_body['category']}]"
        state_str = (
            f" ({issue_body.get('state', 'open')})" if issue_body.get("state") else ""
        )
        header = f"## Original {thread_label}: {issue_body.get('title', 'Untitled')}{state_str}{labels_str}{category_str}"
        parts.append(header)

        author_line = f"**Author:** {issue_body.get('author', 'unknown')}"
        if issue_body.get("created_at"):
            author_line += f" | **Created:** {issue_body['created_at']}"
        parts.append(author_line)

        body_text = issue_body.get("body", "").strip()
        parts.append(body_text if body_text else "(No description provided)")

    if truncated:
        parts.append(
            "\n---\n*Older comments truncated — only recent history shown.*\n---"
        )

    if comments:
        parts.append(f"\n## Comments ({len(comments)})")
        for i, comment in enumerate(comments, 1):
            entry = f"### Comment {i}"
            entry += f"\n**{comment.get('author', 'unknown')}**"
            if comment.get("created_at"):
                entry += f" | {comment['created_at']}"
            entry += f"\n{comment.get('body', '').strip() or '(no content)'}"
            parts.append(entry)

            # Discussion replies (nested under comment)
            for j, reply in enumerate(comment.get("replies", []), 1):
                reply_entry = f"#### Reply {j}"
                reply_entry += f"\n**{reply.get('author', 'unknown')}**"
                if reply.get("created_at"):
                    reply_entry += f" | {reply['created_at']}"
                reply_entry += f"\n{reply.get('body', '').strip() or '(no content)'}"
                parts.append(reply_entry)

    if review_comments:
        parts.append(f"\n## Review Comments ({len(review_comments)})")
        for i, comment in enumerate(review_comments, 1):
            entry = f"### Review Comment {i}"
            entry += f"\n**{comment.get('author', 'unknown')}**"
            if comment.get("created_at"):
                entry += f" | {comment['created_at']}"
            if comment.get("context"):
                entry += f" | `{comment['context']}`"
            entry += f"\n{comment.get('body', '').strip() or '(no content)'}"
            parts.append(entry)

    if not parts:
        return ""

    return "<thread_history>\n" + "\n\n".join(parts) + "\n</thread_history>"


async def fetch_and_format_thread_history(
    repo: str,
    issue_number: int,
    token: str,
    thread_type: str = "issue",
    config: ThreadHistoryConfig | None = None,
) -> str:
    """Fetch and format the complete thread history for agent context injection.

    Main entry point called from sandbox_worker.py. Supports issues, PRs,
    and discussions. For issues/PRs uses the REST API; for discussions uses
    the GraphQL API.

    Returns empty string if disabled or on any failure (graceful degradation).
    """
    if config is None:
        config = ThreadHistoryConfig()

    if not config.enabled:
        return ""

    is_pr = thread_type == "pr"
    is_discussion = thread_type == "discussion"

    try:
        async with httpx.AsyncClient() as client:
            if is_discussion:
                # Discussions require GraphQL API
                issue_body, comments = await _fetch_discussion(
                    repo, issue_number, token, client, max_comments=config.max_comments
                )
                review_comments = None
            else:
                # Issues and PRs use REST API
                issue_body = None
                if config.include_issue_body:
                    issue_body = await _fetch_issue_body(
                        repo, issue_number, token, client, is_pr=is_pr
                    )

                comments = await _fetch_comments(
                    repo, issue_number, token, client, max_comments=config.max_comments
                )

                review_comments = None
                if is_pr and config.include_pr_reviews:
                    review_comments = await _fetch_pr_review_comments(
                        repo,
                        issue_number,
                        token,
                        client,
                        max_comments=config.max_comments,
                    )
    except Exception as e:
        logger.warning(f"Thread history fetch failed for {repo}#{issue_number}: {e}")
        return ""

    truncated = len(comments) >= config.max_comments

    return _format_thread_history(
        issue_body=issue_body,
        comments=comments,
        review_comments=review_comments,
        is_pr=is_pr,
        is_discussion=is_discussion,
        truncated=truncated,
    )
