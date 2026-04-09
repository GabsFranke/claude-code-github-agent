"""Tests for the file tree generation module."""

from pathlib import Path

import pytest

from shared.file_tree import (
    _load_ignore_spec,
    _should_exclude_dir,
    _should_exclude_file,
    generate_file_tree,
)


@pytest.fixture
def sample_repo(tmp_path: Path) -> Path:
    """Create a sample repo structure for testing."""
    # Standard structure
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "__init__.py").write_text("")
    (tmp_path / "src" / "main.py").write_text("def main(): pass")
    (tmp_path / "src" / "utils.py").write_text("def helper(): pass")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_main.py").write_text("def test_main(): pass")
    (tmp_path / "README.md").write_text("# Test")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='test'\n")

    # Noise directories (should be excluded)
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "main.cpython-311.pyc").write_text("bytecode")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "pkg").mkdir()
    (tmp_path / "node_modules" / "pkg" / "index.js").write_text("")

    # Noise files (should be excluded)
    (tmp_path / "package-lock.json").write_text("{}")
    (tmp_path / "bundle.min.js").write_text("minified")

    return tmp_path


class TestExcludeFilters:
    def test_exclude_common_dirs(self):
        assert _should_exclude_dir("node_modules")
        assert _should_exclude_dir("__pycache__")
        assert _should_exclude_dir(".git")
        assert _should_exclude_dir(".venv")
        assert _should_exclude_dir("dist")
        assert _should_exclude_dir("build")

    def test_include_source_dirs(self):
        assert not _should_exclude_dir("src")
        assert not _should_exclude_dir("tests")
        assert not _should_exclude_dir("lib")
        assert not _should_exclude_dir("my_package")

    def test_exclude_dotfiles_dirs(self):
        assert _should_exclude_dir(".idea")
        assert _should_exclude_dir(".vscode")
        assert _should_exclude_dir(".mypy_cache")

    def test_exclude_lock_files(self):
        assert _should_exclude_file("package-lock.json")
        assert _should_exclude_file("yarn.lock")
        assert _should_exclude_file("pnpm-lock.yaml")

    def test_exclude_minified_files(self):
        assert _should_exclude_file("app.min.js")
        assert _should_exclude_file("style.min.css")

    def test_include_source_files(self):
        assert not _should_exclude_file("main.py")
        assert not _should_exclude_file("index.ts")
        assert not _should_exclude_file("README.md")
        assert not _should_exclude_file("config.yaml")


class TestGenerateFileTree:
    def test_basic_tree(self, sample_repo: Path):
        tree = generate_file_tree(sample_repo)
        assert tree
        assert "src/" in tree
        assert "main.py" in tree
        assert "utils.py" in tree
        assert "tests/" in tree
        assert "README.md" in tree

    def test_excludes_noise_dirs(self, sample_repo: Path):
        tree = generate_file_tree(sample_repo)
        assert "__pycache__" not in tree
        assert "node_modules" not in tree
        assert ".git" not in tree

    def test_excludes_noise_files(self, sample_repo: Path):
        tree = generate_file_tree(sample_repo)
        assert "package-lock.json" not in tree
        assert "bundle.min.js" not in tree

    def test_includes_summary(self, sample_repo: Path):
        tree = generate_file_tree(sample_repo)
        assert "files total" in tree

    def test_max_depth(self, sample_repo: Path):
        # Create deeply nested structure
        deep = sample_repo / "a" / "b" / "c" / "d"
        deep.mkdir(parents=True)
        (deep / "deep.py").write_text("pass")

        tree = generate_file_tree(sample_repo, max_depth=2)
        assert "deep.py" not in tree  # Too deep to show

    def test_max_entries(self, sample_repo: Path):
        # Create many files
        for i in range(50):
            (sample_repo / f"file_{i:03d}.py").write_text("pass")

        tree = generate_file_tree(sample_repo, max_entries=10)
        assert "truncated" in tree

    def test_empty_dir(self, tmp_path: Path):
        empty = tmp_path / "empty_repo"
        empty.mkdir()
        tree = generate_file_tree(empty)
        # Should return just the root name with no entries
        assert empty.name in tree

    def test_nonexistent_path(self, tmp_path: Path):
        tree = generate_file_tree(tmp_path / "nonexistent")
        assert tree == ""

    def test_tree_connectors(self, sample_repo: Path):
        tree = generate_file_tree(sample_repo)
        # Should use tree-drawing characters
        assert "├──" in tree or "└──" in tree


