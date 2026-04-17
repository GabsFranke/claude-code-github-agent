"""Tests for workflow context profiles in workflows.yaml."""

from pathlib import Path

import pytest

from shared.context_builder import _MAX_FOCUS_FILES_PER_AREA, find_priority_focus_files
from workflows.engine import ContextProfile, WorkflowEngine


class TestContextProfile:
    def test_default_values(self):
        profile = ContextProfile()
        assert profile.repomap_budget == 2048
        assert profile.personalized is False
        assert profile.include_test_files is True
        assert profile.priority_focus == []

    def test_custom_values(self):
        profile = ContextProfile(
            repomap_budget=4096,
            personalized=True,
            include_test_files=False,
            priority_focus=["build_system"],
        )
        assert profile.repomap_budget == 4096
        assert profile.personalized is True
        assert profile.include_test_files is False
        assert profile.priority_focus == ["build_system"]


class TestWorkflowContextProfiles:
    @pytest.fixture
    def engine(self) -> WorkflowEngine:
        return WorkflowEngine()

    def test_review_pr_profile(self, engine: WorkflowEngine):
        profile = engine.get_context_profile("review-pr")
        assert profile["repomap_budget"] == 4096
        assert profile["personalized"] is True
        assert profile["include_test_files"] is True

    def test_fix_ci_profile(self, engine: WorkflowEngine):
        profile = engine.get_context_profile("fix-ci")
        assert profile["repomap_budget"] == 4096
        assert profile["personalized"] is True
        assert profile["priority_focus"] == ["build_system", "test_structure"]

    def test_triage_issue_profile(self, engine: WorkflowEngine):
        profile = engine.get_context_profile("triage-issue")
        assert profile["repomap_budget"] == 1024
        assert profile["personalized"] is False

    def test_generic_profile(self, engine: WorkflowEngine):
        profile = engine.get_context_profile("generic")
        assert profile["repomap_budget"] == 4096
        assert profile["personalized"] is False

    def test_unknown_workflow_returns_empty(self, engine: WorkflowEngine):
        profile = engine.get_context_profile("nonexistent")
        assert profile == {}

    def test_profiles_accessible_from_workflow_config(self, engine: WorkflowEngine):
        """Context profiles should be accessible from workflow configs."""
        for _name, config in engine.workflows.items():
            assert config.context is not None
            assert isinstance(config.context.repomap_budget, int)
            assert config.context.repomap_budget > 0


class TestPriorityFocusFiles:
    """Tests for the find_priority_focus_files function."""

    def test_finds_build_system_files(self, tmp_path: Path):
        """Should find Dockerfiles, CI configs, etc."""
        (tmp_path / "Dockerfile").write_text("FROM python:3.12")
        (tmp_path / "requirements.txt").write_text("fastapi")
        (tmp_path / "pyproject.toml").write_text("[project]")
        (tmp_path / ".github").mkdir()
        (tmp_path / ".github" / "workflows").mkdir()
        (tmp_path / ".github" / "workflows" / "ci.yml").write_text("name: CI")
        (tmp_path / "app.py").write_text("print('hi')")

        files = find_priority_focus_files(tmp_path, ["build_system"])

        assert "Dockerfile" in files
        assert "requirements.txt" in files
        assert "pyproject.toml" in files
        assert ".github/workflows/ci.yml" in files
        assert "app.py" not in files

    def test_finds_test_structure_files(self, tmp_path: Path):
        """Should find conftest, test files, etc."""
        (tmp_path / "conftest.py").write_text("import pytest")
        (tmp_path / "test_app.py").write_text("def test_foo(): pass")
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_bar.py").write_text("def test_bar(): pass")
        (tmp_path / "app.py").write_text("print('hi')")

        files = find_priority_focus_files(tmp_path, ["test_structure"])

        assert "conftest.py" in files
        assert "test_app.py" in files
        assert "tests/test_bar.py" in files
        assert "app.py" not in files

    def test_multiple_focus_areas(self, tmp_path: Path):
        """Should combine files from multiple focus areas."""
        (tmp_path / "Dockerfile").write_text("FROM python:3.12")
        (tmp_path / "conftest.py").write_text("import pytest")

        files = find_priority_focus_files(tmp_path, ["build_system", "test_structure"])

        assert "Dockerfile" in files
        assert "conftest.py" in files

    def test_unknown_focus_area(self, tmp_path: Path):
        """Should warn and skip unknown focus areas."""
        (tmp_path / "app.py").write_text("print('hi')")

        files = find_priority_focus_files(tmp_path, ["nonexistent_area"])

        assert files == []

    def test_empty_focus_areas(self, tmp_path: Path):
        """Should return empty list for no focus areas."""
        files = find_priority_focus_files(tmp_path, [])
        assert files == []

    def test_respects_exclude_dirs(self, tmp_path: Path):
        """Should not match files in excluded directories."""
        (tmp_path / "Dockerfile").write_text("FROM python:3.12")
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "Dockerfile").write_text("FROM node")

        files = find_priority_focus_files(tmp_path, ["build_system"])

        assert "Dockerfile" in files
        assert not any("node_modules" in f for f in files)


