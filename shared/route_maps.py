"""Route and tool map extractors for code intelligence.

Extracts API route definitions (FastAPI, Flask, Django) and MCP tool
definitions from the codebase. Stores extracted records in SurrealDB
for querying by agents.
"""

import ast
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from .file_tree import load_ignore_spec, walk_source_files
from .surrealdb_client import _raw_result_rows, get_surreal

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class RouteDef:
    """An API route definition."""

    path: str
    method: str
    handler: str
    filepath: str
    line: int
    framework: str = "fastapi"
    decorator: str = ""
    description: str = ""


@dataclass
class ToolDef:
    """An MCP tool definition."""

    name: str
    description: str
    server_file: str
    server_name: str
    required_params: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Regex patterns for route extraction
# ---------------------------------------------------------------------------

_FASTAPI_ROUTE_RE = re.compile(
    r'@(?:app|router)\.(get|post|put|delete|patch|head|options|trace)\s*\(\s*["\']([^"\']+)["\']'
)

_FLASK_ROUTE_RE = re.compile(
    r'@\w+\.route\s*\(\s*["\']([^"\']+)["\']' r"(?:,\s*methods\s*=\s*\[([^\]]+)\])?"
)

_DJANGO_PATH_RE = re.compile(r'(?:path|re_path|url)\s*\(\s*["\']([^"\']+)["\']')

_MCP_TOOL_NAME_RE = re.compile(r'"name"\s*:\s*"(\w+)"')
_MCP_TOOL_DESC_RE = re.compile(r'"description"\s*:\s*\(\s*"([^"]+)"')
_MCP_REQUIRED_RE = re.compile(r'"required"\s*:\s*\[([^\]]*)\]')


# ---------------------------------------------------------------------------
# Route extraction
# ---------------------------------------------------------------------------


def extract_fastapi_routes(filepath: Path, repo_root: Path) -> list[RouteDef]:
    """Extract FastAPI route definitions from a Python file."""
    routes: list[RouteDef] = []
    try:
        source = filepath.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return routes

    rel_path = str(filepath.relative_to(repo_root)).replace("\\", "/")
    lines = source.splitlines()

    for i, line in enumerate(lines, 1):
        m = _FASTAPI_ROUTE_RE.search(line)
        if m:
            method = m.group(1)
            path = m.group(2)

            handler = ""
            if i < len(lines):
                handler_line = lines[i]
                hdr_match = re.match(r"(?:async\s+)?def\s+(\w+)", handler_line)
                if hdr_match:
                    handler = hdr_match.group(1)

            routes.append(
                RouteDef(
                    path=path,
                    method=method.upper(),
                    handler=handler,
                    filepath=rel_path,
                    line=i,
                    framework="fastapi",
                    decorator=line.strip(),
                )
            )

    return routes


def extract_flask_routes(filepath: Path, repo_root: Path) -> list[RouteDef]:
    """Extract Flask route definitions from a Python file."""
    routes: list[RouteDef] = []
    try:
        source = filepath.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return routes

    rel_path = str(filepath.relative_to(repo_root)).replace("\\", "/")
    lines = source.splitlines()

    for i, line in enumerate(lines, 1):
        m = _FLASK_ROUTE_RE.search(line)
        if m:
            path = m.group(1)
            methods_str = m.group(2)
            if methods_str:
                methods = [m.strip().strip("'\"") for m in methods_str.split(",")]
            else:
                methods = ["GET"]

            handler = ""
            if i < len(lines):
                handler_line = lines[i]
                hdr_match = re.match(r"def\s+(\w+)", handler_line)
                if hdr_match:
                    handler = hdr_match.group(1)

            for method in methods:
                routes.append(
                    RouteDef(
                        path=path,
                        method=method.upper(),
                        handler=handler,
                        filepath=rel_path,
                        line=i,
                        framework="flask",
                        decorator=line.strip(),
                    )
                )

    return routes


def extract_django_routes(filepath: Path, repo_root: Path) -> list[RouteDef]:
    """Extract Django URL patterns from a Python file."""
    routes: list[RouteDef] = []
    try:
        source = filepath.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return routes

    rel_path = str(filepath.relative_to(repo_root)).replace("\\", "/")

    for i, line in enumerate(source.splitlines(), 1):
        m = _DJANGO_PATH_RE.search(line)
        if m:
            path = m.group(1)
            routes.append(
                RouteDef(
                    path=path,
                    method="ALL",
                    handler="",
                    filepath=rel_path,
                    line=i,
                    framework="django",
                    decorator=line.strip(),
                )
            )

    return routes


