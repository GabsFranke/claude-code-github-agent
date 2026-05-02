"""Unit tests for codebase tools (tools.py).

Tests find_definitions, find_references, search_codebase, and read_file_summary
using a temporary Python repo fixture.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mcp_servers.codebase_tools.tools import (
    _resolve_and_validate,
    detect_changes,
    find_definitions,
    find_references,
    init_repo,
    read_file_summary,
    search_codebase,
    trace_flow,
)
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
        patch("shared.code_graph.is_initialized", return_value=True),
        patch("shared.code_graph.get_surreal", return_value=fake_db),
        patch("shared.code_graph.apply_schema"),
        patch("mcp_servers.codebase_tools.tools.init_surrealdb"),
        patch("mcp_servers.codebase_tools.tools.get_surreal", return_value=fake_db),
    ):
        yield fake_db


@pytest.fixture
def python_repo(tmp_path: Path) -> Path:
    """Create a small Python repo with known structure for testing."""
    # app.py — main module
    (tmp_path / "app.py").write_text(
        '''"""Main application module."""

import os
import sys
from database import Database


class Application:
    """The main application class."""

    def __init__(self, name: str):
        self.name = name
        self.db = Database()

    def run(self) -> None:
        """Run the application."""
        db = Database()
        db.connect()

    def shutdown(self):
        """Shutdown the application."""
        pass


def helper():
    """A helper function."""
    return Application("test")
''',
        encoding="utf-8",
    )

    # database.py — dependency
    (tmp_path / "database.py").write_text(
        '''"""Database module."""

import logging


logger = logging.getLogger(__name__)


class Database:
    """Database connection handler."""

    def __init__(self, url: str = "localhost"):
        self.url = url

    def connect(self) -> bool:
        """Connect to the database."""
        return True

    def query(self, sql: str):
        """Execute a query."""
        pass


def create_pool(size: int = 5):
    """Create a connection pool."""
    return [Database() for _ in range(size)]
''',
        encoding="utf-8",
    )

    # utils.py — utility functions
    (tmp_path / "utils.py").write_text(
        '''"""Utility functions."""


def format_name(first: str, last: str) -> str:
    """Format a full name."""
    return f"{first} {last}"


def parse_config(path: str) -> dict:
    """Parse a configuration file."""
    return {}
''',
        encoding="utf-8",
    )

    # README — non-Python file
    (tmp_path / "README.md").write_text("# Test Repo\n", encoding="utf-8")

    return tmp_path


@pytest.fixture
def initialized_repo(python_repo: Path):
    """Initialize codebase tools with the test repo."""
    # Reset module state before init
    from mcp_servers.codebase_tools import tools

    tools._repo_path = None
    tools._symbol_index = None
    init_repo(str(python_repo))
    return python_repo


# ---------------------------------------------------------------------------
# Test: init_repo
# ---------------------------------------------------------------------------


class TestInitRepo:
    def test_initializes_with_valid_path(self, python_repo: Path):
        init_repo(str(python_repo))
        from mcp_servers.codebase_tools import tools

        assert tools._repo_path == python_repo.resolve()
        assert tools._symbol_index is not None
        assert tools._symbol_index._built

    def test_raises_on_invalid_path(self):
        with pytest.raises(ValueError, match="does not exist"):
            init_repo("/nonexistent/path/that/does/not/exist")

    def test_build_completes_successfully(self, python_repo: Path):
        """init_repo should build SymbolIndex and mark it as built."""
        init_repo(str(python_repo))
        from mcp_servers.codebase_tools import tools

        assert tools._symbol_index is not None
        assert tools._symbol_index._built


# ---------------------------------------------------------------------------
# Test: find_definitions
# ---------------------------------------------------------------------------


class TestFindDefinitions:
    def test_finds_class_definition(self, initialized_repo: Path):
        results = find_definitions("Application")
        assert len(results) >= 1

        match = results[0]
        assert match["file"] == "app.py"
        assert match["kind"] == "class"
        assert "class Application" in match["signature"]
        assert match["line"] > 0

    def test_finds_function_definition(self, initialized_repo: Path):
        results = find_definitions("helper")
        assert len(results) >= 1

        match = results[0]
        assert match["file"] == "app.py"
        assert match["kind"] == "function"
        assert "def helper" in match["signature"]

    def test_finds_in_multiple_files(self, initialized_repo: Path):
        results = find_definitions("Database")
        # Database class is defined in database.py
        assert any(r["file"] == "database.py" for r in results)

    def test_returns_empty_for_unknown_symbol(self, initialized_repo: Path):
        results = find_definitions("NonexistentSymbol")
        assert results == []

    def test_includes_end_line(self, initialized_repo: Path):
        results = find_definitions("Application")
        assert len(results) >= 1
        match = results[0]
        assert match["end_line"] >= match["line"]


# ---------------------------------------------------------------------------
# Test: find_references
# ---------------------------------------------------------------------------


class TestFindReferences:
    def test_finds_cross_file_references(self, initialized_repo: Path):
        """Database is used in app.py (import + instantiation)."""
        results = find_references("Database")
        app_refs = [r for r in results if r["file"] == "app.py"]
        assert len(app_refs) >= 1

    def test_excludes_definition_lines(self, initialized_repo: Path):
        """Definition lines should not appear in references."""
        results = find_references("Database")
        # Database is defined in database.py — that line should be excluded
        def_lines = {r["line"] for r in results if r["file"] == "database.py"}
        # The class definition line should NOT be in references
        defs = find_definitions("Database")
        for d in defs:
            if d["file"] == "database.py":
                assert d["line"] not in def_lines

    def test_returns_context_line(self, initialized_repo: Path):
        """If references are found, they should have context lines."""
        results = find_references("Database")
        for r in results:
            assert r["context"]  # Should have non-empty context
            assert r["line"] > 0

    def test_returns_empty_for_unknown_symbol(self, initialized_repo: Path):
        results = find_references("NonexistentSymbol")
        assert results == []

    def test_deduplicates_same_line(self, initialized_repo: Path):
        """No two results should have the same (file, line) pair."""
        results = find_references("Database")
        seen = set()
        for r in results:
            key = (r["file"], r["line"])
            assert key not in seen, f"Duplicate reference at {key}"
            seen.add(key)


# ---------------------------------------------------------------------------
# Test: search_codebase
# ---------------------------------------------------------------------------


class TestSearchCodebase:
    def test_finds_pattern(self, initialized_repo: Path):
        results = search_codebase("def helper")
        assert len(results) >= 1
        assert any("helper" in r["match"] for r in results)

    def test_respects_max_results(self, initialized_repo: Path):
        results = search_codebase("import", max_results=2)
        assert len(results) <= 2

    def test_caps_max_results_at_100(self, initialized_repo: Path):
        results = search_codebase("import", max_results=999)
        # Should not exceed 100
        assert len(results) <= 100

    def test_returns_structured_output(self, initialized_repo: Path):
        results = search_codebase("class Application")
        assert len(results) >= 1

        match = results[0]
        assert "file" in match
        assert "line" in match
        assert "match" in match
        assert "context" in match
        assert match["line"] > 0

    def test_file_type_filter(self, initialized_repo: Path):
        results = search_codebase("Application", file_type="python")
        # All results should be from Python files
        for r in results:
            assert r["file"].endswith(".py")

    def test_returns_empty_for_no_matches(self, initialized_repo: Path):
        results = search_codebase("zzz_nonexistent_pattern_xyz")
        assert results == []

    def test_python_fallback_works(self, python_repo: Path):
        """Test the Python regex fallback path."""
        init_repo(str(python_repo))
        from mcp_servers.codebase_tools import tools

        # Temporarily hide rg to force fallback
        original_which = tools.shutil.which
        tools.shutil.which = lambda _: None  # type: ignore[attr-defined]

        try:
            results = search_codebase("def helper")
            assert len(results) >= 1
        finally:
            tools.shutil.which = original_which  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Test: search_codebase — semantic and hybrid modes
# ---------------------------------------------------------------------------


class MockEmbeddingResponse:
    """Fake Gemini embedding response."""

    embeddings: list = []


class MockEmbedding:
    """Fake embedding values."""

    def __init__(self, values):
        self.values = values


@pytest.fixture
def _populate_symbols(_mock_surrealdb: FakeSurrealDB):
    """Populate FakeSurrealDB with symbol records for semantic search tests."""
    symbols = [
        {
            "id": "symbol:1",
            "name": "Database",
            "kind": "definition",
            "filepath": "database.py",
            "line": 6,
            "end_line": 17,
            "language": "python",
            "content": "class Database:\n    ...",
            "embedding": [0.1] * 1024,
        },
        {
            "id": "symbol:2",
            "name": "Application",
            "kind": "definition",
            "filepath": "app.py",
            "line": 7,
            "end_line": 20,
            "language": "python",
            "content": "class Application:\n    ...",
            "embedding": [0.2] * 1024,
        },
        {
            "id": "symbol:3",
            "name": "create_pool",
            "kind": "definition",
            "filepath": "database.py",
            "line": 21,
            "end_line": 23,
            "language": "python",
            "content": "def create_pool(size):\n    ...",
            "embedding": [0.3] * 1024,
        },
    ]
    _mock_surrealdb._ensure_table("symbol").extend(symbols)


class TestSemanticSearch:
    def test_semantic_search_finds_results(
        self, initialized_repo: Path, _populate_symbols: None, monkeypatch
    ):
        """Semantic search should return results from SurrealDB vector search."""
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        mock_values = [0.15] * 1024
        mock_embed = MockEmbedding(mock_values)
        mock_response = MockEmbeddingResponse()
        mock_response.embeddings = [mock_embed]

        mock_genai_client = MagicMock()
        mock_genai_client.models.embed_content.return_value = mock_response

        with patch("google.genai.Client", return_value=mock_genai_client):
            results = search_codebase(
                "database connection handler",
                search_type="semantic",
                max_results=5,
            )
            assert isinstance(results, list)
            assert len(results) >= 1
            assert results[0]["name"] == "Database"
            assert results[0]["kind"] == "definition"
            assert "score" in results[0]

    def test_semantic_search_filters_by_file_type(
        self, initialized_repo: Path, _populate_symbols: None, monkeypatch
    ):
        """When file_type is 'python', results should be Python files only."""
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        mock_values = [0.15] * 1024
        mock_embed = MockEmbedding(mock_values)
        mock_response = MockEmbeddingResponse()
        mock_response.embeddings = [mock_embed]

        mock_genai_client = MagicMock()
        mock_genai_client.models.embed_content.return_value = mock_response

        with patch("google.genai.Client", return_value=mock_genai_client):
            results = search_codebase(
                "database",
                file_type="python",
                search_type="semantic",
            )
            for r in results:
                assert r["file"].endswith(".py")

    def test_semantic_search_filters_by_kind(
        self, initialized_repo: Path, _populate_symbols: None, monkeypatch
    ):
        """When kind_filter is set, only matching symbol kinds are returned."""
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        mock_values = [0.15] * 1024
        mock_embed = MockEmbedding(mock_values)
        mock_response = MockEmbeddingResponse()
        mock_response.embeddings = [mock_embed]

        mock_genai_client = MagicMock()
        mock_genai_client.models.embed_content.return_value = mock_response

        with patch("google.genai.Client", return_value=mock_genai_client):
            results = search_codebase(
                "database connection",
                search_type="semantic",
                kind_filter="class",
            )
            for r in results:
                assert r["kind"] == "class"

    def test_semantic_search_falls_back_on_missing_api_key(
        self, initialized_repo: Path, monkeypatch
    ):
        """When GEMINI_API_KEY is not set, fall back to text search."""
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)

        results = search_codebase(
            "def helper",
            search_type="semantic",
        )
        assert isinstance(results, list)


class TestHybridSearch:
    def test_hybrid_search_returns_merged_results(
        self, initialized_repo: Path, _populate_symbols: None, monkeypatch
    ):
        """Hybrid search should run both modes and merge results."""
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        mock_values = [0.15] * 1024
        mock_embed = MockEmbedding(mock_values)
        mock_response = MockEmbeddingResponse()
        mock_response.embeddings = [mock_embed]

        mock_genai_client = MagicMock()
        mock_genai_client.models.embed_content.return_value = mock_response

        with patch("google.genai.Client", return_value=mock_genai_client):
            results = search_codebase(
                "Database",
                search_type="hybrid",
                max_results=10,
            )
            assert isinstance(results, list)
            # Should have at least the semantic matches
            assert len(results) >= 1
            # Each result should have a "source" field
            for r in results:
                assert "source" in r
                assert r["source"] in ("semantic", "text")
            # At least one semantic result
            assert any(r["source"] == "semantic" for r in results)

    def test_hybrid_deduplicates_by_file_and_line(
        self, initialized_repo: Path, _populate_symbols: None, monkeypatch
    ):
        """Results with same (file, line) should be deduplicated."""
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        mock_values = [0.15] * 1024
        mock_embed = MockEmbedding(mock_values)
        mock_response = MockEmbeddingResponse()
        mock_response.embeddings = [mock_embed]

        mock_genai_client = MagicMock()
        mock_genai_client.models.embed_content.return_value = mock_response

        with patch("google.genai.Client", return_value=mock_genai_client):
            results = search_codebase(
                "class Database",
                search_type="hybrid",
            )
            seen: set[tuple[str, int]] = set()
            for r in results:
                key = (r["file"], r["line"])
                assert key not in seen, f"Duplicate result at {key}"
                seen.add(key)

    def test_hybrid_respects_max_results(
        self, initialized_repo: Path, _populate_symbols: None, monkeypatch
    ):
        """Hybrid search should respect max_results cap."""
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        mock_values = [0.15] * 1024
        mock_embed = MockEmbedding(mock_values)
        mock_response = MockEmbeddingResponse()
        mock_response.embeddings = [mock_embed]

        mock_genai_client = MagicMock()
        mock_genai_client.models.embed_content.return_value = mock_response

        with patch("google.genai.Client", return_value=mock_genai_client):
            results = search_codebase(
                "import",
                search_type="hybrid",
                max_results=3,
            )
            assert len(results) <= 3


# ---------------------------------------------------------------------------
# Test: read_file_summary
# ---------------------------------------------------------------------------


class TestReadFileSummary:
    def test_extracts_docstring(self, initialized_repo: Path):
        result = read_file_summary("app.py")
        assert result["docstring"] is not None
        assert "Main application module" in result["docstring"]

    def test_extracts_imports(self, initialized_repo: Path):
        result = read_file_summary("app.py")
        assert len(result["imports"]) > 0
        # Should include import statements
        import_text = " ".join(result["imports"])
        assert "os" in import_text or "database" in import_text.lower()

    def test_extracts_signatures(self, initialized_repo: Path):
        result = read_file_summary("app.py")
        assert len(result["signatures"]) > 0

        sig_names = [s["name"] for s in result["signatures"]]
        assert "Application" in sig_names
        assert "helper" in sig_names

    def test_signature_has_correct_fields(self, initialized_repo: Path):
        result = read_file_summary("app.py")
        for sig in result["signatures"]:
            assert "name" in sig
            assert "kind" in sig
            assert "line" in sig
            assert "signature" in sig
            assert "end_line" in sig
            assert sig["kind"] in ("class", "function")

    def test_skips_function_bodies(self, initialized_repo: Path):
        result = read_file_summary("app.py")
        # Signatures are just the def/class lines, not the full body.
        # Total lines should be much more than the number of signatures.
        assert result["total_lines"] > len(result["signatures"])

    def test_returns_total_lines(self, initialized_repo: Path):
        result = read_file_summary("app.py")
        assert result["total_lines"] > 0
        assert result["file"] == "app.py"

    def test_raises_for_nonexistent_file(self, initialized_repo: Path):
        with pytest.raises(FileNotFoundError):
            read_file_summary("nonexistent.py")

    def test_regex_fallback_for_unknown_language(self, initialized_repo: Path):
        # Create a non-Python file
        (initialized_repo / "config.yaml").write_text(
            "name: test\nversion: 1.0\n", encoding="utf-8"
        )
        result = read_file_summary("config.yaml")
        assert result["file"] == "config.yaml"
        assert result["language"] == "unknown"

    def test_respects_max_lines(self, initialized_repo: Path):
        result = read_file_summary("app.py", max_lines=1)
        # Should cap signatures at 1
        assert len(result["signatures"]) <= 1


# ---------------------------------------------------------------------------
# Test: path validation
# ---------------------------------------------------------------------------


class TestPathValidation:
    def test_rejects_traversal_attack(self, initialized_repo: Path):
        with pytest.raises(ValueError, match="outside repository"):
            _resolve_and_validate("../../etc/passwd")

    def test_rejects_absolute_path_outside_repo(self, initialized_repo: Path):
        with pytest.raises(ValueError, match="outside repository"):
            _resolve_and_validate("/etc/passwd")

    def test_allows_valid_relative_path(self, initialized_repo: Path):
        result = _resolve_and_validate("app.py")
        assert result.name == "app.py"

    def test_allows_nested_relative_path(self, initialized_repo: Path):
        result = _resolve_and_validate("some/nested/file.py")
        assert result.name == "file.py"
        assert "nested" in str(result)


# ---------------------------------------------------------------------------
# Test: detect_changes
# ---------------------------------------------------------------------------


class TestDetectChanges:
    def test_returns_changed_files_structure(self, initialized_repo: Path):
        import subprocess

        with patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            ),
        ):
            result = detect_changes(scope="staged")
        assert "changed_files" in result
        assert "summary" in result
        assert "risk_level" in result
        assert isinstance(result["changed_files"], list)
        assert result["risk_level"] in ("low", "medium", "high")

    def test_unstaged_scope(self, initialized_repo: Path):
        import subprocess

        with patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            ),
        ):
            result = detect_changes(scope="unstaged")
        assert "changed_files" in result
        assert "summary" in result
        assert "risk_level" in result


# ---------------------------------------------------------------------------
# Test: trace_flow
# ---------------------------------------------------------------------------


class TestTraceFlow:
    def test_returns_structure(self, initialized_repo: Path):
        result = trace_flow("Application")
        assert "entry_point" in result
        assert "entry_definition" in result
        assert "steps" in result
        assert "call_chain" in result
        assert "total_steps" in result
        assert "max_depth_reached" in result
        assert isinstance(result["steps"], list)
        assert isinstance(result["call_chain"], dict)

    def test_unknown_entry_point(self, initialized_repo: Path):
        result = trace_flow("nonexistent_function")
        assert "error" in result

    def test_with_file_hint(self, initialized_repo: Path):
        result = trace_flow("Application", file_hint="app.py")
        assert "error" not in result
        assert "entry_definition" in result

    def test_respects_max_depth(self, initialized_repo: Path):
        result = trace_flow("Application", max_depth=1)
        assert "steps" in result
        for step in result["steps"]:
            assert step["depth"] <= 1
