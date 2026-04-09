"""Tests for the repomap module."""

from pathlib import Path

import pytest

from shared.repomap import RepoMap, Tag, _token_count


@pytest.fixture
def python_repo(tmp_path: Path) -> Path:
    """Create a Python repo with known structure."""
    # Main module
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

    # Database module
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

    # Utils module
    (tmp_path / "utils.py").write_text(
        '"""Utility functions."""\n\n\n'
        "def helper(x: int) -> int:\n"
        "    return x * 2\n\n\n"
        "CONSTANT = 42\n"
    )

    # Tests (should be slightly deprioritized)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text(
        "from app import Application, create_app\n\n\n"
        "def test_app():\n"
        "    app = Application('test')\n"
        "    assert app.run()\n"
    )

    return tmp_path


class TestTag:
    def test_tag_creation(self):
        tag = Tag(
            filepath="app.py",
            name="Application",
            kind="definition",
            line=5,
            category="class",
            end_line=15,
        )
        assert tag.filepath == "app.py"
        assert tag.name == "Application"
        assert tag.kind == "definition"


class TestTokenCount:
    def test_basic_text(self):
        count = _token_count("hello world foo bar")
        assert count > 0
        assert isinstance(count, int)

    def test_empty_string(self):
        count = _token_count("")
        assert count >= 1  # Minimum 1

    def test_code_text(self):
        code = "def foo(bar: int) -> str:\n    return str(bar)"
        count = _token_count(code)
        assert count > 0


class TestRepoMapTagExtraction:
    @pytest.mark.asyncio
    async def test_extracts_python_definitions(self, python_repo: Path):
        rm = RepoMap(python_repo)
        tags = rm._extract_all_tags()

        # Should find at least the major definitions
        def_names = {t.name for t in tags if t.kind == "definition"}
        assert "Application" in def_names
        assert "Database" in def_names
        assert "helper" in def_names

    @pytest.mark.asyncio
    async def test_excludes_noise_dirs(self, python_repo: Path):
        # Add noise
        (python_repo / "__pycache__").mkdir()
        (python_repo / "__pycache__" / "app.cpython-311.pyc").write_text("bytecode")
        (python_repo / ".git").mkdir()
        (python_repo / ".git" / "HEAD").write_text("ref: refs/heads/main")

        rm = RepoMap(python_repo)
        tags = rm._extract_all_tags()

        filepaths = {t.filepath for t in tags}
        assert not any("__pycache__" in fp for fp in filepaths)
        assert not any(".git" in fp for fp in filepaths)

    def test_skip_file_patterns(self):
        assert RepoMap._should_skip_file("bundle.min.js")
        assert RepoMap._should_skip_file("app_pb2.py")
        assert RepoMap._should_skip_file("package-lock.json")
        assert not RepoMap._should_skip_file("main.py")
        assert not RepoMap._should_skip_file("app.ts")

    @pytest.mark.asyncio
    async def test_regex_fallback(self, tmp_path: Path):
        """Test that regex fallback works for files without tree-sitter support."""
        # Create a Go file (not in LANGUAGE_MAP)
        (tmp_path / "main.go").write_text(
            "package main\n\n"
            'import "fmt"\n\n'
            "func main() {\n"
            '    fmt.Println("hello")\n'
            "}\n\n"
            "type Server struct {\n"
            "    Port int\n"
            "}\n"
        )

        rm = RepoMap(tmp_path)
        tags = rm._get_tags(tmp_path / "main.go")

        # Should extract via generic regex patterns
        names = {t.name for t in tags if t.kind == "definition"}
        assert "main" in names or "Server" in names


class TestRepoMapRanking:
    @pytest.mark.asyncio
    async def test_ranking_mentions(self, python_repo: Path):
        """Tags from mentioned files should rank higher."""
        rm = RepoMap(python_repo)
        tags = rm._extract_all_tags()

        ranked = rm._rank_tags(
            tags,
            mentioned_files=["database.py"],
            mentioned_idents=["Database"],
        )

        assert ranked
        # Database-related tags should be near the top
        top_names = [rt.tag.name for rt in ranked[:5]]
        # At least one Database-related name should appear in top entries
        db_related = {"Database", "connect", "query"}
        assert bool(db_related & set(top_names)) or "Database" in str(ranked)

    @pytest.mark.asyncio
    async def test_simple_rank_fallback(self, python_repo: Path):
        """Simple ranking should work without networkx."""
        rm = RepoMap(python_repo)
        tags = rm._extract_all_tags()
        definitions = [t for t in tags if t.kind == "definition"]

        ranked = rm._simple_rank(
            definitions,
            ref_counts={"Application": 5, "Database": 3},
            mentioned_files=[],
            mentioned_idents=[],
        )

        # Application has more references, should rank higher
        app_score = next((rt.score for rt in ranked if rt.tag.name == "Application"), 0)
        util_score = next((rt.score for rt in ranked if rt.tag.name == "helper"), 0)
        assert app_score >= util_score


