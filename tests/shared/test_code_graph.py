"""Tests for the code_graph module (SymbolIndex and Relationship)."""

from pathlib import Path
from unittest.mock import patch

import pytest

from shared.code_graph import Relationship, SymbolIndex, _safe_repo_name
from tests.conftest import FakeSurrealDB


@pytest.fixture(autouse=True)
def _mock_surrealdb():
    """Mock SurrealDB for all tests — no real connection needed."""
    fake_db = FakeSurrealDB()

    with (
        patch("shared.surrealdb_client.is_initialized", return_value=True),
        patch("shared.surrealdb_client.get_surreal", return_value=fake_db),
        patch("shared.surrealdb_client.init_surrealdb"),
        patch("shared.surrealdb_client.apply_schema"),
        patch("shared.code_graph.get_surreal", return_value=fake_db),
        patch("shared.code_graph.is_initialized", return_value=True),
        patch("shared.code_graph.apply_schema"),
    ):
        yield fake_db


@pytest.fixture
def python_repo(tmp_path: Path) -> Path:
    """Create a Python repo with known structure for symbol index testing."""
    (tmp_path / "app.py").write_text(
        '"""Main application."""\n\n'
        "import os\n"
        "from database import Database\n\n\n"
        "class Application:\n"
        '    """Main app class."""\n\n'
        "    def __init__(self, name: str):\n"
        "        self.name = name\n"
        "        self.db = Database()\n\n"
        "    def run(self):\n"
        '        """Run the application."""\n'
        "        self.db.connect()\n"
        "        return True\n\n\n"
        "def create_app(config):\n"
        '    """Factory function."""\n'
        "    return Application(config['name'])\n"
    )

    (tmp_path / "database.py").write_text(
        '"""Database module."""\n\n\n'
        "class Database:\n"
        '    """Database connection."""\n\n'
        "    def __init__(self, url: str = 'sqlite:///default.db'):\n"
        "        self.url = url\n\n"
        "    def connect(self):\n"
        "        pass\n\n"
        "    def query(self, sql: str):\n"
        "        pass\n"
    )

    return tmp_path


@pytest.fixture
def symbol_index(python_repo: Path) -> SymbolIndex:
    """Build a SymbolIndex from the Python repo."""
    idx = SymbolIndex(repo_path=python_repo)
    idx.build()
    return idx


class TestRelationship:
    def test_creation(self):
        rel = Relationship(
            source_file="app.py",
            source_line=10,
            source_name="Application",
            target_name="Database",
            kind="calls",
        )
        assert rel.source_file == "app.py"
        assert rel.kind == "calls"
        assert rel.target_file is None

    def test_with_target_file(self):
        rel = Relationship(
            source_file="app.py",
            source_line=2,
            source_name="app",
            target_name="os",
            target_file="os",
            kind="imports",
        )
        assert rel.target_file == "os"


class TestSymbolIndexBuild:
    def test_build_sets_built_flag(self, symbol_index: SymbolIndex):
        assert symbol_index._built is True

    def test_unknown_symbol_returns_empty(self, symbol_index: SymbolIndex):
        defs = symbol_index.find_definitions("nonexistent_symbol")
        assert defs == []


