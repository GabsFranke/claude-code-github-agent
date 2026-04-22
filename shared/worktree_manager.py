"""Deterministic worktree management for persistent conversations.

Worktrees are keyed by repo + thread type + thread ID + workflow so that
the same conversation always lands in the same path.  This lets the SDK
find its session files on disk and resume conversations seamlessly.
"""

import logging
import shutil
from pathlib import Path
from typing import Any

from shared import execute_git_command

logger = logging.getLogger(__name__)

WORKTREE_BASE = Path.home() / ".claude" / "worktrees"


def get_worktree_path(
    repo: str, thread_type: str, thread_id: str, workflow: str
) -> Path:
    """Return the deterministic worktree path for a conversation.

    Path structure::

        /home/bot/.claude/worktrees/{owner--repo}/{thread_type}-{thread_id}/{workflow}/

    Args:
        repo: Repository full name (``owner/repo``).
        thread_type: One of ``pr``, ``issue``, ``discussion``.
        thread_id: Issue/PR/discussion number.
        workflow: Workflow name (e.g. ``review-pr``, ``generic``).

    Returns:
        Absolute :class:`~pathlib.Path` for the worktree.
    """
    safe_repo = repo.replace("/", "--")
    return WORKTREE_BASE / safe_repo / f"{thread_type}-{thread_id}" / workflow


async def reuse_or_create_worktree(
    bare_repo: str,
    ref: str,
    worktree_path: Path,
    session_mode: str,
) -> None:
    """Ensure a worktree exists and is up-to-date.

    * If the worktree exists and we are resuming, fetch and checkout.
    * Otherwise create a fresh worktree at the deterministic path.

    Args:
        bare_repo: Path to the bare repository (``.git`` dir).
        ref: Git ref to check out.
        worktree_path: Target directory for the worktree.
        session_mode: ``resume``, ``fork``, ``continue``, or ``new``.
    """
    if worktree_path.exists() and session_mode in ("resume", "continue"):
        logger.info(f"Reusing existing worktree at {worktree_path}")
        await _fetch_and_checkout(worktree_path, ref)
        return

    # Remove stale worktree if it exists (from a previous failed run)
    if worktree_path.exists():
        logger.warning(f"Removing stale worktree at {worktree_path}")
        shutil.rmtree(worktree_path, ignore_errors=True)
        # Also clean up from bare repo tracking
        try:
            await execute_git_command(
                [
                    "git",
                    f"--git-dir={bare_repo}",
                    "worktree",
                    "remove",
                    "--force",
                    str(worktree_path),
                ]
            )
        except Exception as e:
            logger.debug(f"Worktree remove failed (may not exist): {e}")

    # Prune any stale worktree registrations (directory missing but registered)
    try:
        await execute_git_command(
            ["git", f"--git-dir={bare_repo}", "worktree", "prune"]
        )
    except Exception as e:
        logger.debug(f"Worktree prune failed (may be clean): {e}")

    # Ensure parent directory exists
    worktree_path.parent.mkdir(parents=True, exist_ok=True)

    # Resolve ref for worktree creation
    bare_ref = _resolve_ref(ref)

    wt_cmd = [
        "git",
        f"--git-dir={bare_repo}",
        "worktree",
        "add",
        "--detach",
        str(worktree_path),
        bare_ref,
    ]
    code, _out, err = await execute_git_command(wt_cmd)

    if code != 0:
        # Try detecting default branch as fallback
        logger.warning(
            f"Worktree ref {bare_ref} failed: {err}. Trying default branch..."
        )
        default_branch = await _detect_default_branch(bare_repo)
        wt_cmd_fb = [
            "git",
            f"--git-dir={bare_repo}",
            "worktree",
            "add",
            "--detach",
            str(worktree_path),
            default_branch,
        ]
        code, _out, err = await execute_git_command(wt_cmd_fb)
        if code != 0:
            raise RuntimeError(f"Failed to create worktree at {worktree_path}: {err}")

    logger.info(f"Created worktree at {worktree_path} for ref {ref}")


async def _fetch_and_checkout(worktree_path: Path, ref: str) -> None:
    """Fetch latest changes and check out the requested ref."""
    # Fetch all remotes
    await execute_git_command(["git", "-C", str(worktree_path), "fetch", "origin"])

    # For existing worktrees, try checking out the fetched HEAD
    code, _, err = await execute_git_command(
        ["git", "-C", str(worktree_path), "checkout", "FETCH_HEAD"]
    )
    if code != 0:
        logger.warning(f"Checkout of FETCH_HEAD failed: {err}, trying merge")
        await execute_git_command(
            ["git", "-C", str(worktree_path), "merge", "FETCH_HEAD"]
        )