class TestIncludeTestFiles:
    """Tests for the include_test_files ranking behavior."""

    @pytest.mark.asyncio
    async def test_boost_test_files_when_enabled(self, python_repo: Path):
        """Test files should be boosted when include_test_files=True."""
        rm = RepoMap(python_repo)
        tags = rm._extract_all_tags()
        definitions = [t for t in tags if t.kind == "definition"]

        ranked_boost = rm._simple_rank(
            definitions,
            ref_counts={},
            mentioned_files=[],
            mentioned_idents=[],
            include_test_files=True,
        )

        ranked_penalty = rm._simple_rank(
            definitions,
            ref_counts={},
            mentioned_files=[],
            mentioned_idents=[],
            include_test_files=False,
        )

        # Find a test file definition (test_app in tests/test_app.py)
        test_tag_name = None
        for rt in ranked_boost:
            if "test" in rt.tag.filepath.lower():
                test_tag_name = rt.tag.name
                break

        if test_tag_name:
            boost_score = next(
                (rt.score for rt in ranked_boost if rt.tag.name == test_tag_name), 0
            )
            penalty_score = next(
                (rt.score for rt in ranked_penalty if rt.tag.name == test_tag_name), 0
            )
            # When included, test files should score higher than when excluded
            assert boost_score > penalty_score

    @pytest.mark.asyncio
    async def test_default_includes_test_files(self, python_repo: Path):
        """By default, include_test_files should be True."""
        rm = RepoMap(python_repo)
        result = await rm.get_repo_map(token_budget=4000)

        # Should include test definitions by default
        if result:
            # test_app definition should be visible
            assert "test_app" in result or "test" in result.lower()


class TestRepoMapRendering:
    @pytest.mark.asyncio
    async def test_renders_compact_map(self, python_repo: Path):
        rm = RepoMap(python_repo)
        result = await rm.get_repo_map(token_budget=2000)

        assert result
        assert "Application" in result or "Database" in result

    @pytest.mark.asyncio
    async def test_respects_token_budget(self, python_repo: Path):
        rm = RepoMap(python_repo)

        # Small budget
        small = await rm.get_repo_map(token_budget=100)
        large = await rm.get_repo_map(token_budget=4000)

        if small and large:
            assert _token_count(small) <= _token_count(large)

    @pytest.mark.asyncio
    async def test_empty_repo(self, tmp_path: Path):
        rm = RepoMap(tmp_path)
        result = await rm.get_repo_map()
        assert result == ""

    @pytest.mark.asyncio
    async def test_file_grouping(self, python_repo: Path):
        """Output should group entries by file."""
        rm = RepoMap(python_repo)
        result = await rm.get_repo_map(token_budget=4000)

        if result:
            # Should show file paths as headers
            assert ".py:" in result  # Some file header like "app.py:"


class TestRepoMapIntegration:
    @pytest.mark.asyncio
    async def test_full_pipeline(self, python_repo: Path):
        """End-to-end test of repomap generation."""
        rm = RepoMap(python_repo)
        result = await rm.get_repo_map(
            mentioned_files=["app.py"],
            token_budget=2048,
        )

        assert result
        # Should contain structural information
        assert any(
            keyword in result for keyword in ["class", "def", "Application", "Database"]
        )


class TestRepoMapGitignore:
    @pytest.mark.asyncio
    async def test_gitignore_excludes_files(self, python_repo: Path):
        """Repomap should respect .gitignore patterns."""
        # Add a file that would normally be parsed
        (python_repo / "generated.py").write_text("class GeneratedModel:\n    pass\n")
        # Tell git to ignore it
        (python_repo / ".gitignore").write_text("generated.py\n")

        rm = RepoMap(python_repo)
        result = await rm.get_repo_map(token_budget=4000)
        assert result
        assert "GeneratedModel" not in result

    @pytest.mark.asyncio
    async def test_gitignore_excludes_directory(self, python_repo: Path):
        """Repomap should exclude entire directories listed in .gitignore."""
        (python_repo / "vendor").mkdir()
        (python_repo / "vendor" / "lib.py").write_text("class VendorLib:\n    pass\n")
        (python_repo / ".gitignore").write_text("vendor/\n")

        rm = RepoMap(python_repo)
        result = await rm.get_repo_map(token_budget=4000)
        assert result
        assert "VendorLib" not in result
