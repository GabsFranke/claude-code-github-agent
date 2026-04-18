"""Tests for shared tree-sitter language registry."""

from dataclasses import FrozenInstanceError
from unittest.mock import MagicMock, patch

import pytest

from shared.ts_languages import (
    EXTENSION_MAP,
    LANGUAGES,
    LanguageConfig,
    get_language,
    get_language_config,
    language_for_extension,
)


@pytest.fixture(autouse=True)
def clear_cache():
    from shared.ts_languages import _language_cache

    _language_cache.clear()
    yield
    _language_cache.clear()


class TestLanguageRegistry:
    def test_has_ten_entries(self):
        assert len(LANGUAGES) == 10

    def test_all_values_are_language_config(self):
        for name, cfg in LANGUAGES.items():
            assert isinstance(cfg, LanguageConfig), f"{name} is not LanguageConfig"

    def test_required_fields_non_empty(self):
        for name, cfg in LANGUAGES.items():
            assert cfg.name, f"{name} has empty name"
            assert cfg.module, f"{name} has empty module"
            assert cfg.extensions, f"{name} has empty extensions"

    def test_extensions_are_lowercase(self):
        for name, cfg in LANGUAGES.items():
            for ext in cfg.extensions:
                assert ext == ext.lower(), f"{name} has non-lowercase extension: {ext}"

    def test_extensions_are_tuples(self):
        for name, cfg in LANGUAGES.items():
            assert isinstance(cfg.extensions, tuple), f"{name} extensions not a tuple"

    def test_no_duplicate_extensions_across_languages(self):
        seen: dict[str, str] = {}
        for name, cfg in LANGUAGES.items():
            for ext in cfg.extensions:
                assert ext not in seen, f"Extension {ext} in {name} also in {seen[ext]}"
                seen[ext] = name

    def test_no_duplicate_modules(self):
        modules: dict[str, str] = {}
        for name, cfg in LANGUAGES.items():
            if cfg.module in modules:
                # tsx and typescript share tree_sitter_typescript — that's expected
                pair = {modules[cfg.module], name}
                assert pair == {
                    "typescript",
                    "tsx",
                }, f"Module {cfg.module} shared by {modules[cfg.module]} and {name}"
            modules[cfg.module] = name

    def test_expected_languages_present(self):
        expected = [
            "python",
            "javascript",
            "typescript",
            "tsx",
            "go",
            "rust",
            "java",
            "c",
            "cpp",
            "ruby",
        ]
        for lang in expected:
            assert lang in LANGUAGES, f"{lang} missing from LANGUAGES"

    def test_keys_match_name_field(self):
        for key, cfg in LANGUAGES.items():
            assert key == cfg.name, f"Key {key!r} != name {cfg.name!r}"


class TestExtensionMap:
    def test_every_extension_present(self):
        for name, cfg in LANGUAGES.items():
            for ext in cfg.extensions:
                assert ext in EXTENSION_MAP, f"{ext} from {name} missing"
                assert EXTENSION_MAP[ext] == name

    def test_common_extensions_present(self):
        common = [".py", ".js", ".ts", ".tsx", ".go", ".rs", ".java", ".rb"]
        for ext in common:
            assert ext in EXTENSION_MAP, f"{ext} not in EXTENSION_MAP"

    def test_all_keys_start_with_dot(self):
        for ext in EXTENSION_MAP:
            assert ext.startswith("."), f"Extension {ext!r} doesn't start with '.'"

    def test_extension_map_values_are_valid_languages(self):
        for ext, name in EXTENSION_MAP.items():
            assert name in LANGUAGES, f"{ext} maps to unknown language {name!r}"


class TestGetLanguageConfig:
    def test_known_language_returns_config(self):
        cfg = get_language_config("python")
        assert cfg is not None
        assert isinstance(cfg, LanguageConfig)
        assert cfg.name == "python"

    def test_unknown_language_returns_none(self):
        assert get_language_config("brainfuck") is None

    def test_empty_string_returns_none(self):
        assert get_language_config("") is None

    def test_case_sensitive(self):
        assert get_language_config("Python") is None
        assert get_language_config("PYTHON") is None


class TestGetLanguage:
    def test_returns_language_for_installed_package(self):
        result = get_language("python")
        assert result is not None

    def test_caches_result(self):
        from shared.ts_languages import _language_cache

        result1 = get_language("python")
        assert "python" in _language_cache
        result2 = get_language("python")
        assert result1 is result2

    def test_returns_none_for_unknown(self):
        assert get_language("nonexistent_lang") is None

    def test_caches_none_result(self):
        from shared.ts_languages import _language_cache

        result1 = get_language("nonexistent_lang")
        result2 = get_language("nonexistent_lang")
        assert result1 is None
        assert result2 is None
        assert "nonexistent_lang" in _language_cache

    @patch("shared.ts_languages.importlib.import_module")
    def test_handles_import_error(self, mock_import):
        mock_import.side_effect = ImportError("no module")
        result = get_language("python")
        assert result is None

    @patch("shared.ts_languages.importlib.import_module")
    def test_handles_generic_exception(self, mock_import):
        mock_mod = MagicMock()
        mock_mod.language.side_effect = RuntimeError("boom")
        mock_import.return_value = mock_mod
        result = get_language("python")
        assert result is None

    def test_typescript_loads_correctly(self):
        result = get_language("typescript")
        assert result is not None

    def test_tsx_loads_correctly(self):
        result = get_language("tsx")
        assert result is not None


class TestLanguageForExtension:
    def test_known_extension_returns_tuple(self):
        lang, cfg = language_for_extension(".py")
        assert lang is not None
        assert cfg is not None
        assert cfg.name == "python"

    def test_unknown_extension_returns_none_tuple(self):
        lang, cfg = language_for_extension(".xyz")
        assert lang is None
        assert cfg is None

    def test_case_insensitive(self):
        lang, cfg = language_for_extension(".PY")
        assert cfg is not None
        assert cfg.name == "python"

    def test_various_extensions(self):
        _, cfg = language_for_extension(".rs")
        assert cfg.name == "rust"

        _, cfg = language_for_extension(".go")
        assert cfg.name == "go"

    def test_empty_extension_returns_none_tuple(self):
        lang, cfg = language_for_extension("")
        assert lang is None
        assert cfg is None


class TestLanguageConfigDataclass:
    def test_frozen_raises_on_set(self):
        cfg = get_language_config("python")
        assert cfg is not None
        with pytest.raises(FrozenInstanceError):
            cfg.name = "changed"

    def test_default_language_attr(self):
        cfg = LanguageConfig(
            name="test",
            module="test_mod",
            extensions=(".tst",),
        )
        assert cfg.language_attr == "language"

    def test_default_frozensets_empty(self):
        cfg = LanguageConfig(
            name="test",
            module="test_mod",
            extensions=(".tst",),
        )
        assert cfg.function_types == frozenset()
        assert cfg.class_types == frozenset()
        assert cfg.method_types == frozenset()
        assert cfg.decorator_types == frozenset()

    def test_python_function_types(self):
        cfg = get_language_config("python")
        assert cfg is not None
        assert "function_definition" in cfg.function_types

    def test_javascript_function_types(self):
        cfg = get_language_config("javascript")
        assert cfg is not None
        assert "function_declaration" in cfg.function_types

    def test_go_function_types(self):
        cfg = get_language_config("go")
        assert cfg is not None
        assert "function_declaration" in cfg.function_types
