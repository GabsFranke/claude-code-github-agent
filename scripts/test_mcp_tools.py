#!/usr/bin/env python3
"""Directly call custom MCP tools for testing without Docker/proxy/SDK.

Supports two modes:
  - Direct mode (default): Import and call tool functions in-process.
  - RPC mode (--rpc): Spawn the server subprocess, JSON-RPC over stdin/stdout.

Usage:
  python scripts/test_mcp_tools.py codebase_tools --list
  python scripts/test_mcp_tools.py codebase_tools --tool find_definitions --symbol_name Application
  python scripts/test_mcp_tools.py codebase_tools --rpc --tool find_definitions --symbol_name Application
  python scripts/test_mcp_tools.py memory --tool memory_read --repo owner/name
  python scripts/test_mcp_tools.py github_actions --tool get_workflow_run_summary --owner X --repo Y --run_id Z
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

# Add project root to sys.path so shared/ and mcp_servers/ imports resolve
_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(_PROJECT_ROOT))


# ---------------------------------------------------------------------------
# .env loading (adapted from scripts/get_github_token.py)
# ---------------------------------------------------------------------------


def load_env() -> None:
    """Load environment variables from .env in project root."""
    try:
        from dotenv import load_dotenv

        env_path = _PROJECT_ROOT / ".env"
        if env_path.exists():
            load_dotenv(env_path)
    except ImportError:
        env_file = _PROJECT_ROOT / ".env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    if key not in os.environ:
                        os.environ[key] = value.strip('"').strip("'")


# ---------------------------------------------------------------------------
# Extra argument parsing
# ---------------------------------------------------------------------------


def _coerce_value(raw: str) -> Any:
    """Convert a CLI string to the best-fit Python type."""
    lower = raw.lower()
    if lower in ("true", "yes"):
        return True
    if lower in ("false", "no"):
        return False
    if lower in ("null", "none"):
        return None
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def parse_extra_args(remaining: list[str]) -> dict[str, Any]:
    """Convert remaining CLI tokens into a kwargs dict.

    Handles: --key value, --key=value, --flag (bool True).
    """
    kwargs: dict[str, Any] = {}
    i = 0
    while i < len(remaining):
        arg = remaining[i]
        if arg.startswith("--"):
            key = arg[2:]
            if "=" in key:
                key, val = key.split("=", 1)
                kwargs[key.replace("-", "_")] = _coerce_value(val)
            elif key.startswith("no-"):
                kwargs[key[3:].replace("-", "_")] = False
            else:
                if i + 1 < len(remaining) and not remaining[i + 1].startswith("--"):
                    kwargs[key.replace("-", "_")] = _coerce_value(remaining[i + 1])
                    i += 1
                else:
                    kwargs[key.replace("-", "_")] = True
        i += 1
    return kwargs


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def print_json(data: Any, *, indent: int | None = 2, is_error: bool = False) -> None:
    stream = sys.stderr if is_error else sys.stdout
    print(
        json.dumps(data, indent=indent, default=str, ensure_ascii=False),
        file=stream,
    )


def print_tool_info(tool: dict) -> None:
    name = tool["name"]
    desc = tool.get("description", "")
    required = tool.get("inputSchema", {}).get("required", [])
    print(f"  {name}")
    print(f"    {desc[:120]}{'...' if len(desc) > 120 else ''}")
    if required:
        print(f"    Required: {', '.join(required)}")
    print()


# ---------------------------------------------------------------------------
# Tool listing helpers
# ---------------------------------------------------------------------------


def list_codebase_tools_direct() -> None:
    from mcp_servers.codebase_tools.server import _tool_definitions

    print("codebase_tools (direct mode):")
    for tool in _tool_definitions():
        print_tool_info(tool)


async def list_memory_tools_direct() -> None:
    from mcp_servers.memory.server import handle_request

    print("memory (direct mode):")
    response = await handle_request({"method": "tools/list", "params": {}})
    for tool in response.get("tools", []):
        print_tool_info(tool)


async def list_github_actions_tools_direct() -> None:
    from mcp_servers.github_actions.server import TOOLS

    print("github_actions (direct mode):")
    for tool in TOOLS:
        print_tool_info(tool)


# ---------------------------------------------------------------------------
# Direct mode runners
# ---------------------------------------------------------------------------


def run_codebase_direct(tool_name: str, kwargs: dict, repo_path: str | None) -> None:
    from mcp_servers.codebase_tools.tools import init_repo

    path = repo_path or os.getcwd()
    print(f"Initializing repo at {path} ...", file=sys.stderr)
    try:
        init_repo(str(path))
    except Exception as e:
        print(f"Error initializing repo: {e}", file=sys.stderr)
        sys.exit(1)

    import mcp_servers.codebase_tools.tools as tmod

    func = getattr(tmod, tool_name, None)
    if func is None:
        print(f"Error: Unknown tool '{tool_name}' in codebase_tools", file=sys.stderr)
        sys.exit(1)

    try:
        result = func(**kwargs)
        print_json(result)
    except TypeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def run_memory_direct(tool_name: str, kwargs: dict) -> None:
    import mcp_servers.memory.tools as tmod

    func = getattr(tmod, tool_name, None)
    if func is None:
        print(f"Error: Unknown tool '{tool_name}' in memory", file=sys.stderr)
        sys.exit(1)

    try:
        result = func(**kwargs)
        print_json(result)
    except TypeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


async def run_github_actions_direct(tool_name: str, kwargs: dict) -> None:
    import mcp_servers.github_actions.tools.github_actions as gmod

    func = getattr(gmod, tool_name, None)
    if func is None:
        print(f"Error: Unknown tool '{tool_name}' in github_actions", file=sys.stderr)
        sys.exit(1)

    try:
        result = await func(**kwargs)
        print_json(result)
    except TypeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# RPC mode (JSON-RPC over subprocess stdin/stdout)
# ---------------------------------------------------------------------------


async def _send_jsonrpc(process: asyncio.subprocess.Process, method: str, params: dict) -> None:
    request: dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    line = json.dumps(request) + "\n"
    if process.stdin is None:
        raise RuntimeError("Process stdin unavailable")
    process.stdin.write(line.encode())
    await process.stdin.drain()


async def _recv_jsonrpc(process: asyncio.subprocess.Process) -> dict[str, Any]:
    if process.stdout is None:
        raise RuntimeError("Process stdout unavailable")
    line = await process.stdout.readline()
    if not line:
        raise RuntimeError("Server process closed stdout unexpectedly")
    return json.loads(line.decode())


async def run_rpc(
    server_name: str,
    tool_name: str | None,
    kwargs: dict,
    repo_path: str | None = None,
    wait_timeout: int = 3,
    no_pretty: bool = False,
) -> None:
    indent: int | None = None if no_pretty else 2

    scripts = {
        "codebase_tools": _PROJECT_ROOT / "mcp_servers/codebase_tools/server.py",
        "memory": _PROJECT_ROOT / "mcp_servers/memory/server.py",
        "github_actions": _PROJECT_ROOT / "mcp_servers/github_actions/server.py",
    }

    env = os.environ.copy()
    if server_name == "codebase_tools":
        rp = repo_path or os.getcwd()
        env.setdefault("REPO_PATH", str(Path(rp).resolve()))
    if server_name == "memory":
        env.setdefault("GITHUB_REPOSITORY", "unknown/repo")

    missing = {
        "codebase_tools": [],
        "memory": [],
        "github_actions": ["GITHUB_TOKEN"],
    }.get(server_name, [])
    for var in missing:
        if var not in env:
            print(f"Error: Missing env var {var} for {server_name} RPC mode", file=sys.stderr)
            sys.exit(1)

    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(scripts[server_name]),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )

    try:
        # Initialize
        await _send_jsonrpc(process, "initialize", {"protocolVersion": "2024-11-05"})
        init_resp = await _recv_jsonrpc(process)
        if "error" in init_resp:
            print_json(init_resp, indent=indent, is_error=True)
            return

        # Wait for background init (codebase_tools spawns init_repo in thread)
        if server_name == "codebase_tools" and wait_timeout > 0:
            print(f"Waiting {wait_timeout}s for index build ...", file=sys.stderr)
            await asyncio.sleep(wait_timeout)

        if tool_name is None:
            # List tools
            await _send_jsonrpc(process, "tools/list", {})
            resp = await _recv_jsonrpc(process)
            tools = resp.get("result", {}).get("tools", [])
            print(f"{server_name} (RPC mode):")
            for tool in tools:
                print_tool_info(tool)
        else:
            # Call tool
            await _send_jsonrpc(process, "tools/call", {"name": tool_name, "arguments": kwargs})
            resp = await _recv_jsonrpc(process)

            if "error" in resp:
                print_json(resp, indent=indent, is_error=True)
                sys.exit(1)

            content = resp.get("result", {}).get("content", [])
            if content and isinstance(content, list):
                text = content[0].get("text", "")
                try:
                    print_json(json.loads(text), indent=indent)
                except (json.JSONDecodeError, TypeError):
                    print(text)
            else:
                print_json(resp.get("result", {}), indent=indent)
    finally:
        # Collect stderr for diagnostics
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                process.kill()


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Test MCP tools directly from the command line.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
        epilog="""Examples:
  %(prog)s codebase_tools --list
  %(prog)s codebase_tools --tool find_definitions --symbol_name Application
  %(prog)s codebase_tools --rpc --tool find_definitions --symbol_name main
  %(prog)s memory --tool memory_read --repo GabsFranke/myrepo
  %(prog)s github_actions --tool get_workflow_run_summary --owner X --repo Y --run_id Z
