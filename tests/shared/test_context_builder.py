"""Tests for the context builder module."""

from pathlib import Path

import pytest

from shared.context_builder import generate_structural_context


@pytest.fixture
def python_repo(tmp_path: Path) -> Path:
    """Create a minimal Python repo for testing."""
    (tmp_path / "main.py").write_text(
        "def hello():\n    return 'world'\n\nclass App:\n    pass\n"
    )
    (tmp_path / "utils.py").write_text("CONSTANT = 42\n")
    # Fake .git for commit hash lookup
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("abc123def456\n")
    return tmp_path


class TestGenerateStructuralContext:
    @pytest.mark.asyncio
    async def test_generates_file_tree(self, python_repo: Path):
        file_tree = await generate_structural_context(
            repo_path=python_repo,
        )
        assert file_tree
        assert "main.py" in file_tree
        assert "utils.py" in file_tree

    @pytest.mark.asyncio
    async def test_returns_string(self, python_repo: Path):
        result = await generate_structural_context(
            repo_path=python_repo,
        )
        assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_handles_empty_repo(self, tmp_path: Path):
        empty = tmp_path / "empty"
        empty.mkdir()
        (empty / ".git").mkdir()
        (empty / ".git" / "HEAD").write_text("abc123\n")

        file_tree = await generate_structural_context(
            repo_path=empty,
        )
        assert isinstance(file_tree, str)

    @pytest.mark.asyncio
    async def test_graceful_failure(self, tmp_path: Path):
        nonexistent = tmp_path / "does_not_exist"
        file_tree = await generate_structural_context(
            repo_path=nonexistent,
        )
        assert file_tree == ""
