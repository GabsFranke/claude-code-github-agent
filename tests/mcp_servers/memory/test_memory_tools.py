"""Unit tests for memory tools."""

from pathlib import Path

import pytest

from mcp_servers.memory.tools import _validate_path, memory_read, memory_write


@pytest.fixture
def temp_memory_dir(tmp_path, monkeypatch):
    """Create a temporary memory directory for testing."""
    memory_base = tmp_path / ".claude" / "projects"
    memory_base.mkdir(parents=True)

    # Monkey patch the memory dir function
    def mock_get_memory_dir(repo: str) -> Path:
        return memory_base / repo / "memory"

    import mcp_servers.memory.tools as tools_module

    monkeypatch.setattr(tools_module, "_get_memory_dir", mock_get_memory_dir)

    return memory_base


def test_memory_read_list_files_empty(temp_memory_dir):
    """Test listing files when memory directory doesn't exist."""
    result = memory_read(file_path=None, repo="test/repo")
    assert result == {"files": []}


def test_memory_read_list_files(temp_memory_dir):
    """Test listing all memory files."""
    repo = "test/repo"
    memory_dir = temp_memory_dir / repo / "memory"
    memory_dir.mkdir(parents=True)

    # Create some files
    (memory_dir / "index.md").write_text("# Index")
    (memory_dir / "commands.md").write_text("# Commands")
    arch_dir = memory_dir / "architecture"
    arch_dir.mkdir()
    (arch_dir / "auth.md").write_text("# Auth")

    result = memory_read(file_path=None, repo=repo)

    assert "files" in result
    files = result["files"]
    assert "index.md" in files
    assert "commands.md" in files
    assert "architecture/auth.md" in files


def test_memory_read_specific_file(temp_memory_dir):
    """Test reading a specific file."""
    repo = "test/repo"
    memory_dir = temp_memory_dir / repo / "memory"
    memory_dir.mkdir(parents=True)

    content = "# Test Content\n\nSome facts here."
    (memory_dir / "index.md").write_text(content)

    result = memory_read(file_path="index.md", repo=repo)

    assert "content" in result
    assert result["content"] == content


def test_memory_read_file_not_found(temp_memory_dir):
    """Test reading a non-existent file."""
    repo = "test/repo"
    memory_dir = temp_memory_dir / repo / "memory"
    memory_dir.mkdir(parents=True)

    with pytest.raises(FileNotFoundError):
        memory_read(file_path="nonexistent.md", repo=repo)


def test_memory_read_nested_file(temp_memory_dir):
    """Test reading a file in a subdirectory."""
    repo = "test/repo"
    memory_dir = temp_memory_dir / repo / "memory"
    arch_dir = memory_dir / "architecture"
    arch_dir.mkdir(parents=True)

    content = "# Authentication Flow"
    (arch_dir / "auth.md").write_text(content)

    result = memory_read(file_path="architecture/auth.md", repo=repo)

    assert result["content"] == content


def test_memory_write_creates_file(temp_memory_dir):
    """Test creating a new file."""
    repo = "test/repo"
    content = "# New File\n\nSome content."

    result = memory_write(file_path="test.md", content=content, repo=repo)

    assert result["success"] is True
    assert result["path"] == "test.md"
    assert result["size"] == len(content)

    # Verify file was created
    memory_dir = temp_memory_dir / repo / "memory"
    assert (memory_dir / "test.md").exists()
    assert (memory_dir / "test.md").read_text() == content


def test_memory_write_creates_directories(temp_memory_dir):
    """Test creating parent directories automatically."""
    repo = "test/repo"
    content = "# Architecture Doc"

    result = memory_write(
        file_path="architecture/database/schema.md", content=content, repo=repo
    )

    assert result["success"] is True

    # Verify nested directories were created
    memory_dir = temp_memory_dir / repo / "memory"
    file_path = memory_dir / "architecture" / "database" / "schema.md"
    assert file_path.exists()
    assert file_path.read_text() == content


def test_memory_write_overwrites_existing(temp_memory_dir):
    """Test overwriting an existing file."""
    repo = "test/repo"
    memory_dir = temp_memory_dir / repo / "memory"
    memory_dir.mkdir(parents=True)

    # Create initial file
    (memory_dir / "test.md").write_text("Old content")

    # Overwrite it
    new_content = "New content"
    result = memory_write(file_path="test.md", content=new_content, repo=repo)

    assert result["success"] is True
    assert (memory_dir / "test.md").read_text() == new_content


def test_path_validation_prevents_traversal(temp_memory_dir):
    """Test that path validation prevents directory traversal attacks."""
    repo = "test/repo"
    memory_dir = temp_memory_dir / repo / "memory"
    memory_dir.mkdir(parents=True)

    # Try to escape memory directory
    with pytest.raises(ValueError, match="outside memory directory"):
        memory_write(file_path="../../../etc/passwd", content="hacked", repo=repo)

    with pytest.raises(ValueError, match="outside memory directory"):
        memory_write(file_path="/etc/passwd", content="hacked", repo=repo)


def test_path_validation_allows_subdirectories(temp_memory_dir):
    """Test that path validation allows legitimate subdirectories."""
    repo = "test/repo"

    # These should all work
    valid_paths = [
        "index.md",
        "architecture/auth.md",
        "issues/bug-123.md",
        "deep/nested/path/file.md",
    ]

    for path in valid_paths:
        result = memory_write(file_path=path, content="test", repo=repo)
        assert result["success"] is True


def test_validate_path_function():
    """Test the _validate_path helper function directly."""
    base_dir = Path("/home/bot/.claude/projects/test/repo/memory")

    # Valid paths
    valid_path = base_dir / "index.md"
    _validate_path(valid_path, base_dir)  # Should not raise

    valid_nested = base_dir / "architecture" / "auth.md"
    _validate_path(valid_nested, base_dir)  # Should not raise

    # Invalid paths
    invalid_path = Path("/etc/passwd")
    with pytest.raises(ValueError):
        _validate_path(invalid_path, base_dir)

    parent_path = base_dir.parent / "other.md"
    with pytest.raises(ValueError):
        _validate_path(parent_path, base_dir)
