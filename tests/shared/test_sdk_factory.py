"""Tests for plugin discovery and path normalization in SDKOptionsBuilder."""

import json
import os

from shared.sdk_factory import SDKOptionsBuilder


def test_with_auto_discovered_plugins_empty(tmp_path, monkeypatch):
    """Should discover nothing when the directory is empty or doesn't exist."""

    def mock_expanduser(path):
        if path.startswith("~/.claude/plugins"):
            suffix = path[len("~/.claude/plugins") :]
            if suffix.startswith("/") or suffix.startswith("\\"):
                suffix = suffix[1:]
            return str(tmp_path / suffix)
        return path

    monkeypatch.setattr(os.path, "expanduser", mock_expanduser)

    builder = SDKOptionsBuilder(cwd="/tmp")
    builder.with_auto_discovered_plugins()
    assert len(builder._plugins) == 0


def test_with_auto_discovered_plugins_skip_invalid_folders(tmp_path, monkeypatch):
    """Should skip empty folders or those lacking plugin identifiers."""

    def mock_expanduser(path):
        if path.startswith("~/.claude/plugins"):
            suffix = path[len("~/.claude/plugins") :]
            if suffix.startswith("/") or suffix.startswith("\\"):
                suffix = suffix[1:]
            return str(tmp_path / suffix)
        return path

    monkeypatch.setattr(os.path, "expanduser", mock_expanduser)

    # Create an empty/invalid directory (like oh-my-claudecode with only cache files)
    invalid_dir = tmp_path / "invalid-plugin"
    invalid_dir.mkdir()
    (invalid_dir / ".usage-cache-anthropic.json").write_text("{}", encoding="utf-8")

    # Create a valid plugin directory
    valid_dir = tmp_path / "valid-plugin"
    valid_dir.mkdir()
    (valid_dir / "commands").mkdir()
    (valid_dir / "commands" / "some-command.md").write_text(
        "# Command", encoding="utf-8"
    )

    builder = SDKOptionsBuilder(cwd="/tmp")
    builder.with_auto_discovered_plugins()

    assert len(builder._plugins) == 1
    assert builder._plugins[0]["path"] == str(valid_dir)


def test_with_auto_discovered_plugins_installed_plugins_json(tmp_path, monkeypatch):
    """Should parse installed_plugins.json and normalize/relocate plugin paths."""

    def mock_expanduser(path):
        if path.startswith("~/.claude/plugins"):
            suffix = path[len("~/.claude/plugins") :]
            if suffix.startswith("/") or suffix.startswith("\\"):
                suffix = suffix[1:]
            return str(tmp_path / suffix)
        return path

    monkeypatch.setattr(os.path, "expanduser", mock_expanduser)

    # Prepare directories
    cache_dir = tmp_path / "cache" / "omc" / "oh-my-claudecode" / "4.14.4"
    cache_dir.mkdir(parents=True)
    (cache_dir / "commands").mkdir()
    (cache_dir / "commands" / "team.md").write_text("# Team", encoding="utf-8")

    # This directory exists but has only cache files
    empty_plugin_dir = tmp_path / "oh-my-claudecode"
    empty_plugin_dir.mkdir()
    (empty_plugin_dir / ".usage-cache.json").write_text("{}", encoding="utf-8")

    # Create installed_plugins.json
    installed_plugins = {
        "version": 2,
        "plugins": {
            "oh-my-claudecode@omc": [
                {
                    "scope": "user",
                    "installPath": "/home/user/.claude/plugins/cache/omc/oh-my-claudecode/4.14.4",
                    "version": "4.14.4",
                }
            ]
        },
    }
    (tmp_path / "installed_plugins.json").write_text(
        json.dumps(installed_plugins), encoding="utf-8"
    )

    builder = SDKOptionsBuilder(cwd="/tmp")
    builder.with_auto_discovered_plugins()

    # Should load the actual installed plugin directory from cache and NOT load the empty oh-my-claudecode directory
    assert len(builder._plugins) == 1
    assert builder._plugins[0]["path"] == str(cache_dir)
