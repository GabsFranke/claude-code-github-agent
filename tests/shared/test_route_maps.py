"""Tests for the route_maps module (RouteDef, ToolDef, extractors)."""

from pathlib import Path
from unittest.mock import patch

import pytest

from shared.route_maps import (
    RouteDef,
    ToolDef,
    extract_fastapi_routes,
    extract_flask_routes,
    extract_mcp_tools,
    extract_routes,
    get_routes_map,
    get_tools_map,
)
from tests.conftest import FakeSurrealDB


@pytest.fixture(autouse=True)
def _mock_surrealdb():
    """Mock SurrealDB for all tests — no real connection needed."""
    fake_db = FakeSurrealDB()

    with (
        patch("shared.surrealdb_client.is_initialized", return_value=True),
        patch("shared.surrealdb_client.get_surreal", return_value=fake_db),
        patch("shared.surrealdb_client.init_surrealdb"),
        patch("shared.surrealdb_client.apply_schema"),
        patch("shared.route_maps.get_surreal", return_value=fake_db),
    ):
        yield fake_db


# ---------------------------------------------------------------------------
# Test: RouteDef
# ---------------------------------------------------------------------------


class TestRouteDef:
    def test_creation(self):
        r = RouteDef(
            path="/api/users",
            method="GET",
            handler="get_users",
            filepath="services/api/main.py",
            line=42,
            framework="fastapi",
        )
        assert r.path == "/api/users"
        assert r.method == "GET"
        assert r.handler == "get_users"
        assert r.framework == "fastapi"

    def test_defaults(self):
        r = RouteDef(path="/", method="GET", handler="index", filepath="app.py", line=1)
        assert r.framework == "fastapi"
        assert r.decorator == ""
        assert r.description == ""


# ---------------------------------------------------------------------------
# Test: ToolDef
# ---------------------------------------------------------------------------


class TestToolDef:
    def test_creation(self):
        t = ToolDef(
            name="find_definitions",
            description="Find where a symbol is defined",
            server_file="mcp_servers/codebase_tools/server.py",
            server_name="codebase_tools",
            required_params=["symbol_name"],
        )
        assert t.name == "find_definitions"
        assert t.required_params == ["symbol_name"]

    def test_defaults(self):
        t = ToolDef(
            name="my_tool",
            description="Does things",
            server_file="server.py",
            server_name="test",
        )
        assert t.required_params == []


# ---------------------------------------------------------------------------
# Test: FastAPI route extraction
# ---------------------------------------------------------------------------


class TestExtractFastAPIRoutes:
    def test_extracts_get_route(self, tmp_path: Path):
        (tmp_path / "main.py").write_text(
            '@app.get("/health")\n'
            "async def health_check():\n"
            '    return {"status": "ok"}\n'
        )
        routes = extract_fastapi_routes(tmp_path / "main.py", tmp_path)
        assert len(routes) == 1
        assert routes[0].path == "/health"
        assert routes[0].method == "GET"
        assert routes[0].handler == "health_check"
        assert routes[0].framework == "fastapi"

    def test_extracts_post_route(self, tmp_path: Path):
        (tmp_path / "main.py").write_text(
            '@router.post("/webhook")\n'
            "async def handle_webhook(request: Request):\n"
            "    pass\n"
        )
        routes = extract_fastapi_routes(tmp_path / "main.py", tmp_path)
        assert len(routes) == 1
        assert routes[0].path == "/webhook"
        assert routes[0].method == "POST"
        assert routes[0].handler == "handle_webhook"

    def test_extracts_multiple_routes(self, tmp_path: Path):
        (tmp_path / "main.py").write_text(
            '@app.get("/")\n'
            "def index():\n"
            '    return "ok"\n\n'
            '@app.post("/items")\n'
            "def create_item():\n"
            "    pass\n\n"
            '@router.put("/items/{id}")\n'
            "def update_item(id: str):\n"
            "    pass\n"
        )
        routes = extract_fastapi_routes(tmp_path / "main.py", tmp_path)
        assert len(routes) == 3
        methods = {r.method for r in routes}
        assert methods == {"GET", "POST", "PUT"}

    def test_no_routes_in_empty_file(self, tmp_path: Path):
        (tmp_path / "empty.py").write_text("# just a comment\n")
        routes = extract_fastapi_routes(tmp_path / "empty.py", tmp_path)
        assert routes == []

    def test_handler_not_found_if_eof(self, tmp_path: Path):
        """If decorator is on last line, handler stays empty."""
        (tmp_path / "main.py").write_text('@app.get("/last")')
        routes = extract_fastapi_routes(tmp_path / "main.py", tmp_path)
        assert len(routes) == 1
        assert routes[0].handler == ""