class TestGitignoreSupport:
    def test_gitignore_excludes_files(self, tmp_path: Path):
        """Files matching .gitignore patterns should be excluded."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("pass")
        (tmp_path / "src" / "config.json").write_text("{}")
        (tmp_path / "src" / "secrets.env").write_text("KEY=xxx")
        (tmp_path / ".gitignore").write_text("*.env\nconfig.json\n")

        tree = generate_file_tree(tmp_path)
        assert "main.py" in tree
        assert "secrets.env" not in tree
        assert "config.json" not in tree

    def test_gitignore_excludes_dirs(self, tmp_path: Path):
        """Directories matching .gitignore patterns should be excluded."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("pass")
        (tmp_path / "output").mkdir()
        (tmp_path / "output" / "report.html").write_text("<html>")
        (tmp_path / "coverage").mkdir()
        (tmp_path / "coverage" / "index.html").write_text("<html>")
        (tmp_path / ".gitignore").write_text("output/\ncoverage/\n")

        tree = generate_file_tree(tmp_path)
        assert "main.py" in tree
        assert "output" not in tree
        assert "coverage" not in tree

    def test_ignore_file_also_respected(self, tmp_path: Path):
        """The .ignore file should work alongside .gitignore."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("pass")
        (tmp_path / "src" / "todo.md").write_text("TODO")
        (tmp_path / ".gitignore").write_text("*.log\n")
        (tmp_path / ".ignore").write_text("*.md\n")

        tree = generate_file_tree(tmp_path)
        assert "main.py" in tree
        assert "todo.md" not in tree

    def test_combined_hardcoded_and_gitignore(self, tmp_path: Path):
        """Both hardcoded exclusions and .gitignore should apply."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("pass")
        (tmp_path / "__pycache__").mkdir()
        (tmp_path / "__pycache__" / "main.pyc").write_text("bytecode")
        (tmp_path / "local_data").mkdir()
        (tmp_path / "local_data" / "data.csv").write_text("a,b")
        (tmp_path / ".gitignore").write_text("local_data/\n")

        tree = generate_file_tree(tmp_path)
        assert "main.py" in tree
        assert "__pycache__" not in tree  # Hardcoded exclusion
        assert "local_data" not in tree  # .gitignore exclusion

    def test_no_gitignore_still_works(self, tmp_path: Path):
        """Repos without .gitignore should still use hardcoded exclusions."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("pass")
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "pkg").mkdir()
        (tmp_path / "node_modules" / "pkg" / "index.js").write_text("")

        tree = generate_file_tree(tmp_path)
        assert "main.py" in tree
        assert "node_modules" not in tree

    def test_gitignore_negation(self, tmp_path: Path):
        """Negation patterns (!foo) should un-exclude previously excluded files."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "important.log").write_text("important")
        (tmp_path / "src" / "debug.log").write_text("debug")
        (tmp_path / ".gitignore").write_text("*.log\n!important.log\n")

        tree = generate_file_tree(tmp_path)
        assert "important.log" in tree
        assert "debug.log" not in tree

    def test_gitignore_wildcard_patterns(self, tmp_path: Path):
        """Wildcard patterns like docs/**/*.pdf should work."""
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "guide.pdf").write_text("pdf")
        (tmp_path / "docs" / "README.md").write_text("# Docs")
        (tmp_path / ".gitignore").write_text("docs/**/*.pdf\n")

        tree = generate_file_tree(tmp_path)
        assert "README.md" in tree
        assert "guide.pdf" not in tree


class TestLoadIgnoreSpec:
    def test_returns_none_for_no_ignore_files(self, tmp_path: Path):
        spec = _load_ignore_spec(tmp_path)
        assert spec is None

    def test_returns_spec_for_gitignore(self, tmp_path: Path):
        (tmp_path / ".gitignore").write_text("*.log\nbuild/\n")
        spec = _load_ignore_spec(tmp_path)
        assert spec is not None
        assert spec.match_file("debug.log")
        assert spec.match_file("build/")

    def test_combines_gitignore_and_ignore(self, tmp_path: Path):
        (tmp_path / ".gitignore").write_text("*.log\n")
        (tmp_path / ".ignore").write_text("*.tmp\n")
        spec = _load_ignore_spec(tmp_path)
        assert spec is not None
        assert spec.match_file("debug.log")
        assert spec.match_file("temp.tmp")

    def test_handles_empty_ignore_file(self, tmp_path: Path):
        (tmp_path / ".gitignore").write_text("")
        spec = _load_ignore_spec(tmp_path)
        # Empty file means no patterns -> spec is still created but matches nothing
        # Actually, pathspec with empty lines returns a spec that matches nothing
        # But our function returns None for no lines
        assert spec is None

    def test_handles_comment_only_gitignore(self, tmp_path: Path):
        (tmp_path / ".gitignore").write_text("# just a comment\n")
        spec = _load_ignore_spec(tmp_path)
        assert spec is not None
        assert not spec.match_file("any_file.txt")