""",
    )
    parser.add_argument(
        "server",
        choices=["codebase_tools", "memory", "github_actions"],
        help="Which MCP server to target",
    )
    parser.add_argument(
        "--tool",
        type=str,
        default=None,
        help="Tool name to call (omit with --list to show all tools)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List available tools for the server instead of calling one",
    )
    parser.add_argument(
        "--rpc",
        action="store_true",
        help="Use JSON-RPC subprocess mode instead of direct function call",
    )
    parser.add_argument(
        "--repo-path",
        type=str,
        default=None,
        help="Repository path for codebase_tools (default: current directory)",
    )
    parser.add_argument(
        "--wait-timeout",
        type=int,
        default=3,
        help="Seconds to wait for codebase_tools index build in RPC mode (default: 3)",
    )
    parser.add_argument(
        "--no-pretty",
        action="store_true",
        help="Output compact JSON (no indentation)",
    )
    return parser


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    load_env()

    parser = build_parser()
    args, remaining = parser.parse_known_args()
    kwargs = parse_extra_args(remaining)

    if args.rpc:
        asyncio.run(
            run_rpc(
                server_name=args.server,
                tool_name=None if args.list else args.tool,
                kwargs=kwargs,
                repo_path=args.repo_path,
                wait_timeout=args.wait_timeout,
                no_pretty=args.no_pretty,
            )
        )
        return

    # Direct mode
    if args.list:
        if args.server == "codebase_tools":
            list_codebase_tools_direct()
        elif args.server == "memory":
            asyncio.run(list_memory_tools_direct())
        elif args.server == "github_actions":
            asyncio.run(list_github_actions_tools_direct())
        return

    if args.tool is None:
        parser.print_help()
        print("\nError: Specify a tool name or use --list", file=sys.stderr)
        sys.exit(1)

    if args.server == "codebase_tools":
        run_codebase_direct(args.tool, kwargs, args.repo_path)
    elif args.server == "memory":
        run_memory_direct(args.tool, kwargs)
    elif args.server == "github_actions":
        asyncio.run(run_github_actions_direct(args.tool, kwargs))


if __name__ == "__main__":
    main()