class TestSymbolIndexQueries:
    def test_find_definitions(self, symbol_index: SymbolIndex):
        defs = symbol_index.find_definitions("Database")
        assert isinstance(defs, list)

    def test_find_references(self, symbol_index: SymbolIndex):
        refs = symbol_index.find_references("Database")
        assert isinstance(refs, list)

    def test_get_context(self, symbol_index: SymbolIndex):
        ctx = symbol_index.get_context("Application")
        assert ctx["symbol"] == "Application"
        assert "definitions" in ctx
        assert "calls" in ctx
        assert "called_by" in ctx

    def test_get_context_not_found(self, symbol_index: SymbolIndex):
        ctx = symbol_index.get_context("nonexistent_symbol")
        assert "error" in ctx

    def test_get_impact(self, symbol_index: SymbolIndex):
        impact = symbol_index.get_impact("database.py")
        assert impact["file"] == "database.py"
        assert "symbols_in_range" in impact
        assert "upstream_impact" in impact
        assert "downstream_impact" in impact
        assert "risk_level" in impact
        assert "risk_summary" in impact

    def test_get_impact_direction_upstream(self, symbol_index: SymbolIndex):
        impact = symbol_index.get_impact("database.py", direction="upstream")
        assert "upstream_impact" in impact
        assert "downstream_impact" in impact
        assert impact["downstream_impact"] == {}

    def test_get_impact_direction_downstream(self, symbol_index: SymbolIndex):
        impact = symbol_index.get_impact("app.py", direction="downstream")
        assert "downstream_impact" in impact
        assert "upstream_impact" in impact
        assert impact["upstream_impact"] == {}

    def test_get_impact_max_depth(self, symbol_index: SymbolIndex):
        impact = symbol_index.get_impact("database.py", max_depth=1)
        assert "upstream_impact" in impact
        # Depth 1 should only have at most depth 1 entries
        assert 2 not in impact["upstream_impact"]

    def test_get_impact_risk_assessment(self, symbol_index: SymbolIndex):
        impact = symbol_index.get_impact("database.py")
        assert impact["risk_level"] in ("low", "medium", "high")
        assert isinstance(impact["risk_summary"], str)
        assert len(impact["risk_summary"]) > 0

    def test_get_impact_clamps_max_depth(self, symbol_index: SymbolIndex):
        """max_depth should be clamped to 1-10 range."""
        impact_high = symbol_index.get_impact("database.py", max_depth=999)
        assert "upstream_impact" in impact_high  # should not error

        impact_low = symbol_index.get_impact("database.py", max_depth=0)
        assert "upstream_impact" in impact_low  # should not error, clamped to 1

    def test_get_file_overview(self, symbol_index: SymbolIndex):
        overview = symbol_index.get_file_overview("app.py")
        assert overview["file"] == "app.py"
        assert "definitions" in overview
        assert "imports" in overview

    def test_get_file_overview_empty(self, symbol_index: SymbolIndex):
        overview = symbol_index.get_file_overview("nonexistent.py")
        assert overview["definitions"] == []


class TestSymbolIndexCaching:
    def test_safe_repo_name(self):
        assert _safe_repo_name("owner/repo") == "owner--repo"
        assert _safe_repo_name("simple") == "simple"
        assert _safe_repo_name("org/project") == "org--project"


class TestSymbolIndexEmptyRepo:
    def test_empty_repo(self, tmp_path: Path):
        """Building index on empty repo should not error."""
        empty = tmp_path / "empty_repo"
        empty.mkdir()
        (empty / ".git").mkdir()
        (empty / ".git" / "HEAD").write_text("abc123\n")

        idx = SymbolIndex(repo_path=empty)
        idx.build()

        defs = idx.find_definitions("anything")
        assert defs == []

        ctx = idx.get_context("anything")
        assert "error" in ctx


