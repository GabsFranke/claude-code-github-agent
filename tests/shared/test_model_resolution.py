"""Tests for which model the SDK ends up running on.

Model resolution belongs to the Claude Code CLI, not to us. It already reads
``~/.claude/settings.json`` (via ``--setting-sources``) and the
``ANTHROPIC_DEFAULT_*_MODEL`` variables, and the SDK omits ``--model``
entirely when ``ClaudeAgentOptions.model`` is falsy.

Re-implementing that resolution here is what caused the original bug: the
copy only looked at the settings ``env`` sub-map, missed the top-level
``model`` key users actually set, and silently pinned every job to a
hardcoded id that later reached end-of-life.

So the contract is: set no model unless a caller explicitly asks for one, and
express tier requests as the CLI's own aliases rather than dated ids.
"""

import json
from pathlib import Path

import pytest

from shared.sdk_factory import SDKOptionsBuilder

REPO_ROOT = Path(__file__).resolve().parents[2]


class TestDefaultLeavesResolutionToTheCLI:
    """No tier requested means we must not pin a model."""

    def test_model_is_unset_by_default(self):
        options = SDKOptionsBuilder(cwd="/tmp").build()

        assert not options.model, (
            "A model was pinned without anyone asking for one. The SDK only "
            "omits --model when this is falsy, and omitting it is what lets "
            "the CLI honour settings.json."
        )

    def test_settings_json_is_not_read_by_us(self, tmp_path, monkeypatch):
        """A configured model must not change what we pass — the CLI reads it."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "settings.json").write_text(
            json.dumps({"model": "opus[1m]"}), encoding="utf-8"
        )
        monkeypatch.setattr("shared.sdk_factory.Path.home", lambda: tmp_path)

        options = SDKOptionsBuilder(cwd="/tmp").build()

        assert not options.model

    def test_anthropic_env_var_is_not_read_by_us(self, monkeypatch):
        """Same for ANTHROPIC_DEFAULT_SONNET_MODEL — the CLI honours it."""
        monkeypatch.setenv("ANTHROPIC_DEFAULT_SONNET_MODEL", "some-model")

        options = SDKOptionsBuilder(cwd="/tmp").build()

        assert not options.model

    def test_setting_sources_are_passed_so_the_cli_can_resolve(self):
        """Delegating only works if the CLI is told to load user settings."""
        options = SDKOptionsBuilder(cwd="/tmp").build()

        assert options.setting_sources is not None
        assert "user" in options.setting_sources


class TestExplicitTierRequests:
    """Tier helpers use CLI aliases, which resolve to the current model."""

    def test_with_sonnet_uses_the_alias(self):
        options = SDKOptionsBuilder(cwd="/tmp").with_sonnet().build()

        assert options.model == "sonnet"

    def test_with_haiku_uses_the_alias(self):
        options = SDKOptionsBuilder(cwd="/tmp").with_haiku().build()

        assert options.model == "haiku"

    def test_explicit_model_is_passed_through(self):
        options = SDKOptionsBuilder(cwd="/tmp").with_model("claude-opus-5").build()

        assert options.model == "claude-opus-5"

    def test_tier_request_is_unaffected_by_configured_model(
        self, tmp_path, monkeypatch
    ):
        """Asking for Haiku means Haiku, whatever the user's default is."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "settings.json").write_text(
            json.dumps({"model": "opus[1m]"}), encoding="utf-8"
        )
        monkeypatch.setattr("shared.sdk_factory.Path.home", lambda: tmp_path)

        options = SDKOptionsBuilder(cwd="/tmp").with_haiku().build()

        assert options.model == "haiku"


class TestNoDatedModelIds:
    """Dated ids go end-of-life; aliases do not."""

    @pytest.mark.parametrize("alias", ["sonnet", "haiku"])
    def test_tier_aliases_carry_no_date(self, alias):
        options = getattr(SDKOptionsBuilder(cwd="/tmp"), f"with_{alias}")().build()

        assert options.model == alias
        assert not any(c.isdigit() for c in options.model)

    def test_no_dated_model_id_is_hardcoded_in_source(self):
        """Guards against reintroducing a version that will silently expire.

        A dated id in a default is invisible until the model retires, at which
        point every job fails at once. Tests and docs may still mention them.
        """
        import re

        dated = re.compile(r"claude-[a-z]+-[\d.]+-\d{8}")
        offenders = []
        for directory in ("shared", "services"):
            for path in (REPO_ROOT / directory).rglob("*.py"):
                if "venv" in path.parts or "__pycache__" in path.parts:
                    continue
                for i, line in enumerate(
                    path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1
                ):
                    if dated.search(line):
                        offenders.append(f"{path.relative_to(REPO_ROOT)}:{i}")

        assert not offenders, (
            "Dated model ids found; prefer a CLI alias so the model cannot "
            f"silently expire: {offenders}"
        )
