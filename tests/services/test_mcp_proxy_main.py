"""Tests for services/mcp_proxy/main.py — security boundary functions."""

import os
from unittest.mock import patch

# Import the module under test
from services.mcp_proxy.main import _build_subprocess_env, _is_safe_server_name


class TestIsSafeServerName:
    """Test _is_safe_server_name for path traversal and injection prevention."""

    def test_accepts_valid_names(self):
        assert _is_safe_server_name("github") is True
        assert _is_safe_server_name("my-server") is True
        assert _is_safe_server_name("codebase_tools") is True
        assert _is_safe_server_name("Server123") is True

    def test_rejects_empty(self):
        assert _is_safe_server_name("") is False

    def test_rejects_path_traversal(self):
        assert _is_safe_server_name("..") is False
        assert _is_safe_server_name("../etc/passwd") is False
        assert _is_safe_server_name("server/../../etc") is False

    def test_rejects_slashes(self):
        assert _is_safe_server_name("server/name") is False
        assert _is_safe_server_name("server\\name") is False

    def test_rejects_null_bytes(self):
        assert _is_safe_server_name("server\x00evil") is False

    def test_rejects_spaces(self):
        assert _is_safe_server_name("server name") is False

    def test_rejects_special_chars(self):
        assert _is_safe_server_name("server;rm") is False
        assert _is_safe_server_name("server&cmd") is False
        assert _is_safe_server_name("server|pipe") is False
        assert _is_safe_server_name("server$(cmd)") is False


class TestBuildSubprocessEnv:
    """Test _build_subprocess_env for sensitive var exclusion."""

    def test_includes_safe_system_vars(self):
        """Safe system vars like PATH and HOME should be included."""
        with patch.dict(
            os.environ,
            {"PATH": "/usr/bin", "HOME": "/home/user", "LANG": "en_US.UTF-8"},
            clear=True,
        ):
            env = _build_subprocess_env("memory")
        assert env.get("PATH") == "/usr/bin"
        assert env.get("HOME") == "/home/user"
        assert env.get("LANG") == "en_US.UTF-8"

    def test_excludes_sensitive_vars(self):
        """Sensitive vars should never appear in the subprocess env."""
        with patch.dict(
            os.environ,
            {
                "ANTHROPIC_API_KEY": "sk-ant-secret",
                "REDIS_PASSWORD": "super-secret",
                "PATH": "/usr/bin",
                "HOME": "/home/user",
            },
            clear=True,
        ):
            env = _build_subprocess_env("memory")
        assert "ANTHROPIC_API_KEY" not in env
        assert "REDIS_PASSWORD" not in env
        assert "PATH" in env

    def test_includes_whitelisted_prefixes(self):
        """Vars matching allowed prefixes should be included."""
        with patch.dict(
            os.environ,
            {
                "REPO_URL": "https://github.com/owner/repo",
                "GITHUB_TOKEN": "ghs_test",
                "MCP_CONFIG": "test",
                "PATH": "/usr/bin",
            },
            clear=True,
        ):
            env = _build_subprocess_env("memory")
        assert env.get("REPO_URL") == "https://github.com/owner/repo"
        assert env.get("GITHUB_TOKEN") == "ghs_test"
        assert env.get("MCP_CONFIG") == "test"

    def test_excludes_non_whitelisted_vars(self):
        """Vars that don't match any allowed prefix should be excluded."""
        with patch.dict(
            os.environ,
            {
                "RANDOM_VAR": "value",
                "AWS_SECRET_KEY": "secret",
                "PATH": "/usr/bin",
            },
            clear=True,
        ):
            env = _build_subprocess_env("memory")
        assert "RANDOM_VAR" not in env
        assert "AWS_SECRET_KEY" not in env


class TestPerServerSecrets:
    """A secret must reach only the MCP server declared to need it.

    SCHEDULER_INTERNAL_TOKEN authenticates the scheduler's one-shot endpoint.
    Injecting it into every spawned server is unnecessary blast radius on a
    credential whose whole purpose is to bound who can enqueue agent jobs.
    """

    def test_scheduler_receives_its_token(self):
        with patch.dict(os.environ, {"SCHEDULER_INTERNAL_TOKEN": "s3cret"}, clear=True):
            env = _build_subprocess_env("scheduler")

        assert env.get("SCHEDULER_INTERNAL_TOKEN") == "s3cret"

    def test_other_servers_do_not(self):
        with patch.dict(os.environ, {"SCHEDULER_INTERNAL_TOKEN": "s3cret"}, clear=True):
            for server in ("memory", "github_actions", "unknown"):
                env = _build_subprocess_env(server)
                assert "SCHEDULER_INTERNAL_TOKEN" not in env, server

    def test_token_is_not_overridable_by_an_allowed_prefix(self):
        """The name must not fall under a prefix a query param can set.

        Query parameters matching an allowed prefix are injected into the
        subprocess env, so a secret named e.g. MCP_* could be substituted by
        the caller.
        """
        from services.mcp_proxy.main import _get_allowed_env_prefixes

        for prefix in _get_allowed_env_prefixes():
            assert not "SCHEDULER_INTERNAL_TOKEN".startswith(prefix)
