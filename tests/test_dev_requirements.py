"""Guard: requirements-dev.txt must install every service's dependencies.

A test module that imports a dependency CI never installed fails at
*collection*, which aborts the entire pytest run — so a single missing
``-r`` line silently reduces the whole suite to zero executed tests while
the checks tab reports only one error. This test makes that failure mode
loud and local instead.
"""

import ast
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DEV_REQUIREMENTS = REPO_ROOT / "requirements-dev.txt"
SERVICES_DIR = REPO_ROOT / "services"


def _referenced_requirement_paths() -> set[str]:
    """Return every path requirements-dev.txt pulls in via a ``-r`` line."""
    referenced = set()
    for line in DEV_REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^\s*-r\s+(\S+)", line)
        if match:
            referenced.add(match.group(1).replace("\\", "/"))
    return referenced


def _service_requirement_paths() -> set[str]:
    """Return every ``services/*/requirements.txt`` in the repo."""
    return {
        path.relative_to(REPO_ROOT).as_posix()
        for path in SERVICES_DIR.glob("*/requirements.txt")
    }


@pytest.mark.unit
def test_dev_requirements_includes_every_service():
    missing = _service_requirement_paths() - _referenced_requirement_paths()
    assert not missing, (
        "requirements-dev.txt does not install: "
        + ", ".join(sorted(missing))
        + ". A test importing one of their dependencies would abort pytest "
        "at collection, silently skipping the entire suite."
    )


@pytest.mark.unit
def test_dev_requirements_references_only_existing_files():
    dangling = {
        ref
        for ref in _referenced_requirement_paths()
        if not (REPO_ROOT / ref).is_file()
    }
    assert not dangling, f"requirements-dev.txt references missing files: {dangling}"


def _declared_distributions() -> set[str]:
    """Return every distribution installed by requirements-dev.txt.

    Follows ``-r`` references, because a package pulled in through a
    service's requirements file is just as installed as one named directly —
    what matters for a collection error is whether pip installs it at all.
    """
    declared: set[str] = set()
    seen: set[Path] = set()
    queue = [DEV_REQUIREMENTS]

    while queue:
        current = queue.pop()
        resolved = current.resolve()
        if resolved in seen or not current.is_file():
            continue
        seen.add(resolved)

        for line in current.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            nested = re.match(r"^-r\s+(\S+)", line)
            if nested:
                queue.append(REPO_ROOT / nested.group(1))
                continue
            if line.startswith("-"):
                continue
            name = re.split(r"[<>=!~\[;]", line, maxsplit=1)[0].strip()
            if name:
                declared.add(name.lower().replace("_", "-"))

    return declared


def _top_level_imports_in_tests() -> dict[str, set[str]]:
    """Map each module imported by tests/** to the files importing it.

    Only module-scope imports: those are the ones that turn a missing
    dependency into a collection error, which aborts the whole run rather
    than failing one test.
    """
    found: dict[str, set[str]] = {}
    for path in (REPO_ROOT / "tests").rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for node in tree.body:
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            for name in names:
                root = name.split(".")[0]
                found.setdefault(root, set()).add(
                    path.relative_to(REPO_ROOT).as_posix()
                )
    return found


# Import name -> distribution name, where the two differ.
_IMPORT_NAME_ALIASES = {
    "yaml": "pyyaml",
    "jwt": "pyjwt",
    "dotenv": "python-dotenv",
}

# Packages that live in this repo rather than on PyPI.
_FIRST_PARTY = {
    "services",
    "shared",
    "subagents",
    "hooks",
    "plugins",
    "mcp_servers",
    "workflows",
    "tests",
    "conftest",
    # Modules reached via sys.path manipulation or a plugin directory rather
    # than by package import.
    "tools",
    "repo_setup",
}


@pytest.mark.unit
def test_test_only_imports_are_declared_in_dev_requirements():
    """Guard the other half of the collection-error failure mode.

    The service-requirements check above catches a missing ``-r`` line. This
    catches a third-party package a test imports at module scope that nothing
    installs — same symptom, same blast radius: pytest aborts at collection
    and the entire suite silently reports zero executed tests.
    """
    declared = _declared_distributions()
    stdlib = sys.stdlib_module_names
    missing: dict[str, set[str]] = {}

    for module, importers in _top_level_imports_in_tests().items():
        if module in stdlib or module in _FIRST_PARTY or module.startswith("_"):
            continue
        normalized = module.lower().replace("_", "-")
        if normalized in declared:
            continue
        # Distributions whose import name differs from their package name.
        if normalized in _IMPORT_NAME_ALIASES:
            continue
        missing[module] = importers

    detail = {name: sorted(files) for name, files in missing.items()}
    assert not missing, (
        "third-party modules imported at test module scope but not declared "
        f"in requirements-dev.txt: {detail}. A missing one aborts pytest at "
        "collection, which reports a single error while silently skipping "
        "every test in the suite."
    )
