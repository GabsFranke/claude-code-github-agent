"""Post-session workers are off by default and pick their own model tier.

Memory extraction and retrospection each run a *second* Claude session after
every job, so they stay off until explicitly turned on. The switch is a replica
count rather than a boolean: the same number is the compose ``scale`` for the
service and the value the rest of the system reads, so a worker with no
container also has no work queued for it. Their tier is an alias
(opus/sonnet/haiku) resolved by the CLI through ANTHROPIC_DEFAULT_*_MODEL,
never a dated model id.
"""

import pathlib

import pytest

from shared.constants import MODEL_TIERS, replica_count, resolve_model_tier
from shared.sdk_factory import SDKOptionsBuilder

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


@pytest.mark.unit
class TestReplicaCount:
    def test_unset_is_off(self, monkeypatch):
        monkeypatch.delenv("MEMORY_WORKER_REPLICAS", raising=False)
        assert replica_count("MEMORY_WORKER_REPLICAS") == 0

    def test_blank_is_off(self, monkeypatch):
        monkeypatch.setenv("MEMORY_WORKER_REPLICAS", "   ")
        assert replica_count("MEMORY_WORKER_REPLICAS") == 0

    @pytest.mark.parametrize(("value", "expected"), [("0", 0), ("1", 1), (" 3 ", 3)])
    def test_counts_are_read(self, monkeypatch, value, expected):
        monkeypatch.setenv("MEMORY_WORKER_REPLICAS", value)
        assert replica_count("MEMORY_WORKER_REPLICAS") == expected

    @pytest.mark.parametrize("value", ["true", "yes", "on", "1.5", ""])
    def test_non_numeric_degrades_to_the_default(self, monkeypatch, value):
        """An old boolean in .env must not be read as "on"."""
        monkeypatch.setenv("MEMORY_WORKER_REPLICAS", value)
        assert replica_count("MEMORY_WORKER_REPLICAS") == 0

    def test_negative_is_off(self, monkeypatch):
        monkeypatch.setenv("MEMORY_WORKER_REPLICAS", "-2")
        assert replica_count("MEMORY_WORKER_REPLICAS") == 0


@pytest.mark.unit
class TestResolveModelTier:
    def test_unset_uses_default(self, monkeypatch):
        monkeypatch.delenv("MEMORY_WORKER_MODEL", raising=False)
        assert resolve_model_tier("MEMORY_WORKER_MODEL", "haiku") == "haiku"

    @pytest.mark.parametrize("tier", MODEL_TIERS)
    def test_each_tier_is_accepted(self, monkeypatch, tier):
        monkeypatch.setenv("MEMORY_WORKER_MODEL", tier.upper())
        assert resolve_model_tier("MEMORY_WORKER_MODEL", "haiku") == tier

    def test_unknown_tier_falls_back(self, monkeypatch):
        """A dated id or a typo degrades to the default, never fails startup."""
        monkeypatch.setenv("MEMORY_WORKER_MODEL", "claude-haiku-4-5-20251001")
        assert resolve_model_tier("MEMORY_WORKER_MODEL", "haiku") == "haiku"


@pytest.mark.unit
class TestWithModelTier:
    @pytest.mark.parametrize("tier", MODEL_TIERS)
    def test_tier_reaches_the_options(self, tier):
        options = SDKOptionsBuilder(cwd="/tmp").with_model_tier(tier).build()
        assert options.model == tier

    def test_unknown_tier_leaves_resolution_to_the_cli(self):
        options = SDKOptionsBuilder(cwd="/tmp").with_model_tier("gpt-9").build()
        assert not options.model


@pytest.mark.unit
class TestPostSessionHooksFollowTheReplicaCount:
    """No hooks are registered unless a worker is actually running."""

    def _hooks(self, monkeypatch, **env):
        for key in ("MEMORY_WORKER_REPLICAS", "RETROSPECTOR_REPLICAS"):
            monkeypatch.delenv(key, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        builder = SDKOptionsBuilder(cwd="/tmp").with_transcript_staging(repo="o/r")
        return builder.build().hooks or {}

    def test_no_hooks_when_both_unset(self, monkeypatch):
        assert self._hooks(monkeypatch) == {}

    def test_no_hooks_at_zero_replicas(self, monkeypatch):
        hooks = self._hooks(
            monkeypatch, MEMORY_WORKER_REPLICAS="0", RETROSPECTOR_REPLICAS="0"
        )
        assert hooks == {}

    def test_hooks_when_memory_runs(self, monkeypatch):
        hooks = self._hooks(monkeypatch, MEMORY_WORKER_REPLICAS="1")
        assert "Stop" in hooks and "SubagentStop" in hooks

    def test_hooks_when_retrospector_runs(self, monkeypatch):
        hooks = self._hooks(monkeypatch, RETROSPECTOR_REPLICAS="2")
        assert "Stop" in hooks and "SubagentStop" in hooks


@pytest.mark.unit
class TestComposeScaleGatesTheContainers:
    """The replica count must reach compose, and the same var must reach the
    sandbox worker that decides whether to queue jobs for these workers."""

    @staticmethod
    def _services():
        import yaml

        compose = REPO_ROOT / "docker-compose.yml"
        return yaml.safe_load(compose.read_text(encoding="utf-8"))["services"]

    @pytest.mark.parametrize(
        ("service", "env_var"),
        [
            ("memory_worker", "MEMORY_WORKER_REPLICAS"),
            ("retrospector_worker", "RETROSPECTOR_REPLICAS"),
        ],
    )
    def test_service_scale_defaults_to_zero(self, service, env_var):
        assert self._services()[service]["scale"] == f"${{{env_var}:-0}}"

    @pytest.mark.parametrize(
        "env_var", ["MEMORY_WORKER_REPLICAS", "RETROSPECTOR_REPLICAS"]
    )
    def test_sandbox_worker_sees_the_count(self, env_var):
        """Without it the Stop hooks would never register, silently."""
        environment = self._services()["sandbox_worker"]["environment"]
        assert any(item.startswith(f"{env_var}=") for item in environment)

    def test_env_example_ships_both_off(self):
        env = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
        assert "MEMORY_WORKER_REPLICAS=0" in env
        assert "RETROSPECTOR_REPLICAS=0" in env
