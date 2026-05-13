"""Tests for import resolution (import_resolver.py)."""

from pathlib import Path

from shared.import_resolver import is_stdlib, resolve_python_import, resolve_ts_import


class TestIsStdlib:
    def test_known_stdlib(self):
        assert is_stdlib("os") is True
        assert is_stdlib("logging") is True
        assert is_stdlib("collections") is True
        assert is_stdlib("typing") is True

    def test_not_stdlib(self):
        assert is_stdlib("database") is False
        assert is_stdlib("shared.utils") is False
        assert is_stdlib("my_custom_lib") is False


class TestResolvePythonImport:
    def test_resolves_same_directory_import(self, tmp_path: Path):
        (tmp_path / "app.py").write_text("")
        (tmp_path / "database.py").write_text("")

        result = resolve_python_import("database", "app.py", tmp_path)
        assert result == "database.py"

    def test_resolves_subdirectory_import(self, tmp_path: Path):
        (tmp_path / "main.py").write_text("")
        pkg = tmp_path / "mypackage"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "utils.py").write_text("")

        result = resolve_python_import("mypackage.utils", "main.py", tmp_path)
        assert result == "mypackage/utils.py"

    def test_resolves_package_init(self, tmp_path: Path):
        (tmp_path / "main.py").write_text("")
        pkg = tmp_path / "mypackage"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")

        result = resolve_python_import("mypackage", "main.py", tmp_path)
        assert result == "mypackage/__init__.py"

    def test_resolves_relative_import_single_dot(self, tmp_path: Path):
        pkg = tmp_path / "mypackage"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "core.py").write_text("")
        (pkg / "utils.py").write_text("")

        result = resolve_python_import(".utils", "mypackage/core.py", tmp_path)
        assert result == "mypackage/utils.py"

    def test_resolves_relative_import_double_dot(self, tmp_path: Path):
        pkg = tmp_path / "mypackage"
        sub = pkg / "sub"
        sub.mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
        (sub / "__init__.py").write_text("")
        (sub / "module.py").write_text("")
        (pkg / "shared.py").write_text("")

        result = resolve_python_import("..shared", "mypackage/sub/module.py", tmp_path)
        assert result == "mypackage/shared.py"

    def test_returns_none_for_stdlib(self, tmp_path: Path):
        (tmp_path / "app.py").write_text("")
        result = resolve_python_import("os", "app.py", tmp_path)
        assert result is None

    def test_returns_none_for_unresolvable(self, tmp_path: Path):
        (tmp_path / "app.py").write_text("")
        result = resolve_python_import("nonexistent_module", "app.py", tmp_path)
        assert result is None

    def test_resolves_from_repo_root(self, tmp_path: Path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "__init__.py").write_text("")
        (src / "database.py").write_text("")
        (tmp_path / "app.py").write_text("")

        result = resolve_python_import("src.database", "app.py", tmp_path)
        assert result == "src/database.py"


class TestResolveTsImport:
    def test_resolves_relative_ts_import(self, tmp_path: Path):
        (tmp_path / "index.ts").write_text("")
        (tmp_path / "utils.ts").write_text("")

        result = resolve_ts_import("./utils", "index.ts", tmp_path)
        assert result == "utils.ts"

    def test_resolves_tsx_import(self, tmp_path: Path):
        (tmp_path / "App.tsx").write_text("")
        comp = tmp_path / "components"
        comp.mkdir()
        (comp / "Button.tsx").write_text("")

        result = resolve_ts_import("./components/Button", "App.tsx", tmp_path)
        assert result == "components/Button.tsx"

    def test_returns_none_for_bare_specifier(self, tmp_path: Path):
        (tmp_path / "index.ts").write_text("")
        result = resolve_ts_import("lodash", "index.ts", tmp_path)
        assert result is None

    def test_resolves_index_file(self, tmp_path: Path):
        (tmp_path / "index.ts").write_text("")
        comp = tmp_path / "components"
        comp.mkdir()
        (comp / "index.ts").write_text("")

        result = resolve_ts_import("./components", "index.ts", tmp_path)
        assert result == "components/index.ts"
