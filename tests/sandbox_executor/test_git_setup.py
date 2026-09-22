"""Tests for sandbox worktree git configuration.

The credential file holds a live GitHub App installation token with write
access to every repo in the installation. The agent's entire job is to stage,
commit and push, so where that file lives is a security property, not a
detail.
"""

import os
from unittest.mock import AsyncMock, patch

import pytest

from services.sandbox_executor.git_setup import (
    configure_git,
    credentials_path,
    sweep_orphaned_credentials,
)
from shared.exceptions import WorktreeCreationError
from shared.worktree_manager import get_worktree_path


@pytest.fixture
def creds_root(tmp_path, monkeypatch):
    """Point credential storage at an isolated directory.

    Uses BOT_CREDENTIALS_DIR rather than CLAUDE_TEMP_DIR or TMPDIR: this
    project documents both of those as being set to the job workspace, which
    is the one place the token must not go.
    """
    root = tmp_path / "creds-root"
    monkeypatch.setenv("BOT_CREDENTIALS_DIR", str(root))
    return root


class TestCredentialsPath:
    """The token file must never sit inside the tree the agent commits from."""

    def test_path_is_outside_every_real_worktree(self, creds_root):
        """Checked against real worktree layouts, not invented basenames.

        Persistent sessions live at
        ``{repo}/{thread_type}-{thread_id}/{workflow}``, so a path derived
        from the workspace would be named after the *workflow*.
        """
        workspace = str(get_worktree_path("owner/repo", "pr", "42", "generic"))

        path = credentials_path("job_abc123")

        assert not path.startswith(workspace), (
            "credential file is inside the checkout, where a single "
            f"`git add -A` would stage it: {path}"
        )

    def test_distinct_jobs_on_the_same_workflow_do_not_collide(self, creds_root):
        """Two repos running one workflow must not share a credential file.

        They hold different installation tokens and take different worktree
        locks. Sharing the file means each truncates the other's token, and
        whichever finishes first deletes it mid-push for the other.
        """
        first = credentials_path("job_aaa-1")
        second = credentials_path("job_bbb-2")

        assert first != second

    def test_workspace_derived_naming_would_have_collided(self):
        """Pins the reason the path is keyed on job_id rather than workspace.

        If this ever stops being true the basename approach becomes viable
        again, but while it holds, workspace-derived naming is unsafe.
        """
        a = get_worktree_path("owner/repoA", "pr", "1", "generic")
        b = get_worktree_path("other/repoB", "issue", "5", "generic")

        assert a != b
        assert os.path.basename(a) == os.path.basename(b) == "generic"

    def test_job_id_cannot_escape_the_credentials_directory(self, creds_root):
        path = credentials_path("../../etc/evil")

        assert os.path.dirname(path) == str(creds_root / "job-creds")

    def test_empty_job_id_still_yields_a_path(self, creds_root):
        assert credentials_path("").endswith("job.git-credentials")

    def test_directory_is_created(self, creds_root):
        path = credentials_path("job_ccc")

        assert os.path.isdir(os.path.dirname(path))

    def test_refuses_a_base_inside_the_worktree_root(self, monkeypatch):
        """TMPDIR and CLAUDE_TEMP_DIR are documented as the workspace.

        If the credential directory resolves inside the worktree root the
        function must fail loudly rather than write an installation token
        somewhere the agent is about to stage.
        """
        monkeypatch.setenv(
            "BOT_CREDENTIALS_DIR",
            str(get_worktree_path("owner/repo", "pr", "42", "generic")),
        )

        with pytest.raises(WorktreeCreationError, match="worktree root"):
            credentials_path("job_ddd")

    def test_tmpdir_pointing_at_a_worktree_is_also_refused(self, monkeypatch):
        """The same guard must hold via tempfile's own TMPDIR lookup."""
        monkeypatch.delenv("BOT_CREDENTIALS_DIR", raising=False)
        workspace = str(get_worktree_path("owner/repo", "issue", "7", "triage"))
        monkeypatch.setattr("tempfile.gettempdir", lambda: workspace)

        with pytest.raises(WorktreeCreationError, match="worktree root"):
            credentials_path("job_eee")


class TestConfigureGit:
    """``configure_git`` must point git at the out-of-tree credential file."""

    @pytest.mark.asyncio
    async def test_token_is_not_written_into_the_workspace(self, tmp_path, creds_root):
        workspace = tmp_path / "job_fff"
        workspace.mkdir()

        with patch(
            "services.sandbox_executor.git_setup.execute_git_command",
            new=AsyncMock(return_value=(0, "", "")),
        ):
            await configure_git(str(workspace), "ghs_exampletoken", "job-1")

        assert not (workspace / ".git-credentials").exists()

        creds = credentials_path("job-1")
        assert os.path.isfile(creds)
        with open(creds, encoding="utf-8") as fh:
            assert "ghs_exampletoken" in fh.read()

    @pytest.mark.asyncio
    async def test_credential_helper_points_at_the_out_of_tree_file(
        self, tmp_path, creds_root
    ):
        workspace = tmp_path / "job_ggg"
        workspace.mkdir()
        expected = credentials_path("job-2")

        runner = AsyncMock(return_value=(0, "", ""))
        with patch(
            "services.sandbox_executor.git_setup.execute_git_command", new=runner
        ):
            await configure_git(str(workspace), "ghs_exampletoken", "job-2")

        helper_args = [
            call.args[0]
            for call in runner.await_args_list
            if "credential.helper" in call.args[0]
        ]
        assert helper_args, "no credential.helper configuration was issued"
        for argv in helper_args:
            assert f"store --file={expected}" in argv


class TestSweepOrphanedCredentials:
    """A job killed non-gracefully must not strand a live token on disk.

    ``JobProcessor._cleanup`` is the normal deleter, but it does not run on
    SIGKILL, an OOM kill under ``mem_limit``, or a container restart mid-job.
    """

    def test_removes_files_older_than_the_cutoff(self, creds_root):
        stale = credentials_path("job-old")
        with open(stale, "w", encoding="utf-8") as fh:
            fh.write("https://x-access-token:ghs_stale@github.com\n")
        old = 1_000_000_000
        os.utime(stale, (old, old))

        removed = sweep_orphaned_credentials(max_age_seconds=3600)

        assert removed == 1
        assert not os.path.exists(stale)

    def test_leaves_a_live_job_alone(self, creds_root):
        live = credentials_path("job-live")
        with open(live, "w", encoding="utf-8") as fh:
            fh.write("https://x-access-token:ghs_live@github.com\n")

        removed = sweep_orphaned_credentials(max_age_seconds=3600)

        assert removed == 0
        assert os.path.exists(live)

    def test_ignores_unrelated_files(self, creds_root):
        credentials_path("seed")  # ensure the directory exists
        unrelated = os.path.join(str(creds_root / "job-creds"), "notes.txt")
        with open(unrelated, "w", encoding="utf-8") as fh:
            fh.write("keep me")
        os.utime(unrelated, (1_000_000_000, 1_000_000_000))

        removed = sweep_orphaned_credentials(max_age_seconds=3600)

        assert removed == 0
        assert os.path.exists(unrelated)