class TestCrossFileImportResolution:
    def test_import_has_resolved_target_file(self, tmp_path: Path):
        """From-import relationships should have target_file resolved."""
        (tmp_path / "app.py").write_text(
            "from database import Database\n" "db = Database()\n"
        )
        (tmp_path / "database.py").write_text(
            "class Database:\n" "    def connect(self):\n" "        pass\n"
        )
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "HEAD").write_text("abc123\n")

        idx = SymbolIndex(repo_path=tmp_path)
        idx.build()

        defs = idx.find_definitions("Database")
        assert len(defs) >= 1
        db_def = [d for d in defs if d.filepath == "database.py"]
        assert len(db_def) == 1

    def test_resolves_package_import(self, tmp_path: Path):
        """Imports from packages should resolve to the correct __init__.py."""
        pkg = tmp_path / "mypackage"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("from .core import Engine\n")
        (pkg / "core.py").write_text("class Engine:\n" "    pass\n")
        (tmp_path / "main.py").write_text("from mypackage import Engine\n")
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "HEAD").write_text("abc123\n")

        idx = SymbolIndex(repo_path=tmp_path)
        idx.build()

        defs = idx.find_definitions("Engine")
        assert len(defs) >= 1

    def test_context_with_file_hint(self, tmp_path: Path):
        """file_hint parameter disambiguates when symbol exists in multiple files."""
        (tmp_path / "app.py").write_text("class Config:\n" "    DEBUG = True\n")
        (tmp_path / "settings.py").write_text("class Config:\n" "    DEBUG = False\n")
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "HEAD").write_text("abc123\n")

        idx = SymbolIndex(repo_path=tmp_path)
        idx.build()

        # Without file_hint: ambiguous (both files have Config)
        ctx = idx.get_context("Config")
        assert ctx["ambiguous"] is True
        assert len(ctx["definitions"]) == 2

        # With file_hint: resolves to the specific one
        ctx = idx.get_context("Config", file_hint="app.py")
        assert ctx.get("ambiguous") is not True
        assert ctx["definitions"][0]["file"] == "app.py"

        ctx = idx.get_context("Config", file_hint="settings.py")
        assert ctx.get("ambiguous") is not True
        assert ctx["definitions"][0]["file"] == "settings.py"

    def test_file_hint_no_match_falls_back_to_ambiguous(self, tmp_path: Path):
        """If file_hint doesn't match, treat as ambiguous."""
        (tmp_path / "app.py").write_text("class Config:\n    pass\n")
        (tmp_path / "settings.py").write_text("class Config:\n    pass\n")
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "HEAD").write_text("abc123\n")

        idx = SymbolIndex(repo_path=tmp_path)
        idx.build()

        ctx = idx.get_context("Config", file_hint="nonexistent.py")
        assert ctx["ambiguous"] is True
        assert len(ctx["definitions"]) == 2


# ---------------------------------------------------------------------------
# Test: git diff parsing helpers
# ---------------------------------------------------------------------------


class TestParseGitDiff:
    def test_parses_single_file_single_hunk(self):
        from shared.code_graph import _parse_git_diff

        diff = (
            "diff --git a/app.py b/app.py\n"
            "index abc123..def456 100644\n"
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "@@ -0,0 +10,5 @@\n"
        )
        result = _parse_git_diff(diff)
        assert len(result) == 1
        assert result[0]["file"] == "app.py"
        assert result[0]["ranges"] == [(10, 14)]

    def test_parses_multiple_files(self):
        from shared.code_graph import _parse_git_diff

        diff = (
            "diff --git a/app.py b/app.py\n"
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "@@ -1,0 +5,3 @@\n"
            "diff --git a/db.py b/db.py\n"
            "--- a/db.py\n"
            "+++ b/db.py\n"
            "@@ -10,0 +20,1 @@\n"
        )
        result = _parse_git_diff(diff)
        assert len(result) == 2
        assert result[0]["file"] == "app.py"
        assert result[1]["file"] == "db.py"

    def test_parses_multiple_hunks_same_file(self):
        from shared.code_graph import _parse_git_diff

        diff = (
            "diff --git a/app.py b/app.py\n"
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "@@ -0,0 +10,3 @@\n"
            "@@ -30,0 +45,5 @@\n"
        )
        result = _parse_git_diff(diff)
        assert len(result) == 1
        assert result[0]["file"] == "app.py"
        assert result[0]["ranges"] == [(10, 12), (45, 49)]

    def test_empty_diff(self):
        from shared.code_graph import _parse_git_diff

        result = _parse_git_diff("")
        assert result == []

    def test_single_line_hunk(self):
        from shared.code_graph import _parse_git_diff

        diff = (
            "diff --git a/app.py b/app.py\n"
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "@@ -0,0 +7 @@\n"
        )
        result = _parse_git_diff(diff)
        assert result[0]["ranges"] == [(7, 7)]


class TestGetSymbolsInRange:
    def test_returns_empty_for_nonexistent_file(self, symbol_index):
        from shared.code_graph import _get_symbols_in_range, get_surreal

        db = get_surreal()
        symbols = _get_symbols_in_range(db, "nonexistent.py", 1, 10)
        assert symbols == []


