"""Tests for shared MCP server base (mcp_servers/base.py)."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def mock_handle_request():
    return AsyncMock(return_value={"protocolVersion": "2024-11-05"})


class TestReadStdinLine:
    @pytest.mark.asyncio
    async def test_reads_line_from_stdin(self):
        with patch("mcp_servers.base.sys.stdin") as mock_stdin:
            mock_stdin.readline = MagicMock(return_value="test line\n")
            from mcp_servers.base import read_stdin_line

            result = await read_stdin_line()
            assert result == "test line\n"

    @pytest.mark.asyncio
    async def test_returns_none_on_empty_input(self):
        with patch("mcp_servers.base.sys.stdin") as mock_stdin:
            mock_stdin.readline = MagicMock(return_value="")
            from mcp_servers.base import read_stdin_line

            result = await read_stdin_line()
            assert result == ""


class TestRunServer:
    @pytest.mark.asyncio
    async def test_processes_valid_request(self, mock_handle_request, capsys):
        from mcp_servers.base import run_server

        request_line = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        )

        with patch(
            "mcp_servers.base.read_stdin_line",
            side_effect=[request_line, ""],
        ):
            await run_server("test-server", mock_handle_request)

        mock_handle_request.assert_called_once()
        captured = capsys.readouterr()
        response = json.loads(captured.out.strip())
        assert response["jsonrpc"] == "2.0"
        assert response["id"] == 1
        assert "result" in response

    @pytest.mark.asyncio
    async def test_skips_notifications(self, mock_handle_request, capsys):
        from mcp_servers.base import run_server

        notification = json.dumps(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}
        )

        with patch(
            "mcp_servers.base.read_stdin_line",
            side_effect=[notification, ""],
        ):
            await run_server("test-server", mock_handle_request)

        mock_handle_request.assert_not_called()
        captured = capsys.readouterr()
        assert captured.out == ""

    @pytest.mark.asyncio
    async def test_handles_json_decode_error(self, capsys):
        from mcp_servers.base import run_server

        mock_handler = AsyncMock()

        with patch(
            "mcp_servers.base.read_stdin_line",
            side_effect=["not valid json{", ""],
        ):
            await run_server("test-server", mock_handler)

        captured = capsys.readouterr()
        response = json.loads(captured.out.strip())
        assert response["error"]["code"] == -32700

    @pytest.mark.asyncio
    async def test_handles_generic_exception(self, capsys):
        from mcp_servers.base import run_server

        async def bad_handler(request):
            raise RuntimeError("boom")

        with patch(
            "mcp_servers.base.read_stdin_line",
            side_effect=[
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": "test"}),
                "",
            ],
        ):
            await run_server("test-server", bad_handler)

        captured = capsys.readouterr()
        response = json.loads(captured.out.strip())
        assert response["error"]["code"] == -32603
        assert response["error"]["message"] == "Internal error"

    @pytest.mark.asyncio
    async def test_calls_init_fn(self, mock_handle_request, capsys):
        from mcp_servers.base import run_server

        init_fn = MagicMock()

        with patch("mcp_servers.base.read_stdin_line", side_effect=[""]):
            await run_server("test-server", mock_handle_request, init_fn=init_fn)

        init_fn.assert_called_once()

    @pytest.mark.asyncio
    async def test_calls_async_init_fn(self, mock_handle_request, capsys):
        from mcp_servers.base import run_server

        init_fn = AsyncMock()

        with patch("mcp_servers.base.read_stdin_line", side_effect=[""]):
            await run_server("test-server", mock_handle_request, init_fn=init_fn)

        init_fn.assert_called_once()

    @pytest.mark.asyncio
    async def test_calls_cleanup_fn(self, mock_handle_request, capsys):
        from mcp_servers.base import run_server

        cleanup_fn = MagicMock()

        with patch("mcp_servers.base.read_stdin_line", side_effect=[""]):
            await run_server(
                "test-server",
                mock_handle_request,
                cleanup_fn=cleanup_fn,
            )

        cleanup_fn.assert_called_once()

    @pytest.mark.asyncio
    async def test_calls_async_cleanup_fn(self, mock_handle_request, capsys):
        from mcp_servers.base import run_server

        cleanup_fn = AsyncMock()

        with patch("mcp_servers.base.read_stdin_line", side_effect=[""]):
            await run_server(
                "test-server",
                mock_handle_request,
                cleanup_fn=cleanup_fn,
            )

        cleanup_fn.assert_called_once()

    @pytest.mark.asyncio
    async def test_breaks_on_empty_line(self, mock_handle_request, capsys):
        from mcp_servers.base import run_server

        with patch("mcp_servers.base.read_stdin_line", side_effect=[""]):
            await run_server("test-server", mock_handle_request)

        mock_handle_request.assert_not_called()
