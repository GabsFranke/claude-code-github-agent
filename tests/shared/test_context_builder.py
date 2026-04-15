"""Tests for the context builder module."""

from pathlib import Path

import pytest

from shared.context_builder import (
    _cache_key,
    _cache_repomap,
    _get_cached_repomap,
    generate_structural_context,
)


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


class TestCacheKey:
    def test_deterministic(self):
        key1 = _cache_key("owner/repo", "abc123", ["file1.py"])
        key2 = _cache_key("owner/repo", "abc123", ["file1.py"])
        assert key1 == key2

    def test_different_for_different_repos(self):
        key1 = _cache_key("owner/repo", "abc123", [])
        key2 = _cache_key("owner/other", "abc123", [])
        assert key1 != key2

    def test_different_for_different_commits(self):
        key1 = _cache_key("owner/repo", "abc123", [])
        key2 = _cache_key("owner/repo", "def456", [])
        assert key1 != key2

    def test_different_for_different_files(self):
        key1 = _cache_key("owner/repo", "abc123", ["a.py"])
        key2 = _cache_key("owner/repo", "abc123", ["b.py"])
        assert key1 != key2


class TestCaching:
    def test_cache_miss(self, tmp_path: Path):
        result = _get_cached_repomap("nonexistent_key", tmp_path)
        assert result is None

    def test_cache_roundtrip(self, tmp_path: Path):
        key = "testkey123"
        text = "app.py:\n  1│ class App\n  5│ def run"
        _cache_repomap(key, text, tmp_path)
        result = _get_cached_repomap(key, tmp_path)
        assert result == text

    def test_cache_creates_directory(self, tmp_path: Path):
        cache_dir = tmp_path / "new_cache_dir"
        key = "testkey"
        text = "content"
        _cache_repomap(key, text, cache_dir)
        assert (cache_dir / "repomap_cache" / f"{key}.txt").exists()


class TestGenerateStructuralContext:
    @pytest.mark.asyncio
    async def test_generates_file_tree(self, python_repo: Path):
        file_tree, repomap = await generate_structural_context(
            repo_path=python_repo,
        )
        assert file_tree
        assert "main.py" in file_tree
        assert "utils.py" in file_tree

    @pytest.mark.asyncio
    async def test_generates_repomap(self, python_repo: Path):
        file_tree, repomap = await generate_structural_context(
            repo_path=python_repo,
        )
        # Repomap may be empty if tree-sitter is not installed
        # but should not error
        assert isinstance(repomap, str)

    @pytest.mark.asyncio
    async def test_caching(self, python_repo: Path):
        # Use a cache dir OUTSIDE the repo so it doesn't affect file tree generation
        cache_dir = python_repo.parent / "cache_external"

        # First call — should generate
        ft1, rm1 = await generate_structural_context(
            repo_path=python_repo,
            repo="test/repo",
            cache_dir=cache_dir,
        )

        # Second call — should hit cache
        ft2, rm2 = await generate_structural_context(
            repo_path=python_repo,
            repo="test/repo",
            cache_dir=cache_dir,
        )

        # File tree should be the same
        assert ft1 == ft2
        # Repomap should be the same (from cache)
        assert rm1 == rm2

    @pytest.mark.asyncio
    async def test_handles_empty_repo(self, tmp_path: Path):
        empty = tmp_path / "empty"
        empty.mkdir()
        (empty / ".git").mkdir()
        (empty / ".git" / "HEAD").write_text("abc123\n")

        file_tree, repomap = await generate_structural_context(
            repo_path=empty,
        )
        # Should not error, repomap will be empty
        assert isinstance(file_tree, str)
        assert isinstance(repomap, str)

    @pytest.mark.asyncio
    async def test_graceful_failure(self, tmp_path: Path):
        nonexistent = tmp_path / "does_not_exist"
        file_tree, repomap = await generate_structural_context(
            repo_path=nonexistent,
        )
        assert file_tree == ""
        assert repomap == ""
