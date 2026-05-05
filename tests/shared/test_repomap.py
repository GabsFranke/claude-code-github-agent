"""Tests for the repomap module."""

from pathlib import Path

import pytest

from shared.repomap import RepoMap, Tag


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


class TestRepoMapTagExtraction:
    def test_extracts_python_definitions(self, python_repo: Path):
        rm = RepoMap(python_repo)
        tags = rm._extract_all_tags()

        # Should find at least the major definitions
        def_names = {t.name for t in tags if t.kind == "definition"}
        assert "Application" in def_names
        assert "Database" in def_names
        assert "helper" in def_names

    def test_excludes_noise_dirs(self, python_repo: Path):
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

    def test_skip_file_patterns(self, tmp_path: Path):
        """walk_source_files should skip excluded file patterns."""
        from shared.file_tree import walk_source_files

        # Create files that should be skipped
        (tmp_path / "bundle.min.js").write_text("// minified", encoding="utf-8")
        (tmp_path / "app_pb2.py").write_text("# pb2", encoding="utf-8")
        (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
        # Create files that should be kept
        (tmp_path / "main.py").write_text("pass", encoding="utf-8")
        (tmp_path / "app.ts").write_text("// app", encoding="utf-8")

        found = {f.name for f in walk_source_files(tmp_path)}
        assert "main.py" in found
        assert "app.ts" in found
        assert "bundle.min.js" not in found
        assert "app_pb2.py" not in found
        assert "package-lock.json" not in found

    def test_regex_fallback(self, tmp_path: Path):
        """Test that regex fallback works for files without tree-sitter support."""
        # Create a Go file (tests regex fallback when tree-sitter-go is not installed)
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


class TestRepoMapGitignore:
    def test_gitignore_excludes_files(self, python_repo: Path):
        """Tag extraction should respect .gitignore patterns."""
        # Add a file that would normally be parsed
        (python_repo / "generated.py").write_text("class GeneratedModel:\n    pass\n")
        # Tell git to ignore it
        (python_repo / ".gitignore").write_text("generated.py\n")

        rm = RepoMap(python_repo)
        tags = rm.extract_tags()
        names = {t.name for t in tags}
        assert "GeneratedModel" not in names

    def test_gitignore_excludes_directory(self, python_repo: Path):
        """Tag extraction should exclude entire directories listed in .gitignore."""
        (python_repo / "vendor").mkdir()
        (python_repo / "vendor" / "lib.py").write_text("class VendorLib:\n    pass\n")
        (python_repo / ".gitignore").write_text("vendor/\n")

        rm = RepoMap(python_repo)
        tags = rm.extract_tags()
        names = {t.name for t in tags}
        assert "VendorLib" not in names
