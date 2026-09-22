"""Git configuration and submodule setup for sandbox worktrees.

Extracted from processor.py to keep the JobProcessor class focused.
These are standalone async operations that run after worktree creation.
"""

import logging
import os
import re
import tempfile
import time
from pathlib import Path

from shared import WorktreeCreationError, execute_git_command
from shared.worktree_manager import WORKTREE_BASE

logger = logging.getLogger(__name__)

_SAFE_PATTERN = re.compile(r"^[a-zA-Z0-9\s.\-\[\]@]+$")
# Job ids are generated internally, but this keeps a hostile one from
# escaping the credential directory via path separators or traversal.
_SAFE_JOB_ID = re.compile(r"[^A-Za-z0-9_.-]")
CREDENTIALS_DIRNAME = "job-creds"


def credentials_dir() -> str:
    """Return the directory holding per-job credential files, created 0o700.

    The base is ``BOT_CREDENTIALS_DIR`` when set, else the system temp dir.
    Deliberately *not* ``CLAUDE_TEMP_DIR``: this project documents that
    variable, and ``TMPDIR``, as being set to the job workspace, which is the
    one location a credential file must never occupy.

    ``TMPDIR`` also feeds ``tempfile.gettempdir()``, so the resolved directory
    is checked against the worktree root and rejected if it falls inside.
    Raising beats silently writing an installation token somewhere the agent
    is about to ``git add`` — set ``BOT_CREDENTIALS_DIR`` to recover.
    """
    base = os.environ.get("BOT_CREDENTIALS_DIR") or tempfile.gettempdir()
    creds_dir = os.path.abspath(os.path.join(base, CREDENTIALS_DIRNAME))

    worktree_root = os.path.abspath(str(WORKTREE_BASE))
    if os.path.normcase(creds_dir).startswith(os.path.normcase(worktree_root)):
        raise WorktreeCreationError(
            f"Refusing to write git credentials to {creds_dir}: it lies inside "
            f"the worktree root {worktree_root}, where the agent would stage "
            "the token. Set BOT_CREDENTIALS_DIR to a path outside it."
        )

    os.makedirs(creds_dir, mode=0o700, exist_ok=True)
    return creds_dir


def credentials_path(job_id: str) -> str:
    """Return the git credential file path for the job *job_id*.

    The file holds a live installation token, so it deliberately lives
    *outside* any checkout.  Inside a worktree it would be untracked but not
    ignored, and the agent's whole job is to stage, commit and push — a single
    ``git add -A`` would publish a token with write access to every repo in
    the installation.

    Keyed on the job id, not on the workspace path.  Persistent-session
    worktrees are laid out ``{repo}/{thread_type}-{thread_id}/{workflow}``, so
    a workspace-derived name would be the *workflow* and two jobs on different
    repos running the same workflow would share one credential file — each
    truncating the other's token and deleting it mid-push on cleanup.

    The caller removes the file when the job ends; unlike the old in-worktree
    location it no longer dies with the worktree.  See
    ``JobProcessor._cleanup`` and ``sweep_orphaned_credentials``.
    """
    safe = _SAFE_JOB_ID.sub("_", job_id) or "job"
    return os.path.join(credentials_dir(), f"{safe}.git-credentials")


def sweep_orphaned_credentials(max_age_seconds: int = 7200) -> int:
    """Delete credential files left behind by jobs that died non-gracefully.

    ``JobProcessor._cleanup`` is the normal deleter, but it does not run on
    SIGKILL, an OOM kill under ``mem_limit``, or a container restart mid-job,
    which would otherwise strand a live installation token on disk. The
    default age comfortably exceeds the ~1h installation-token lifetime, so a
    swept file is already useless.

    Returns the number of files removed.
    """
    removed = 0
    try:
        creds_dir = credentials_dir()
    except OSError as e:
        logger.warning(f"Cannot open credential directory to sweep: {e}")
        return 0

    cutoff = time.time() - max_age_seconds
    try:
        entries = os.listdir(creds_dir)
    except OSError as e:
        logger.warning(f"Cannot list credential directory {creds_dir}: {e}")
        return 0

    for name in entries:
        if not name.endswith(".git-credentials"):
            continue
        path = os.path.join(creds_dir, name)
        try:
            if os.path.getmtime(path) >= cutoff:
                continue
            os.remove(path)
            removed += 1
        except OSError as e:
            logger.warning(f"Failed to remove stale credential file {path}: {e}")

    if removed:
        logger.info(f"Swept {removed} orphaned credential file(s) from {creds_dir}")
    return removed


async def configure_git(workspace: str, github_token: str, job_id: str) -> None:
    """Configure git credentials and identity in a worktree.

    Sets up a per-job credential store keyed on *job_id*, configures
    user.name/user.email, and enables submodule authentication via the global
    credential helper.
    """
    # Validate the workspace is a valid git repo before running git commands
    code, _, _ = await execute_git_command(
        ["git", "-C", workspace, "rev-parse", "--git-dir"]
    )
    if code != 0:
        raise WorktreeCreationError(
            f"Workspace {workspace} is not a valid git repository. "
            "The worktree may have been corrupted (e.g., after container restart). "
            "Ensure the worktree directory has a valid .git file pointing to "
            "the bare repository."
        )

    credentials_file = credentials_path(job_id)
    # Set worktree-level credential helper for primary repo operations
    config_code, _, config_err = await execute_git_command(
        ["git", "config", "credential.helper", f"store --file={credentials_file}"],
        cwd=workspace,
    )
    if config_code != 0:
        raise WorktreeCreationError(
            f"Failed to configure git credentials: {config_err}"
        )

    # Also set globally — submodule clone operations spawn new git repos
    # that don't inherit worktree-level config.  The global setting
    # ensures submodule `git clone` can authenticate without an
    # interactive terminal.
    await execute_git_command(
        [
            "git",
            "config",
            "--global",
            "credential.helper",
            f"store --file={credentials_file}",
        ]
    )

    fd = os.open(credentials_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(
            fd,
            f"https://x-access-token:{github_token}@github.com\n".encode(),
        )
    finally:
        os.close(fd)

    bot_username = os.getenv("BOT_USERNAME", "Claude Code Agent")
    bot_email = os.getenv(
        "BOT_USER_EMAIL", "claude-code-agent[bot]@users.noreply.github.com"
    )

    if not _SAFE_PATTERN.match(bot_username):
        raise ValueError(f"BOT_USERNAME contains invalid characters: {bot_username!r}")
    if not _SAFE_PATTERN.match(bot_email):
        raise ValueError(f"BOT_USER_EMAIL contains invalid characters: {bot_email!r}")

    await execute_git_command(
        ["git", "config", "user.name", bot_username], cwd=workspace
    )
    await execute_git_command(["git", "config", "user.email", bot_email], cwd=workspace)


async def init_submodules(workspace: str, repo: str) -> None:
    """Initialize git submodules if .gitmodules exists in the worktree.

    Runs after worktree creation and git credential configuration so
    that private submodules can authenticate.  Failure is non-fatal —
    the agent can still work with source code, just without submodules.
    """
    gitmodules = Path(workspace) / ".gitmodules"
    if not gitmodules.exists():
        return

    logger.info(f"Found .gitmodules, initializing submodules for {repo}...")
    code, _, err = await execute_git_command(
        [
            "git",
            "-C",
            workspace,
            "submodule",
            "update",
            "--init",
            "--recursive",
        ]
    )
    if code != 0:
        logger.warning(
            f"Submodule init failed for {repo} (exit {code}): "
            f"{err}. Continuing without submodules."
        )
    else:
        logger.info(f"Submodules initialized successfully for {repo}")
