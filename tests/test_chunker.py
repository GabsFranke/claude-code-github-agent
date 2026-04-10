"""Unit tests for the tree-sitter code chunker."""

from pathlib import Path

import pytest

from shared.chunker import chunk_file, chunk_repo
from shared.ts_languages import EXTENSION_MAP, get_language_config

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def python_repo(tmp_path: Path) -> Path:
    """Create a Python repo with known structure for chunking tests."""
    (tmp_path / "app.py").write_text(
        '''"""Main application module."""

import os
import sys


class Application:
    """The main application class."""

    def __init__(self, name: str):
        self.name = name
        self.db = None

    def run(self) -> None:
        """Run the application."""
        return True

    def shutdown(self):
        pass


def helper():
    """A helper function."""
    return 42


async def async_handler():
    """An async handler."""
    pass
''',
        encoding="utf-8",
    )

    (tmp_path / "utils.py").write_text(
        '''"""Utility functions."""


def format_name(first: str, last: str) -> str:
    """Format a full name."""
    return f"{first} {last}"


def parse_config(path: str) -> dict:
    """Parse a config file."""
    return {}
''',
        encoding="utf-8",
    )

    # Non-Python file
    (tmp_path / "README.md").write_text("# Test\n", encoding="utf-8")

    # Excluded dir
    excluded = tmp_path / "__pycache__"
    excluded.mkdir()
    (excluded / "app.cpython-311.pyc").write_bytes(b"compiled")

    return tmp_path


@pytest.fixture
def large_class_repo(tmp_path: Path) -> Path:
    """Create a Python file with a large class that exceeds max lines."""
    lines = ['"""Module with large class."""', "", "class HugeClass:"]
    lines.append('    """A very large class."""')
    lines.append("    attr = 42")
    lines.append("")
    # Add 20 methods to make it large
    for i in range(20):
        lines.append(f"    def method_{i}(self):")
        lines.append(f'        """Method {i}."""')
        lines.append(f"        return {i}")
        lines.append("")
    lines.append("")

    (tmp_path / "large.py").write_text("\n".join(lines), encoding="utf-8")
    return tmp_path


@pytest.fixture
def multi_lang_repo(tmp_path: Path) -> Path:
    """Create a repo with multiple languages for testing."""
    # JavaScript
    (tmp_path / "app.js").write_text(
        """
function greet(name) {
    return "Hello, " + name;
}

class User {
    constructor(name) {
        this.name = name;
    }

    toString() {
        return this.name;
    }
}
""",
        encoding="utf-8",
    )

    # Go
    (tmp_path / "main.go").write_text(
        """package main

import "fmt"

func main() {
    fmt.Println("hello")
}

func greet(name string) string {
    return "Hello, " + name
}
""",
        encoding="utf-8",
    )

    # Rust
    (tmp_path / "lib.rs").write_text(
        """use std::io;

struct Config {
    name: String,
}

impl Config {
    fn new(name: &str) -> Self {
        Config { name: name.to_string() }
    }
}

fn main() {
    println!("hello");
}
""",
        encoding="utf-8",
    )

    # Unknown language
    (tmp_path / "Makefile").write_text("all:\n\techo hello\n", encoding="utf-8")

    return tmp_path


# ---------------------------------------------------------------------------
# Test: chunk_file
# ---------------------------------------------------------------------------


class TestChunkFile:
    def test_chunks_python_file(self, python_repo: Path):
        chunks = chunk_file(python_repo / "app.py", python_repo)
        assert len(chunks) > 0

        names = {c.name for c in chunks}
        assert "Application" in names
        assert "helper" in names

    def test_module_docstring_extracted(self, python_repo: Path):
        chunks = chunk_file(python_repo / "app.py", python_repo)
        docstrings = [c for c in chunks if c.kind == "module_docstring"]
        if docstrings:
            assert "Main application" in docstrings[0].content
        assert len(chunks) >= 1

    def test_class_chunk(self, python_repo: Path):
        chunks = chunk_file(python_repo / "app.py", python_repo)
        classes = [c for c in chunks if c.kind == "class"]
        assert len(classes) >= 1
        app_class = classes[0]
        assert app_class.name == "Application"
        assert "class Application" in app_class.content
        assert app_class.start_line >= 1
        assert app_class.end_line >= app_class.start_line

    def test_function_chunks(self, python_repo: Path):
        chunks = chunk_file(python_repo / "app.py", python_repo)
        functions = [c for c in chunks if c.kind == "function"]
        func_names = {f.name for f in functions}
        assert "helper" in func_names

    def test_method_chunks_for_large_class(self, large_class_repo: Path):
        chunks = chunk_file(large_class_repo / "large.py", large_class_repo)
        assert len(chunks) >= 1

        methods = [c for c in chunks if c.kind == "method"]
        if methods:
            assert len(methods) >= 10
            for m in methods:
                assert m.parent == "HugeClass"
                assert m.start_line >= 1
                assert m.end_line >= m.start_line
        else:
            classes = [c for c in chunks if c.kind == "class"]
            assert len(classes) >= 1
            assert classes[0].name == "HugeClass"

    def test_chunks_have_correct_filepath(self, python_repo: Path):
        chunks = chunk_file(python_repo / "app.py", python_repo)
        for chunk in chunks:
            assert chunk.filepath == "app.py"

    def test_chunks_have_language(self, python_repo: Path):
        chunks = chunk_file(python_repo / "app.py", python_repo)
        for chunk in chunks:
            assert chunk.language == "python"

    def test_empty_file_returns_empty(self, python_repo: Path):
        (python_repo / "empty.py").write_text("", encoding="utf-8")
        chunks = chunk_file(python_repo / "empty.py", python_repo)
        assert chunks == []

    def test_nonexistent_file_returns_empty(self, python_repo: Path):
        chunks = chunk_file(python_repo / "nonexistent.py", python_repo)
        assert chunks == []

    def test_non_python_file_gets_unknown_language(self, python_repo: Path):
        chunks = chunk_file(python_repo / "README.md", python_repo)
        for chunk in chunks:
            assert chunk.language == "unknown"

    def test_chunk_content_is_valid(self, python_repo: Path):
        chunks = chunk_file(python_repo / "utils.py", python_repo)
        for chunk in chunks:
            assert chunk.content.strip()
            assert chunk.start_line >= 1
            assert chunk.end_line >= chunk.start_line


