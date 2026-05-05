"""Route and tool map extractors for code intelligence.

Extracts API route definitions (FastAPI, Flask, Django) and MCP tool
definitions from the codebase. Stores extracted records in SurrealDB
for querying by agents.
"""

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

        # Find all tool name blocks
        # Each tool definition in the JSON schema looks like:
        # {"name": "tool_name", "description": "...", ...}
        tool_blocks = re.finditer(
            r'\{\s*"name"\s*:\s*"(\w+)"\s*,'
            r'\s*"description"\s*:\s*\(\s*"([^"]*(?:"[^"]*"[^"]*)*)"\s*\)',
            source,
            re.DOTALL,
        )

        for block in tool_blocks:
            name = block.group(1)
            description = block.group(2)[:300]

            # Find required params in the nearby context
            block_start = block.start()
            context = source[block_start : block_start + 2000]
            req_match = _MCP_REQUIRED_RE.search(context)
            required = []
            if req_match:
                params = req_match.group(1)
                required = [
                    p.strip().strip('"') for p in params.split(",") if p.strip()
                ]

            tools.append(
                ToolDef(
                    name=name,
                    description=description,
                    server_file=str(server_file.relative_to(repo_path)).replace(
                        "\\", "/"
                    ),
                    server_name=server_name,
                    required_params=required,
                )
            )

    return tools


# ---------------------------------------------------------------------------
# Query helpers (SurrealDB)
# ---------------------------------------------------------------------------


def _upsert_routes(db, routes: list[RouteDef]) -> None:
    """Upsert route definitions into SurrealDB."""
    try:
        db.query("DELETE FROM route")
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
                }""",
                {
                    "path": r.path,
                    "method": r.method,
                    "handler": r.handler,
                    "filepath": r.filepath,
                    "line": r.line,
                    "framework": r.framework,
                    "description": r.description,
                },
            )
        except Exception as e:
            logger.debug("Route upsert failed: %s", e)


def _upsert_tools(db, tools: list[ToolDef]) -> None:
    """Upsert tool definitions into SurrealDB."""
    try:
        db.query("DELETE FROM tool_def")
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
                }""",
                {
                    "name": t.name,
                    "description": t.description,
                    "server_file": t.server_file,
                    "server_name": t.server_name,
                    "required_params": t.required_params,
                },
            )
        except Exception as e:
            logger.debug("Tool upsert failed: %s", e)


def get_routes_map(
    repo_path: Path | None = None,
    framework: str | None = None,
) -> list[dict]:
    """Get API routes from SurrealDB, optionally filtered by framework.

    If no routes exist in the database and repo_path is provided, extracts
    and upserts them first.
    """
    db = get_surreal()

    # Check if routes exist
    try:
        existing = _raw_result_rows(db.query("SELECT count() FROM route GROUP ALL"))
        count = existing[0].get("count", 0) if existing else 0
    except Exception:
        count = 0

    if count == 0 and repo_path:
        routes = extract_routes(repo_path)
        if routes:
            _upsert_routes(db, routes)

    # Query — handle missing table gracefully
    try:
        if framework:
            result = db.query(
                "SELECT * FROM route WHERE framework = $fw ORDER BY path, method",
                {"fw": framework},
            )
        else:
            result = db.query("SELECT * FROM route ORDER BY framework, path, method")
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


def get_tools_map(repo_path: Path | None = None) -> list[dict]:
    """Get MCP tool definitions from SurrealDB.

    If no tools exist in the database and repo_path is provided, extracts
    and upserts them first.
    """
    db = get_surreal()

    # Check if tools exist
    try:
        existing = _raw_result_rows(db.query("SELECT count() FROM tool_def GROUP ALL"))
        count = existing[0].get("count", 0) if existing else 0
    except Exception:
        count = 0

    if count == 0 and repo_path:
        tools = extract_mcp_tools(repo_path)
        if tools:
            _upsert_tools(db, tools)

    try:
        result = db.query("SELECT * FROM tool_def ORDER BY server_name, name")
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
