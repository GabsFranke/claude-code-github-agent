"""Integration tests for codebase tools MCP server (server.py).

Tests the JSON-RPC protocol handling: initialize, tools/list, tools/call.
"""

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

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
        patch("shared.code_graph.is_initialized", return_value=True),
        patch("shared.code_graph.get_surreal", return_value=fake_db),
        patch("shared.code_graph.apply_schema"),
        patch("mcp_servers.codebase_tools.tools.init_surrealdb"),
    ):
        yield fake_db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def python_repo(tmp_path: Path) -> Path:
    """Create a small Python repo for server tests."""
    (tmp_path / "app.py").write_text(
        '''"""Main module."""

import os


class App:
    def run(self):
        pass


def main():
    return App()
''',
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture
def mock_server(python_repo: Path):
    """Set up the server module with a mock repo path."""
    os.environ["REPO_PATH"] = str(python_repo)

    # Import after setting env var
    from mcp_servers.codebase_tools import server

    # Initialize the repo
    server.init_repo(python_repo)

    yield server

    # Cleanup
    os.environ.pop("REPO_PATH", None)


# ---------------------------------------------------------------------------
# Test: initialize
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_initialize_returns_server_info(mock_server):
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2024-11-05"},
    }
    response = await mock_server.handle_request(request)

    assert response["protocolVersion"] == "2024-11-05"
    assert response["serverInfo"]["name"] == "codebase_tools"
    assert response["serverInfo"]["version"] == "1.0.0"
    assert "tools" in response["capabilities"]


# ---------------------------------------------------------------------------
# Test: tools/list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tools_list_returns_four_tools(mock_server):
    request = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/list",
        "params": {},
    }
    response = await mock_server.handle_request(request)

    tools = response["tools"]
    assert len(tools) == 11

    tool_names = {t["name"] for t in tools}
    assert tool_names == {
        "find_definitions",
        "find_references",
        "search_codebase",
        "read_file_summary",
        "get_context",
        "get_impact",
        "get_file_overview",
        "detect_changes",
        "trace_flow",
        "get_routes_map",
        "get_tools_map",
    }


@pytest.mark.asyncio
async def test_tools_have_required_schema_fields(mock_server):
    request = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/list",
        "params": {},
    }
    response = await mock_server.handle_request(request)

    for tool in response["tools"]:
        assert "name" in tool
        assert "description" in tool
        assert "inputSchema" in tool
        assert tool["inputSchema"]["type"] == "object"
        assert "properties" in tool["inputSchema"]


# ---------------------------------------------------------------------------
# Test: tools/call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_definitions_call(mock_server):
    request = {
        "jsonrpc": "2.0",
        "id": 4,
        "method": "tools/call",
        "params": {
            "name": "find_definitions",
            "arguments": {"symbol_name": "App"},
        },
    }
    response = await mock_server.handle_request(request)

    assert "content" in response
    content = response["content"][0]
    assert content["type"] == "text"

    results = json.loads(content["text"])
    assert isinstance(results, dict)
    assert len(results.get("results", [])) >= 1
    assert any(
        r.get("kind") == "class" and "App" in r.get("signature", "")
        for r in results.get("results", [])
    )


@pytest.mark.asyncio
async def test_find_references_call(mock_server):
    request = {
        "jsonrpc": "2.0",
        "id": 5,
        "method": "tools/call",
        "params": {
            "name": "find_references",
            "arguments": {"symbol_name": "App"},
        },
    }
    response = await mock_server.handle_request(request)

    assert "content" in response
    results = json.loads(response["content"][0]["text"])
    assert isinstance(results, list)


@pytest.mark.asyncio
async def test_search_codebase_call(mock_server):
    request = {
        "jsonrpc": "2.0",
        "id": 6,
        "method": "tools/call",
        "params": {
            "name": "search_codebase",
            "arguments": {"pattern": "class App"},
        },
    }
    response = await mock_server.handle_request(request)

    assert "content" in response
    results = json.loads(response["content"][0]["text"])
    assert isinstance(results, list)
    assert len(results) >= 1


@pytest.mark.asyncio
async def test_read_file_summary_call(mock_server):
    request = {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {
            "name": "read_file_summary",
            "arguments": {"file_path": "app.py"},
        },
    }
    response = await mock_server.handle_request(request)

    assert "content" in response
    result = json.loads(response["content"][0]["text"])
    assert result["file"] == "app.py"
    assert "signatures" in result
    assert "imports" in result


# ---------------------------------------------------------------------------
# Test: error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_tool_returns_error(mock_server):
    request = {
        "jsonrpc": "2.0",
        "id": 8,
        "method": "tools/call",
        "params": {
            "name": "nonexistent_tool",
            "arguments": {},
        },
    }
    response = await mock_server.handle_request(request)

    assert "error" in response
    assert response["error"]["code"] == -32601
    assert "Unknown tool" in response["error"]["message"]


@pytest.mark.asyncio
async def test_unknown_method_returns_error(mock_server):
    request = {
        "jsonrpc": "2.0",
        "id": 9,
        "method": "unknown/method",
        "params": {},
    }
    response = await mock_server.handle_request(request)

    assert "error" in response
    assert response["error"]["code"] == -32601


@pytest.mark.asyncio
async def test_read_file_summary_not_found(mock_server):
    request = {
        "jsonrpc": "2.0",
        "id": 10,
        "method": "tools/call",
        "params": {
            "name": "read_file_summary",
            "arguments": {"file_path": "nonexistent.py"},
        },
    }
    response = await mock_server.handle_request(request)

    assert "error" in response
    assert "not found" in response["error"]["message"].lower()


@pytest.mark.asyncio
async def test_path_traversal_returns_error(mock_server):
    request = {
        "jsonrpc": "2.0",
        "id": 11,
        "method": "tools/call",
        "params": {
            "name": "read_file_summary",
            "arguments": {"file_path": "../../etc/passwd"},
        },
    }
    response = await mock_server.handle_request(request)

    assert "error" in response
    assert response["error"]["code"] == -32602
    assert "outside repository" in response["error"]["message"]
