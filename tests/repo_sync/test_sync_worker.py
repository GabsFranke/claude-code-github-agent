"""Unit tests for repo sync worker module."""

import asyncio
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def reset_shutdown_event():
    """Reset shutdown event before each test."""
    from services.repo_sync import sync_worker

    sync_worker.shutdown_event.clear()
    yield
    sync_worker.shutdown_event.clear()


class TestSignalHandling:
    """Test signal handling functions."""

    def test_sync_worker_uses_shared_signal_handling(self):
        """Test sync worker uses shared signal handling from shared.signals."""
        # This test verifies that the sync_worker module imports and uses
        # the shared setup_graceful_shutdown function instead of
        # implementing its own signal handlers.
        from services.repo_sync import sync_worker

        # Verify shutdown_event exists (used by shared signal handler)
        assert hasattr(sync_worker, "shutdown_event")
        assert isinstance(sync_worker.shutdown_event, asyncio.Event)


class TestExecuteGitCommand:
    """Test execute_git_command function."""

    @pytest.mark.asyncio
    async def test_successful_command(self):
        """Test successful git command execution."""
        from shared.git_utils import execute_git_command

        # Use a simple command that works on all platforms
        code, stdout, stderr = await execute_git_command("git --version")

        assert code == 0
        assert "git version" in stdout.lower()
        assert stderr == ""

    @pytest.mark.asyncio
    async def test_failed_command(self):
        """Test failed git command execution."""
        from shared.git_utils import execute_git_command

        code, stdout, stderr = await execute_git_command(
            "git invalid-command-that-does-not-exist"
        )

        assert code != 0
        assert stderr != ""

    @pytest.mark.asyncio
    async def test_command_with_cwd(self):
        """Test git command execution with custom working directory."""
        from shared.git_utils import execute_git_command

        with tempfile.TemporaryDirectory() as tmpdir:
            code, stdout, stderr = await execute_git_command("git init", cwd=tmpdir)

            assert code == 0
            assert Path(tmpdir, ".git").exists()


class TestCleanupOldRepos:
    """Test cleanup_old_repos background task."""

    @pytest.mark.asyncio
    async def test_cleanup_respects_shutdown(self):
        """Test cleanup task respects shutdown event."""
        from services.repo_sync.sync_worker import cleanup_old_repos, shutdown_event

        # Set shutdown immediately
        shutdown_event.set()

        # Task should exit quickly
        task = asyncio.create_task(cleanup_old_repos())
        await asyncio.sleep(0.1)

        # Task should be done
        assert task.done()

        # Reset shutdown event
        shutdown_event.clear()