class TestSummarizeChanges:
    def test_empty_changes(self):
        from shared.code_graph import _summarize_changes

        summary = _summarize_changes([])
        assert "No symbol" in summary

    def test_with_changes(self):
        from shared.code_graph import _summarize_changes

        changed = [
            {
                "file": "app.py",
                "affected_symbols": [
                    {"name": "run", "line": 10, "end_line": 15, "kind": "method"}
                ],
                "risk_level": "low",
            }
        ]
        summary = _summarize_changes(changed)
        assert "app.py" in summary or "1 file" in summary

    def test_high_risk_mentioned(self):
        from shared.code_graph import _summarize_changes

        changed = [
            {
                "file": "app.py",
                "affected_symbols": [{"name": "run", "line": 10}],
                "risk_level": "high",
            }
        ]
        summary = _summarize_changes(changed)
        assert "high risk" in summary


class TestOverallRisk:
    def test_all_low(self):
        from shared.code_graph import _overall_risk

        changed = [{"risk_level": "low"}, {"risk_level": "low"}]
        assert _overall_risk(changed) == "low"

    def test_one_medium(self):
        from shared.code_graph import _overall_risk

        changed = [{"risk_level": "low"}, {"risk_level": "medium"}]
        assert _overall_risk(changed) == "medium"

    def test_one_high(self):
        from shared.code_graph import _overall_risk

        changed = [
            {"risk_level": "low"},
            {"risk_level": "medium"},
            {"risk_level": "high"},
        ]
        assert _overall_risk(changed) == "high"


# ---------------------------------------------------------------------------
# Test: detect_changes_from_diff
# ---------------------------------------------------------------------------


class TestDetectChangesFromDiff:
    def test_no_changes_when_clean(self, symbol_index):
        """When git diff produces no output, return empty changed_files."""
        import subprocess

        with patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            ),
        ):
            result = symbol_index.detect_changes_from_diff(scope="staged")
            assert result["changed_files"] == []
            assert "summary" in result
            assert result["risk_level"] == "low"

    def test_git_error_returns_error(self, symbol_index):
        import subprocess

        with patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=128, stdout="", stderr="fatal: not a git repo"
            ),
        ):
            result = symbol_index.detect_changes_from_diff(scope="staged")
            assert "error" in result

    def test_subprocess_exception_returns_error(self, symbol_index):
        with patch(
            "subprocess.run",
            side_effect=FileNotFoundError("git not found"),
        ):
            result = symbol_index.detect_changes_from_diff(scope="staged")
            assert "error" in result


# ---------------------------------------------------------------------------
# Test: trace_flow
# ---------------------------------------------------------------------------


class TestTraceFlow:
    def test_returns_structure(self, symbol_index):
        result = symbol_index.trace_flow("Application")
        assert "entry_point" in result
        assert result["entry_point"] == "Application"
        assert "entry_definition" in result
        assert "steps" in result
        assert "call_chain" in result
        assert "total_steps" in result
        assert isinstance(result["total_steps"], int)
        assert "max_depth_reached" in result

    def test_unknown_symbol_returns_error(self, symbol_index):
        result = symbol_index.trace_flow("nonexistent_function")
        assert "error" in result

    def test_with_file_hint(self, symbol_index):
        result = symbol_index.trace_flow("Application", file_hint="app.py")
        assert "error" not in result
        assert result["entry_definition"]["file"] == "app.py"

    def test_respects_max_depth(self, symbol_index):
        result = symbol_index.trace_flow("Application", max_depth=1)
        for step in result["steps"]:
            assert step["depth"] <= 1

    def test_max_depth_clamped(self, symbol_index):
        result = symbol_index.trace_flow("Application", max_depth=999)
        assert "error" not in result  # clamped, not rejected

    def test_call_chain_is_nested(self, symbol_index):
        result = symbol_index.trace_flow("Application")
        chain = result["call_chain"]
        assert "name" in chain
        assert "callees" in chain
        assert isinstance(chain["callees"], list)