def extract_routes(repo_path: Path) -> list[RouteDef]:
    """Extract all API route definitions from the repository."""
    routes: list[RouteDef] = []
    ignore_spec = load_ignore_spec(repo_path)

    for filepath in walk_source_files(repo_path, ignore_spec):
        if filepath.suffix != ".py":
            continue

        routes.extend(extract_fastapi_routes(filepath, repo_path))
        routes.extend(extract_flask_routes(filepath, repo_path))

        if filepath.name == "urls.py":
            routes.extend(extract_django_routes(filepath, repo_path))

    return routes


# ---------------------------------------------------------------------------
# MCP tool extraction
# ---------------------------------------------------------------------------


def _extract_required_params_from_ast(schema_node: ast.AST) -> list[str]:
    """Extract required params list from an inputSchema dict node."""
    if not isinstance(schema_node, ast.Dict):
        return []
    for key, value in zip(schema_node.keys, schema_node.values):
        if (
            isinstance(key, ast.Constant)
            and key.value == "required"
            and isinstance(value, ast.List)
        ):
            return [
                elt.value
                for elt in value.elts
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
            ]
    return []


def _extract_tools_from_ast(
    source: str, server_file: str, server_name: str
) -> list[ToolDef]:
    """Extract tool definitions by parsing the Python AST.

    Handles both parenthesized concatenated descriptions and plain strings.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    tools: list[ToolDef] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue

        name = None
        description = None
        required_params: list[str] = []

        for key, value in zip(node.keys, node.values):
            if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                continue
            if (
                key.value == "name"
                and isinstance(value, ast.Constant)
                and isinstance(value.value, str)
            ):
                name = value.value
            elif (
                key.value == "description"
                and isinstance(value, ast.Constant)
                and isinstance(value.value, str)
            ):
                description = value.value
            elif key.value == "inputSchema":
                required_params = _extract_required_params_from_ast(value)

        if name and description:
            tools.append(
                ToolDef(
                    name=name,
                    description=description[:300],
                    server_file=server_file,
                    server_name=server_name,
                    required_params=required_params,
                )
            )
    return tools


def _extract_tools_regex(
    source: str, server_file: str, server_name: str
) -> list[ToolDef]:
    """Fallback regex extraction for when AST parsing fails."""
    tools: list[ToolDef] = []
    seen_names: set[str] = set()

    # Pattern A: plain string description  ("description": "...")
    for m in re.finditer(
        r'\{\s*"name"\s*:\s*"(\w+)"\s*,\s*"description"\s*:\s*"([^"]{1,300})"',
        source,
    ):
        name, desc = m.group(1), m.group(2)
        tools.append(
            ToolDef(
                name=name,
                description=desc,
                server_file=server_file,
                server_name=server_name,
            )
        )
        seen_names.add(name)

    # Pattern B: parenthesized description  ("description": ("..." "..."))
    for m in re.finditer(
        r'\{\s*"name"\s*:\s*"(\w+)"\s*,\s*"description"\s*:\s*\(\s*(.*?)\s*\)',
        source,
        re.DOTALL,
    ):
        name = m.group(1)
        if name in seen_names:
            continue
        # Extract all quoted fragments from inside the parens
        fragments = re.findall(r'"([^"]*)"', m.group(2))
        desc = "".join(fragments)[:300]
        if desc:
            tools.append(
                ToolDef(
                    name=name,
                    description=desc,
                    server_file=server_file,
                    server_name=server_name,
                )
            )
            seen_names.add(name)

    # Extract required params from nearby context for each tool
    for tool in tools:
        escaped = re.escape(tool.name)
        ctx = re.search(
            rf'"{escaped}".*?"required"\s*:\s*\[([^\]]*)\]',
            source,
            re.DOTALL,
        )
        if ctx:
            tool.required_params = [
                p.strip().strip('"') for p in ctx.group(1).split(",") if p.strip()
            ]

    return tools


def extract_mcp_tools(repo_path: Path) -> list[ToolDef]:
    """Extract MCP tool definitions from MCP server files."""
    tools: list[ToolDef] = []
    mcps_dir = repo_path / "mcp_servers"

    if not mcps_dir.is_dir():
        return tools

    for server_dir in mcps_dir.iterdir():
        if not server_dir.is_dir():
            continue

        server_name = server_dir.name
        server_file = server_dir / "server.py"
        if not server_file.is_file():
            continue

        try:
            source = server_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        server_rel = str(server_file.relative_to(repo_path)).replace("\\", "/")

        extracted = _extract_tools_from_ast(source, server_rel, server_name)
        if extracted:
            tools.extend(extracted)
            continue

        tools.extend(_extract_tools_regex(source, server_rel, server_name))

    return tools


# ---------------------------------------------------------------------------
# Query helpers (SurrealDB)
# ---------------------------------------------------------------------------


def _upsert_routes(db, routes: list[RouteDef], repo: str = "") -> None:
    """Upsert route definitions into SurrealDB, scoped by repo."""
    try:
        db.query("DELETE FROM route WHERE repo = $repo", {"repo": repo})
    except Exception:
        pass

    for r in routes:
        try:
            db.query(
                """INSERT INTO route {
                    path: $path,
                    method: $method,
                    handler: $handler,
                    filepath: $filepath,
                    line: $line,
                    framework: $framework,
                    description: $description,
                    repo: $repo,
                }""",
                {
                    "path": r.path,
                    "method": r.method,
                    "handler": r.handler,
                    "filepath": r.filepath,
                    "line": r.line,
                    "framework": r.framework,
                    "description": r.description,
                    "repo": repo,
                },
            )
        except Exception as e:
            logger.debug("Route upsert failed: %s", e)


def _upsert_tools(db, tools: list[ToolDef], repo: str = "") -> None:
    """Upsert tool definitions into SurrealDB, scoped by repo."""
    try:
        db.query("DELETE FROM tool_def WHERE repo = $repo", {"repo": repo})
    except Exception:
        pass

    for t in tools:
        try:
            db.query(
                """INSERT INTO tool_def {
                    name: $name,
                    description: $description,
                    server_file: $server_file,
                    server_name: $server_name,
                    required_params: $required_params,
                    repo: $repo,
                }""",
                {
                    "name": t.name,
                    "description": t.description,
                    "server_file": t.server_file,
                    "server_name": t.server_name,
                    "required_params": t.required_params,
                    "repo": repo,
                },
            )
        except Exception as e:
            logger.debug("Tool upsert failed: %s", e)


def get_routes_map(
    repo_path: Path | None = None,
    framework: str | None = None,
    repo: str = "",
) -> list[dict]:
    """Get API routes from SurrealDB, optionally filtered by framework.

    If no routes exist in the database and repo_path is provided, extracts
    and upserts them first.
    """
    db = get_surreal()

    # Check if routes exist for this repo
    try:
        existing = _raw_result_rows(
            db.query(
                "SELECT count() FROM route WHERE repo = $repo GROUP ALL",
                {"repo": repo},
            )
        )
        count = existing[0].get("count", 0) if existing else 0
    except Exception:
        count = 0

    if count == 0 and repo_path:
        routes = extract_routes(repo_path)
        if routes:
            _upsert_routes(db, routes, repo)

    # Query — handle missing table gracefully
    try:
        if framework:
            result = db.query(
                "SELECT * FROM route WHERE framework = $fw AND repo = $repo ORDER BY path, method",
                {"fw": framework, "repo": repo},
            )
        else:
            result = db.query(
                "SELECT * FROM route WHERE repo = $repo ORDER BY framework, path, method",
                {"repo": repo},
            )
        rows = _raw_result_rows(result)
    except Exception:
        logger.debug("Failed to query route table (table may not exist)")
        return []
    return [
        {
            "path": r.get("path", ""),
            "method": r.get("method", ""),
            "handler": r.get("handler", ""),
            "filepath": r.get("filepath", ""),
            "line": r.get("line", 0),
            "framework": r.get("framework", ""),
            "description": r.get("description", ""),
        }
        for r in rows
    ]


def get_tools_map(repo_path: Path | None = None, repo: str = "") -> list[dict]:
    """Get MCP tool definitions from SurrealDB.

    If no tools exist in the database and repo_path is provided, extracts
    and upserts them first.
    """
    db = get_surreal()

    # Check if tools exist for this repo
    try:
        existing = _raw_result_rows(
            db.query(
                "SELECT count() FROM tool_def WHERE repo = $repo GROUP ALL",
                {"repo": repo},
            )
        )
        count = existing[0].get("count", 0) if existing else 0
    except Exception:
        count = 0

    if count == 0 and repo_path:
        tools = extract_mcp_tools(repo_path)
        if tools:
            _upsert_tools(db, tools, repo)

    try:
        result = db.query(
            "SELECT * FROM tool_def WHERE repo = $repo ORDER BY server_name, name",
            {"repo": repo},
        )
        rows = _raw_result_rows(result)
    except Exception:
        logger.debug("Failed to query tool_def table (table may not exist)")
        return []
    return [
        {
            "name": r.get("name", ""),
            "description": r.get("description", ""),
            "server_file": r.get("server_file", ""),
            "server_name": r.get("server_name", ""),
            "required_params": r.get("required_params", []),
        }
        for r in rows
    ]