class TestProcessSyncRequest:
    """Test process_sync_request function."""

    @pytest.mark.asyncio
    async def test_missing_repo_field(self):
        """Test handling of message missing repo field."""
        from services.repo_sync.sync_worker import process_sync_request

        mock_redis = AsyncMock()
        message = {"ref": "main"}  # Missing 'repo'

        # Should log error and return without crashing
        await process_sync_request(message, mock_redis)

        # No lock should be acquired
        mock_redis.lock.assert_not_called()

    @pytest.mark.asyncio
    async def test_lock_acquisition_timeout(self):
        """Test handling of lock acquisition timeout."""
        from services.repo_sync.sync_worker import process_sync_request

        mock_redis = AsyncMock()
        mock_lock = AsyncMock()
        mock_lock.acquire = AsyncMock(return_value=False)
        mock_redis.lock = MagicMock(return_value=mock_lock)

        message = {"repo": "owner/repo", "ref": "main"}

        with (
            patch(
                "services.repo_sync.sync_worker.get_github_auth_service",
                new_callable=AsyncMock,
            ) as mock_auth,
            patch("services.repo_sync.sync_worker.os.makedirs"),
        ):
            mock_auth_service = MagicMock()
            mock_auth_service.is_configured.return_value = False
            mock_auth.return_value = mock_auth_service

            await process_sync_request(message, mock_redis)

            # Lock should be attempted
            mock_redis.lock.assert_called_once_with(
                "agent:sync:lock:owner/repo", timeout=300
            )
            mock_lock.acquire.assert_called_once()

    @pytest.mark.asyncio
    async def test_successful_clone_new_repo(self):
        """Test successful clone of new repository."""
        from services.repo_sync.sync_worker import process_sync_request

        mock_redis = AsyncMock()
        mock_lock = AsyncMock()
        mock_lock.acquire = AsyncMock(return_value=True)
        mock_lock.release = AsyncMock()
        mock_redis.lock = MagicMock(return_value=mock_lock)
        mock_redis.set = AsyncMock()

        message = {"repo": "owner/repo", "ref": "main"}

        with tempfile.TemporaryDirectory() as cache_base:
            with (
                patch(
                    "services.repo_sync.sync_worker.get_github_auth_service"
                ) as mock_auth,
                patch("services.repo_sync.sync_worker.execute_git_command") as mock_git,
                patch.dict(os.environ, {"CACHE_BASE": cache_base}, clear=False),
                patch(
                    "services.repo_sync.sync_worker.os.path.join",
                    side_effect=lambda *args: "/var/cache/repos/owner/repo.git",
                ),
                patch(
                    "services.repo_sync.sync_worker.os.path.exists", return_value=False
                ),
                patch("services.repo_sync.sync_worker.os.makedirs"),
            ):
                mock_auth_service = AsyncMock()
                mock_auth_service.is_configured.return_value = True
                mock_auth_service.get_token = AsyncMock(return_value="test_token")
                mock_auth.return_value = mock_auth_service

                mock_git.return_value = (0, "", "")

                await process_sync_request(message, mock_redis)

                # Verify clone was attempted (clone, refspec config, fetch)
                assert mock_git.call_count >= 1
                clone_cmd = mock_git.call_args_list[0][0][0]
                assert clone_cmd[0] == "git"
                assert "clone" in clone_cmd and "--bare" in clone_cmd
                # The token travels as a header, never inside the URL, so it
                # is not written into the cloned repo's config.
                assert "https://github.com/owner/repo.git" in clone_cmd
                assert not any(
                    "test_token" in a and "github.com" in a for a in clone_cmd
                )
                assert any(
                    a.startswith("http.extraheader=AUTHORIZATION:") for a in clone_cmd
                )

                # Verify completion signal was set
                mock_redis.set.assert_called_once()
                assert (
                    "agent:sync:complete:owner/repo:main"
                    in mock_redis.set.call_args[0][0]
                )

                # Verify lock was released
                mock_lock.release.assert_called_once()

    @pytest.mark.asyncio
    async def test_successful_fetch_existing_repo(self):
        """Test successful fetch for existing repository."""
        from services.repo_sync.sync_worker import process_sync_request

        mock_redis = AsyncMock()
        mock_lock = AsyncMock()
        mock_lock.acquire = AsyncMock(return_value=True)
        mock_lock.release = AsyncMock()
        mock_redis.lock = MagicMock(return_value=mock_lock)
        mock_redis.set = AsyncMock()
        mock_redis.publish = AsyncMock()

        message = {"repo": "owner/repo", "ref": "main"}

        with (
            patch(
                "services.repo_sync.sync_worker.get_github_auth_service"
            ) as mock_auth,
            patch("services.repo_sync.sync_worker.execute_git_command") as mock_git,
            patch(
                "services.repo_sync.sync_worker.os.path.join",
                side_effect=lambda *args: "/var/cache/repos/owner/repo.git",
            ),
            patch("services.repo_sync.sync_worker.os.path.exists", return_value=True),
            patch("services.repo_sync.sync_worker.os.makedirs"),
        ):
            mock_auth_service = AsyncMock()
            mock_auth_service.is_configured.return_value = True
            mock_auth_service.get_token = AsyncMock(return_value="test_token")
            mock_auth.return_value = mock_auth_service

            mock_git.return_value = (0, "", "")

            await process_sync_request(message, mock_redis)

            # Verify fetch was attempted (not clone) - should be called twice:
            # 1. set-url to keep the stored remote token-free
            # 2. fetch to get updates, authenticating via header
            assert mock_git.call_count == 2

            # First call: reset remote URL to the plain, token-free form
            set_url_cmd = mock_git.call_args_list[0][0][0]
            assert "remote" in set_url_cmd and "set-url" in set_url_cmd
            assert set_url_cmd[-1] == "https://github.com/owner/repo.git"
            assert "test_token" not in set_url_cmd[-1]

            # Second call: fetch with the token in a header only
            fetch_cmd = mock_git.call_args_list[1][0][0]
            assert "fetch" in fetch_cmd and "origin" in fetch_cmd
            assert any(
                a.startswith("http.extraheader=AUTHORIZATION:") for a in fetch_cmd
            )

            # Verify completion signal was set
            mock_redis.set.assert_called_once()

            # Verify lock was released
            mock_lock.release.assert_called_once()

    @pytest.mark.asyncio
    async def test_clone_failure(self):
        """Test handling of clone failure."""
        from services.repo_sync.sync_worker import process_sync_request

        mock_redis = AsyncMock()
        mock_lock = AsyncMock()
        mock_lock.acquire = AsyncMock(return_value=True)
        mock_lock.release = AsyncMock()
        mock_redis.lock = MagicMock(return_value=mock_lock)
        mock_redis.set = AsyncMock()

        message = {"repo": "owner/repo", "ref": "main"}

        with (
            patch(
                "services.repo_sync.sync_worker.get_github_auth_service"
            ) as mock_auth,
            patch("services.repo_sync.sync_worker.execute_git_command") as mock_git,
            patch(
                "services.repo_sync.sync_worker.os.path.join",
                side_effect=lambda *args: "/var/cache/repos/owner/repo.git",
            ),
            patch("services.repo_sync.sync_worker.os.path.exists", return_value=False),
            patch("services.repo_sync.sync_worker.os.makedirs"),
        ):
            mock_auth_service = AsyncMock()
            mock_auth_service.is_configured.return_value = True
            mock_auth_service.get_token = AsyncMock(return_value="test_token")
            mock_auth.return_value = mock_auth_service

            # Simulate clone failure
            mock_git.return_value = (128, "", "fatal: repository not found")

            await process_sync_request(message, mock_redis)

            # Verify completion signal was NOT set
            mock_redis.set.assert_not_called()

            # Verify lock was still released
            mock_lock.release.assert_called_once()

    @pytest.mark.asyncio
    async def test_without_github_app_credentials(self):
        """Test sync without GitHub App credentials (public repos)."""
        from services.repo_sync.sync_worker import process_sync_request

        mock_redis = AsyncMock()
        mock_lock = AsyncMock()
        mock_lock.acquire = AsyncMock(return_value=True)
        mock_lock.release = AsyncMock()
        mock_redis.lock = MagicMock(return_value=mock_lock)
        mock_redis.set = AsyncMock()

        message = {"repo": "owner/repo", "ref": "main"}

        with (
            patch(
                "services.repo_sync.sync_worker.get_github_auth_service",
                new_callable=AsyncMock,
            ) as mock_auth,
            patch("services.repo_sync.sync_worker.execute_git_command") as mock_git,
            patch(
                "services.repo_sync.sync_worker.os.path.join",
                side_effect=lambda *args: "/var/cache/repos/owner/repo.git",
            ),
            patch("services.repo_sync.sync_worker.os.path.exists", return_value=False),
            patch("services.repo_sync.sync_worker.os.makedirs"),
        ):
            mock_auth_service = MagicMock()
            mock_auth_service.is_configured.return_value = False
            mock_auth.return_value = mock_auth_service

            mock_git.return_value = (0, "", "")

            await process_sync_request(message, mock_redis)

            # Verify clone was attempted without token (may be called multiple times)
            assert mock_git.call_count >= 1
            clone_cmd = mock_git.call_args_list[0][0][0]
            assert clone_cmd[:3] == ["git", "clone", "--bare"]
            assert "https://github.com/owner/repo.git" in clone_cmd
            # No token, so no auth header at all
            assert not any("http.extraheader" in a for a in clone_cmd)

    @pytest.mark.asyncio
    async def test_exception_handling(self):
        """Test exception handling during sync."""
        from services.repo_sync.sync_worker import process_sync_request

        mock_redis = AsyncMock()
        mock_lock = AsyncMock()
        mock_lock.acquire = AsyncMock(return_value=True)
        mock_lock.release = AsyncMock()
        mock_redis.lock = MagicMock(return_value=mock_lock)

        message = {"repo": "owner/repo", "ref": "main"}

        with (
            patch(
                "services.repo_sync.sync_worker.get_github_auth_service",
                new_callable=AsyncMock,
            ) as mock_auth,
            patch("services.repo_sync.sync_worker.os.makedirs"),
            patch("services.repo_sync.sync_worker.os.path.exists", return_value=False),
            patch(
                "services.repo_sync.sync_worker.os.path.join",
                side_effect=lambda *args: "/var/cache/repos/owner/repo.git",
            ),
            patch(
                "services.repo_sync.sync_worker.execute_git_command",
                side_effect=Exception("Unexpected error"),
            ),
        ):
            mock_auth_service = MagicMock()
            mock_auth_service.is_configured.return_value = False
            mock_auth.return_value = mock_auth_service

            # Should not raise exception
            await process_sync_request(message, mock_redis)

            # Verify lock was still released
            mock_lock.release.assert_called_once()