class TestPriorityFocusFilesExtended:
    """Extended tests for find_priority_focus_files edge cases."""

    def test_api_surface_finds_routes_and_endpoints(self, tmp_path: Path):
        """Should find api/, routes/, views.py, urls.py, etc."""
        (tmp_path / "api").mkdir()
        (tmp_path / "api" / "routes.py").write_text("# routes")
        (tmp_path / "routes").mkdir()
        (tmp_path / "routes" / "index.py").write_text("# index")
        (tmp_path / "views.py").write_text("# views")
        (tmp_path / "urls.py").write_text("# urls")
        (tmp_path / "router.py").write_text("# router")
        (tmp_path / "unrelated.py").write_text("# nope")

        files = find_priority_focus_files(tmp_path, ["api_surface"])

        assert "api/routes.py" in files
        assert "routes/index.py" in files
        assert "views.py" in files
        assert "urls.py" in files
        assert "router.py" in files
        assert "unrelated.py" not in files

    def test_dependencies_finds_manifest_files(self, tmp_path: Path):
        """Should find requirements.txt, package.json, go.mod, etc."""
        (tmp_path / "requirements.txt").write_text("fastapi")
        (tmp_path / "package.json").write_text("{}")
        (tmp_path / "go.mod").write_text("module example")
        (tmp_path / "Cargo.toml").write_text("[package]")
        (tmp_path / "Gemfile").write_text("source 'https://rubygems.org'")
        (tmp_path / "app.py").write_text("print('hi')")

        files = find_priority_focus_files(tmp_path, ["dependencies"])

        assert "requirements.txt" in files
        assert "package.json" in files
        assert "go.mod" in files
        assert "Cargo.toml" in files
        assert "Gemfile" in files
        assert "app.py" not in files

    def test_max_file_cap_respected(self, tmp_path: Path):
        """Result should be capped at _MAX_FOCUS_FILES_PER_AREA * len(focus_areas)."""
        focus_areas = ["build_system"]
        cap = _MAX_FOCUS_FILES_PER_AREA * len(focus_areas)

        # Create more matching files than the cap allows.
        # "setup.py" is a build_system pattern — create 60 of them in subdirs.
        for i in range(cap + 15):
            d = tmp_path / f"dir{i}"
            d.mkdir()
            (d / "setup.py").write_text(f"# setup {i}")

        files = find_priority_focus_files(tmp_path, focus_areas)

        assert len(files) <= cap

    def test_cross_area_dedup(self, tmp_path: Path):
        """pyproject.toml appears in both build_system and dependencies but only once."""
        (tmp_path / "pyproject.toml").write_text("[project]")

        files = find_priority_focus_files(tmp_path, ["build_system", "dependencies"])

        # pyproject.toml should appear exactly once
        count = files.count("pyproject.toml")
        assert count == 1
        assert "pyproject.toml" in files

    def test_mixed_known_and_unknown_areas(self, tmp_path: Path):
        """One valid + one invalid area returns only matches for the valid one."""
        (tmp_path / "Dockerfile").write_text("FROM python:3.12")
        (tmp_path / "app.py").write_text("print('hi')")

        files = find_priority_focus_files(
            tmp_path, ["build_system", "totally_fake_area"]
        )

        assert "Dockerfile" in files
        # app.py is not a build_system pattern, so it should not appear
        assert "app.py" not in files