# ---------------------------------------------------------------------------
# Test: Flask route extraction
# ---------------------------------------------------------------------------


class TestExtractFlaskRoutes:
    def test_extracts_basic_route(self, tmp_path: Path):
        (tmp_path / "app.py").write_text(
            '@app.route("/home")\n' "def home():\n" '    return "hello"\n'
        )
        routes = extract_flask_routes(tmp_path / "app.py", tmp_path)
        assert len(routes) == 1
        assert routes[0].path == "/home"
        assert routes[0].method == "GET"
        assert routes[0].framework == "flask"

    def test_extracts_route_with_methods(self, tmp_path: Path):
        (tmp_path / "app.py").write_text(
            '@app.route("/api", methods=["GET", "POST"])\n'
            "def api_handler():\n"
            "    pass\n"
        )
        routes = extract_flask_routes(tmp_path / "app.py", tmp_path)
        assert len(routes) == 2
        methods = {r.method for r in routes}
        assert methods == {"GET", "POST"}


# ---------------------------------------------------------------------------
# Test: MCP tool extraction
# ---------------------------------------------------------------------------


class TestExtractMCPTools:
    def test_extracts_tools_from_server(self, tmp_path: Path):
        server_dir = tmp_path / "mcp_servers" / "test_server"
        server_dir.mkdir(parents=True)
        (server_dir / "server.py").write_text(
            '{"name": "my_tool", "description": ("Does something."),'
            ' "inputSchema": {"required": ["arg1", "arg2"]}}\n'
        )

        tools = extract_mcp_tools(tmp_path)
        assert len(tools) == 1
        assert tools[0].name == "my_tool"
        assert tools[0].server_name == "test_server"

    def test_no_mcp_servers_dir(self, tmp_path: Path):
        tools = extract_mcp_tools(tmp_path)
        assert tools == []


# ---------------------------------------------------------------------------
# Test: extract_routes (full scan)
# ---------------------------------------------------------------------------


class TestExtractRoutes:
    def test_scans_python_files(self, tmp_path: Path):
        (tmp_path / "main.py").write_text('@app.get("/")\ndef index():\n    pass\n')
        (tmp_path / "other.py").write_text(
            '@router.post("/data")\ndef post_data():\n    pass\n'
        )
        routes = extract_routes(tmp_path)
        assert len(routes) == 2

    def test_skips_non_python_files(self, tmp_path: Path):
        (tmp_path / "main.py").write_text('@app.get("/")\ndef index():\n    pass\n')
        (tmp_path / "script.js").write_text('app.get("/api", (req, res) => {})\n')
        routes = extract_routes(tmp_path)
        assert len(routes) == 1


# ---------------------------------------------------------------------------
# Test: get_routes_map from SurrealDB
# ---------------------------------------------------------------------------


class TestGetRoutesMap:
    def test_returns_list_from_db(self):
        routes = get_routes_map()
        assert isinstance(routes, list)

    def test_filter_by_framework(self):
        routes = get_routes_map(framework="fastapi")
        assert isinstance(routes, list)
        for r in routes:
            assert r.get("framework", "fastapi") == "fastapi"


# ---------------------------------------------------------------------------
# Test: get_tools_map from SurrealDB
# ---------------------------------------------------------------------------


class TestGetToolsMap:
    def test_returns_list_from_db(self):
        tools = get_tools_map()
        assert isinstance(tools, list)