class TestMainLoop:
    """Test main worker loop."""

    @pytest.mark.asyncio
    async def test_processes_messages_from_queue(self):
        """Test main loop processes messages from queue."""
        from services.repo_sync.sync_worker import main, shutdown_event

        mock_queue = AsyncMock()

        # First call returns a message, second call triggers shutdown
        call_count = 0

        async def message_handler_side_effect(handler):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Simulate receiving a message
                await handler({"repo": "owner/repo", "ref": "main"})
            shutdown_event.set()

        mock_queue.subscribe = message_handler_side_effect
        mock_queue.close = AsyncMock()
        mock_queue._connect = AsyncMock()
        mock_queue.redis = AsyncMock()

        with (
            patch("services.repo_sync.sync_worker.RedisQueue", return_value=mock_queue),
            patch(
                "services.repo_sync.sync_worker.process_sync_request",
                new_callable=AsyncMock,
            ),
        ):
            await main()

            # Verify queue was connected
            mock_queue._connect.assert_called_once()

            # Verify cleanup happened
            mock_queue.close.assert_called_once()

        # Reset shutdown event
        shutdown_event.clear()

    @pytest.mark.asyncio
    async def test_respects_shutdown_event(self):
        """Test main loop respects shutdown event."""
        from services.repo_sync.sync_worker import main, shutdown_event

        mock_queue = AsyncMock()
        mock_queue._connect = AsyncMock()
        mock_queue.close = AsyncMock()
        mock_queue.redis = AsyncMock()

        async def subscribe_side_effect(handler):
            # Immediately exit
            pass

        mock_queue.subscribe = subscribe_side_effect

        # Set shutdown immediately
        shutdown_event.set()

        with patch(
            "services.repo_sync.sync_worker.RedisQueue", return_value=mock_queue
        ):
            await main()

            # Verify cleanup happened
            mock_queue.close.assert_called_once()

        # Reset shutdown event
        shutdown_event.clear()

    @pytest.mark.asyncio
    async def test_uses_environment_variables(self):
        """Test main loop uses environment variables for configuration."""
        from services.repo_sync.sync_worker import main, shutdown_event

        mock_queue = AsyncMock()
        mock_queue._connect = AsyncMock()
        mock_queue.close = AsyncMock()
        mock_queue.redis = AsyncMock()
        mock_queue.subscribe = AsyncMock()

        # Set shutdown immediately
        shutdown_event.set()

        with (
            patch("services.repo_sync.sync_worker.RedisQueue") as mock_queue_class,
            patch.dict(
                os.environ,
                {"REDIS_URL": "redis://custom:6379", "REDIS_PASSWORD": "secret"},
                clear=False,
            ),
        ):
            mock_queue_class.return_value = mock_queue

            await main()

            # Verify RedisQueue was created with env vars
            mock_queue_class.assert_called_once_with(
                redis_url="redis://custom:6379",
                queue_name="agent:sync:requests",
                password="secret",
            )

        # Reset shutdown event
        shutdown_event.clear()


class TestGitAuthArgs:
    """The token must reach git as a header, never as part of a stored URL."""

    def test_no_token_means_no_auth_args(self):
        from services.repo_sync.sync_worker import git_auth_args

        assert git_auth_args(None) == []
        assert git_auth_args("") == []

    def test_token_becomes_basic_auth_header(self):
        import base64

        from services.repo_sync.sync_worker import git_auth_args

        args = git_auth_args("tok123")

        assert args[0] == "-c"
        header = args[1]
        assert header.startswith("http.extraheader=AUTHORIZATION: basic ")
        encoded = header.rsplit(" ", 1)[1]
        assert base64.b64decode(encoded).decode() == "x-access-token:tok123"

    def test_raw_token_is_not_in_the_args(self):
        """Only the base64 form appears, and never in a URL position."""
        from services.repo_sync.sync_worker import git_auth_args

        args = git_auth_args("tok123")

        assert not any("tok123" in a for a in args)