# ---------------------------------------------------------------------------
# Test: multi-language chunking
# ---------------------------------------------------------------------------


class TestMultiLanguage:
    def test_javascript_file_chunks(self, multi_lang_repo: Path):
        """Test that JavaScript files produce chunks."""
        chunks = chunk_file(multi_lang_repo / "app.js", multi_lang_repo)
        assert len(chunks) > 0
        names = {c.name for c in chunks}
        # Should find the function and class definitions
        # If tree-sitter-javascript is installed, names include 'greet' and 'User'
        # If not installed, regex fallback should still find them
        assert len(names) >= 1

    def test_go_file_chunks(self, multi_lang_repo: Path):
        """Test that Go files produce chunks."""
        chunks = chunk_file(multi_lang_repo / "main.go", multi_lang_repo)
        assert len(chunks) > 0
        names = {c.name for c in chunks}
        assert len(names) >= 1

    def test_rust_file_chunks(self, multi_lang_repo: Path):
        """Test that Rust files produce chunks."""
        chunks = chunk_file(multi_lang_repo / "lib.rs", multi_lang_repo)
        assert len(chunks) > 0

    def test_unknown_file_regex_fallback(self, multi_lang_repo: Path):
        """Test that unknown file types use regex fallback."""
        chunks = chunk_file(multi_lang_repo / "Makefile", multi_lang_repo)
        # Makefile has no matching patterns — should produce a single module chunk
        assert len(chunks) >= 1
        assert chunks[0].kind == "module"

    def test_multi_lang_repo_full_scan(self, multi_lang_repo: Path):
        """Test full scan of multi-language repo."""
        chunks = chunk_repo(multi_lang_repo)
        assert len(chunks) > 0

        files = {c.filepath for c in chunks}
        # Should have chunks from multiple file types
        assert len(files) >= 2


# ---------------------------------------------------------------------------
# Test: chunk_repo
# ---------------------------------------------------------------------------


class TestChunkRepo:
    def test_full_scan(self, python_repo: Path):
        chunks = chunk_repo(python_repo)
        assert len(chunks) > 0

        files = {c.filepath for c in chunks}
        assert "app.py" in files
        assert "utils.py" in files

        for c in chunks:
            assert "__pycache__" not in c.filepath

    def test_incremental_mode(self, python_repo: Path):
        chunks = chunk_repo(python_repo, changed_files=["app.py"])
        assert len(chunks) > 0

        files = {c.filepath for c in chunks}
        assert "app.py" in files
        assert "utils.py" not in files

    def test_incremental_empty_list(self, python_repo: Path):
        chunks = chunk_repo(python_repo, changed_files=[])
        assert chunks == []

    def test_excludes_noise_dirs(self, python_repo: Path):
        chunks = chunk_repo(python_repo)
        for c in chunks:
            assert "__pycache__" not in c.filepath
            assert ".pyc" not in c.filepath

    def test_chunk_count_reasonable(self, python_repo: Path):
        chunks = chunk_repo(python_repo)
        # app.py: module docstring + class (or class+methods) + helper + async_handler
        # utils.py: module docstring + format_name + parse_config
        assert len(chunks) >= 6


# ---------------------------------------------------------------------------
# Test: language registry integration
# ---------------------------------------------------------------------------


class TestLanguageRegistry:
    def test_extension_map_has_expected_entries(self):
        assert ".py" in EXTENSION_MAP
        assert EXTENSION_MAP[".py"] == "python"
        assert ".js" in EXTENSION_MAP
        assert ".go" in EXTENSION_MAP
        assert ".rs" in EXTENSION_MAP
        assert ".java" in EXTENSION_MAP

    def test_config_for_python(self):
        config = get_language_config("python")
        assert config is not None
        assert config.name == "python"
        assert "function_definition" in config.function_types
        assert "class_definition" in config.class_types

    def test_config_for_javascript(self):
        config = get_language_config("javascript")
        assert config is not None
        assert "function_declaration" in config.function_types
        assert "class_declaration" in config.class_types

    def test_unknown_language_returns_none(self):
        config = get_language_config("brainfuck")
        assert config is None