def _resolve_ref(ref: str) -> str:
    """Convert a ref to the format expected by the bare repo worktree add."""
    if ref.startswith("refs/pull/") or ref.startswith("refs/tags/"):
        return ref
    base = (
        ref.replace("refs/heads/", "")
        if ref.startswith("refs/heads/")
        else ref.replace("refs/", "")
    )
    return f"refs/remotes/origin/{base}"


async def _detect_default_branch(bare_repo: str) -> str:
    """Detect the default branch from a bare repository."""
    code, out, _ = await execute_git_command(
        ["git", f"--git-dir={bare_repo}", "branch", "--list", "-r"]
    )
    if code == 0 and out:
        branches = [b.strip() for b in out.split("\n") if b.strip() and "origin/" in b]
        if branches:
            branch_name = branches[0].replace("origin/", "")
            return f"refs/remotes/origin/{branch_name}"
    return "refs/remotes/origin/main"


def get_project_dir_for_worktree(worktree_path: Path) -> Path:
    """Return the SDK project directory for a given worktree path."""
    safe_cwd = str(worktree_path).replace("/", "-").replace("\\", "-")
    return Path.home() / ".claude" / "projects" / safe_cwd


async def cleanup_worktrees(
    repo: str,
    thread_type: str,
    thread_id: str,
) -> None:
    """Remove all worktrees for a specific thread (PR close, issue close, etc.)."""
    safe_repo = repo.replace("/", "--")
    thread_dir = WORKTREE_BASE / safe_repo / f"{thread_type}-{thread_id}"

    if not thread_dir.exists():
        return

    for workflow_dir in thread_dir.iterdir():
        if workflow_dir.is_dir():
            try:
                shutil.rmtree(workflow_dir, ignore_errors=True)
                project_dir = get_project_dir_for_worktree(workflow_dir)
                if project_dir.exists():
                    shutil.rmtree(project_dir, ignore_errors=True)
                logger.info(f"Cleaned up worktree and project dir: {workflow_dir}")
            except Exception as e:
                logger.warning(f"Failed to clean up {workflow_dir}: {e}")

    # Remove the thread dir itself if empty
    try:
        thread_dir.rmdir()
    except OSError:
        pass


async def cleanup_worktrees_by_branch(repo: str, branch: str) -> None:
    """Remove worktrees tracking a specific branch (branch deleted event)."""
    safe_repo = repo.replace("/", "--")
    repo_dir = WORKTREE_BASE / safe_repo
    if not repo_dir.exists():
        return

    for thread_dir in repo_dir.iterdir():
        if not thread_dir.is_dir():
            continue
        for workflow_dir in thread_dir.iterdir():
            if not workflow_dir.is_dir():
                continue
            try:
                code, out, _ = await execute_git_command(
                    [
                        "git",
                        "-C",
                        str(workflow_dir),
                        "rev-parse",
                        "--abbrev-ref",
                        "HEAD",
                    ]
                )
                if code == 0 and branch in (out or ""):
                    shutil.rmtree(workflow_dir, ignore_errors=True)
                    project_dir = get_project_dir_for_worktree(workflow_dir)
                    if project_dir.exists():
                        shutil.rmtree(project_dir, ignore_errors=True)
                    logger.info(
                        f"Cleaned up worktree and project dir for deleted branch: {workflow_dir}"
                    )
            except Exception as e:
                logger.debug(f"Worktree branch check/remove failed: {e}")


async def detect_orphan_worktrees(session_store: Any) -> list[Path]:
    """Find worktrees on disk with no corresponding Redis session.

    Returns list of orphan paths for optional cleanup.
    """
    orphans: list[Path] = []
    if not WORKTREE_BASE.exists():
        return orphans

    sessions = await session_store.list_sessions("*")
    active_paths = {s.worktree_path for s in sessions}

    for repo_dir in WORKTREE_BASE.iterdir():
        if not repo_dir.is_dir():
            continue
        for thread_dir in repo_dir.iterdir():
            if not thread_dir.is_dir():
                continue
            for workflow_dir in thread_dir.iterdir():
                if not workflow_dir.is_dir():
                    continue
                if str(workflow_dir) not in active_paths:
                    orphans.append(workflow_dir)

    if orphans:
        logger.info(f"Detected {len(orphans)} orphan worktrees")

    return orphans
