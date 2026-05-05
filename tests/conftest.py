"""Pytest configuration and shared fixtures."""

import asyncio
import os
import re
import sys
from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio


class FakeSurrealDB:
    """A simple in-memory SurrealDB mock for tests.

    Stores rows inserted via INSERT INTO and returns them
    for SELECT queries. Supports basic RELATE graph edges and
    UPDATE operations. Enough to make SymbolIndex operations work
    without a real SurrealDB connection.
    """

    def __init__(self):
        self._tables: dict[str, list[dict]] = {}
        self._id_counter = 0

    def _next_id(self) -> str:
        self._id_counter += 1
        return f"symbol:{self._id_counter}"

    def _ensure_table(self, table: str) -> list[dict]:
        if table not in self._tables:
            self._tables[table] = []
        return self._tables[table]

    def query(self, sql: str, params: dict | None = None):
        params = params or {}
        sql_stripped = sql.strip()
        sql_upper = sql_stripped.upper()

        # DEFINE TABLE / FIELD / INDEX — schema DDL, ignore
        if sql_upper.startswith("DEFINE"):
            return []

        # INSERT INTO symbol $records (batch upsert from _upsert_symbols)
        if "INSERT INTO SYMBOL" in sql_upper and "$RECORDS" in sql_upper:
            records = params.get("records", [])
            table = self._ensure_table("symbol")
            for rec in records:
                if "id" not in rec:
                    rec["id"] = self._next_id()
                table.append(rec)
            return []

        # INSERT INTO _schema_meta { ... } (inline record syntax)
        if "INSERT INTO _SCHEMA_META" in sql_upper:
            record = self._parse_inline_record(sql_stripped, params)
            table = self._ensure_table("_schema_meta")
            table.clear()
            table.append(record)
            return []

        # INSERT INTO route { ... } or INSERT INTO tool_def { ... }
        if "INSERT INTO ROUTE" in sql_upper or "INSERT INTO TOOL_DEF" in sql_upper:
            record = self._parse_inline_record(sql_stripped, params)
            tname = "route" if "ROUTE" in sql_upper else "tool_def"
            if record:
                if "id" not in record:
                    record["id"] = self._next_id()
                self._ensure_table(tname).append(record)
            return []

        # Other INSERT — ignore
        if sql_upper.startswith("INSERT"):
            return []

        # DELETE FROM <table>
        if sql_upper.startswith("DELETE"):
            table_name = self._extract_table_name(sql_stripped)
            if table_name:
                t = self._ensure_table(table_name)
                repo = params.get("repo")
                fp = params.get("fp")
                if repo is not None and fp is not None:
                    self._tables[table_name] = [
                        r
                        for r in t
                        if not (r.get("repo") == repo and r.get("filepath") == fp)
                    ]
                elif repo is not None:
                    self._tables[table_name] = [r for r in t if r.get("repo") != repo]
                else:
                    self._tables[table_name] = []
            return []

        # UPDATE _schema_meta SET repo_commit = $hash
        if sql_upper.startswith("UPDATE"):
            self._handle_update(sql_stripped, params)
            return []

        # RELATE $src->calls->$tgt SET source_line = $line
        if sql_upper.startswith("RELATE"):
            self._handle_relate(sql_stripped, params)
            return []

        # SELECT ... FROM symbol ...
        if "SELECT" in sql_upper and "FROM SYMBOL" in sql_upper:
            return self._select_from_table("symbol", sql_stripped, params)

        # SELECT ... FROM _schema_meta ...
        if "SELECT" in sql_upper and "_SCHEMA_META" in sql_upper:
            return self._select_from_table("_schema_meta", sql_stripped, params)

        # SELECT ... FROM route ...
        if "SELECT" in sql_upper and "FROM ROUTE" in sql_upper:
            return self._select_from_table("route", sql_stripped, params)

        # SELECT ... FROM tool_def ...
        if "SELECT" in sql_upper and "FROM TOOL_DEF" in sql_upper:
            return self._select_from_table("tool_def", sql_stripped, params)

        # SELECT ... FROM calls/imports/inherits/contains_edge
        if "SELECT" in sql_upper:
            return self._select_from_edge(sql_stripped, params)

        # REMOVE TABLE — ignore
        return []

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _parse_inline_record(self, sql: str, params: dict) -> dict:
        """Parse SurrealQL inline record like { version: $ver, created_at: time::now() }."""
        record: dict = {}
        m = re.search(r"\{\s*(.+?)\s*\}", sql, re.DOTALL)
        if m:
            inner = m.group(1)
            for pair in inner.split(","):
                pair = pair.strip()
                if ":" in pair:
                    key, val = pair.split(":", 1)
                    key = key.strip()
                    val = val.strip()
                    if val.startswith("$"):
                        record[key] = params.get(val[1:])
                    elif val.startswith("time::now()"):
                        record[key] = "2024-01-01T00:00:00Z"
                    else:
                        stripped = val.strip("'\"")
                        try:
                            record[key] = int(stripped)
                        except ValueError:
                            try:
                                record[key] = float(stripped)
                            except ValueError:
                                record[key] = stripped
        return record

    def _extract_table_name(self, sql: str) -> str | None:
        """Extract table name from DELETE FROM <table>."""
        m = re.search(r"DELETE\s+FROM\s+(\w+)", sql, re.IGNORECASE)
        if m:
            return m.group(1).lower()
        return None

    def _handle_update(self, sql: str, params: dict) -> None:
        """Handle UPDATE _schema_meta SET field = $param."""
        table = self._ensure_table("_schema_meta")
        if not table:
            table.append({})
        m = re.search(r"SET\s+(\w+)\s*=\s*\$(\w+)", sql, re.IGNORECASE)
        if m:
            field = m.group(1).lower()
            param_name = m.group(2).lower()
            table[0][field] = params.get(param_name)

    def _handle_relate(self, sql: str, params: dict) -> None:
        """Store a graph edge from RELATE $src->table->$tgt SET ..."""
        m = re.search(r"RELATE\s+\$(\w+)\s*->(\w+)->\s*\$(\w+)", sql, re.IGNORECASE)
        if m:
            src_param = m.group(1).lower()
            edge_table = m.group(2).lower()
            tgt_param = m.group(3).lower()
            src_id = params.get(src_param)
            tgt_id = params.get(tgt_param)
            record: dict = {"in": src_id, "out": tgt_id}
            # Extract SET fields
            set_m = re.search(r"SET\s+(.+)$", sql, re.IGNORECASE)
            if set_m:
                set_clause = set_m.group(1)
                for pair in set_clause.split(","):
                    if "=" in pair:
                        k, v = pair.split("=", 1)
                        k = k.strip().lower()
                        v = v.strip()
                        if v.startswith("$"):
                            record[k] = params.get(v[1:])
                        else:
                            stripped = v.strip("'\"")
                            try:
                                record[k] = int(stripped)
                            except ValueError:
                                record[k] = stripped
            self._ensure_table(edge_table).append(record)

    def _select_from_table(self, table_name: str, sql: str, params: dict) -> list[dict]:
        """Handle SELECT ... FROM <table> with WHERE and LIMIT."""
        table = self._ensure_table(table_name)
        results = list(table)

        # Parse parameterized WHERE clauses: WHERE field = $param
        for m in re.finditer(r"(\w+)\s*=\s*\$(\w+)", sql):
            field = m.group(1).lower()
            param_name = m.group(2).lower()
            value = params.get(param_name)
            if value is not None:
                results = [r for r in results if r.get(field) == value]

        # Parse literal WHERE clauses: WHERE kind = 'definition'
        for m in re.finditer(r"(\w+)\s*=\s*'([^']+)'", sql):
            field = m.group(1).lower()
            value = m.group(2)
            results = [r for r in results if r.get(field) == value]

        # Parse IS NOT NULL conditions: WHERE field IS NOT NULL
        for m in re.finditer(r"(\w+)\s+IS\s+NOT\s+NULL", sql, re.IGNORECASE):
            field = m.group(1).lower()
            results = [r for r in results if r.get(field) is not None]

        # LIMIT
        limit_m = re.search(r"LIMIT\s+(\d+)", sql, re.IGNORECASE)
        if limit_m:
            results = results[: int(limit_m.group(1))]

        # $k param limit (used by indexing worker)
        k = params.get("k")
        if k is not None and isinstance(k, int):
            results = results[:k]

        return results

    def _select_from_edge(self, sql: str, params: dict) -> list[dict]:
        """Handle SELECT ... FROM edge_table WHERE in/out.field = $param."""
        edge_tables = ["calls", "imports", "inherits", "contains_edge"]
        table_name = None
        for t in edge_tables:
            if t.upper() in sql.upper():
                table_name = t
                break
        if not table_name:
            return []

        table = self._ensure_table(table_name)
        results = list(table)

        # Look up in/out symbol names from the symbol table
        sym_table = self._ensure_table("symbol")

        def _get_name(rid):
            if not rid:
                return ""
            for s in sym_table:
                if s.get("id") == rid:
                    return s.get("name", "")
            return rid if isinstance(rid, str) else ""

        # Annotate edges with in.name and out.name for filtering
        annotated = []
        for r in results:
            ann = dict(r)
            ann["in.name"] = _get_name(r.get("in"))
            ann["out.name"] = _get_name(r.get("out"))
            annotated.append(ann)

        # Parse WHERE in.field = $param or WHERE out.field = $param
        where_m = re.search(
            r"WHERE\s+(in|out)\.(\w+)\s*=\s*\$(\w+)", sql, re.IGNORECASE
        )
        if where_m:
            direction = where_m.group(1).lower()
            field = where_m.group(2).lower()
            param_name = where_m.group(3).lower()
            value = params.get(param_name)
            if value is not None:
                lookup = f"{direction}.{field}"
                annotated = [r for r in annotated if r.get(lookup) == value]

        # Multi-field WHERE (e.g., in.filepath = $fp AND in.line = $line AND in.name = $name)
        for m in re.finditer(r"(in|out)\.(\w+)\s*=\s*\$(\w+)", sql, re.IGNORECASE):
            direction = m.group(1).lower()
            field = m.group(2).lower()
            param_name = m.group(3).lower()
            value = params.get(param_name)
            if value is not None:
                lookup = f"{direction}.{field}"
                annotated = [r for r in annotated if r.get(lookup) == value]

        # Parse SELECT in/out.field AS alias
        select_m = re.search(
            r"SELECT\s+(in|out)\.(\w+)\s+AS\s+(\w+)", sql, re.IGNORECASE
        )
        if select_m:
            direction = select_m.group(1).lower()
            field = select_m.group(2).lower()
            alias = select_m.group(3).lower()
            lookup = f"{direction}.{field}"
            return [{alias: r.get(lookup, "")} for r in annotated]

        return annotated


# CRITICAL: Set test environment variables BEFORE any imports
# This allows worker.py and main.py to be imported without validation errors
os.environ.setdefault("GITHUB_APP_ID", "123456")
os.environ.setdefault("GITHUB_INSTALLATION_ID", "789012")
os.environ.setdefault(
    "GITHUB_PRIVATE_KEY",
    "-----BEGIN RSA PRIVATE KEY-----\ntest_key_content\n-----END RSA PRIVATE KEY-----",
)
os.environ.setdefault("GITHUB_WEBHOOK_SECRET", "test_webhook_secret")
os.environ.setdefault("ANTHROPIC_API_KEY", "test_anthropic_key")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")
os.environ.setdefault("LOG_LEVEL", "INFO")
os.environ.setdefault("PORT", "8000")

# CRITICAL: Mock dotenv BEFORE any imports that use pydantic-settings
sys.modules["dotenv"] = MagicMock()
sys.modules["dotenv.main"] = MagicMock()

# CRITICAL: Mock claude_agent_sdk BEFORE any imports that use it
# This allows sandbox_executor tests to run without the SDK installed
mock_sdk = MagicMock()
mock_sdk.AssistantMessage = MagicMock
mock_sdk.ClaudeAgentOptions = MagicMock
mock_sdk.ClaudeSDKClient = MagicMock
mock_sdk.HookMatcher = MagicMock
mock_sdk.ResultMessage = MagicMock
mock_sdk.TextBlock = MagicMock
sys.modules["claude_agent_sdk"] = mock_sdk

from redis.asyncio import Redis  # noqa: E402


@pytest.fixture(scope="session")
def event_loop() -> Generator[asyncio.AbstractEventLoop, None, None]:
    """Create an event loop for the test session."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Clear environment variables for each test, but preserve test defaults."""
    # Store the test defaults
    test_defaults = {
        "GITHUB_APP_ID": "123456",
        "GITHUB_INSTALLATION_ID": "789012",
        "GITHUB_PRIVATE_KEY": "-----BEGIN RSA PRIVATE KEY-----\ntest_key_content\n-----END RSA PRIVATE KEY-----",
        "GITHUB_WEBHOOK_SECRET": "test_webhook_secret",
        "ANTHROPIC_API_KEY": "test_anthropic_key",
        "REDIS_URL": "redis://localhost:6379",
        "LOG_LEVEL": "INFO",
        "PORT": "8000",
    }

    # Clear all config vars
    config_vars = [
        "REDIS_URL",
        "REDIS_PASSWORD",
        "QUEUE_NAME",
        "GITHUB_APP_ID",
        "GITHUB_INSTALLATION_ID",
        "GITHUB_PRIVATE_KEY",
        "GITHUB_WEBHOOK_SECRET",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_VERTEX_PROJECT_ID",
        "ANTHROPIC_VERTEX_REGION",
        "LOG_LEVEL",
        "PORT",
    ]
    for var in config_vars:
        monkeypatch.delenv(var, raising=False)

    # Restore test defaults
    for key, value in test_defaults.items():
        monkeypatch.setenv(key, value)


@pytest.fixture
def mock_redis() -> MagicMock:
    """Mock Redis client."""
    redis_mock = MagicMock(spec=Redis)
    redis_mock.ping = AsyncMock(return_value=True)
    redis_mock.publish = AsyncMock(return_value=1)
    redis_mock.pubsub = MagicMock()
    return redis_mock


@pytest.fixture
def mock_httpx_client() -> AsyncMock:
    """Mock httpx AsyncClient."""
    client = AsyncMock()
    client.get = AsyncMock()
    client.post = AsyncMock()
    client.patch = AsyncMock()
    client.delete = AsyncMock()
    return client


@pytest_asyncio.fixture
async def redis_client():
    """Create real Redis client for integration testing."""
    # Use password from Docker setup
    client = Redis(
        host="localhost",
        port=6379,
        password="S5e_V7kdhPOI9DNJfBvYodxJgeQCG8Xup2mG3rBPwDU",
        db=15,
        decode_responses=True,
    )
    try:
        await client.ping()
    except Exception as e:
        pytest.skip(f"Redis not available: {e}")
    yield client
    await client.aclose()


@pytest.fixture
def sample_github_webhook_payload() -> dict:
    """Sample GitHub webhook payload for testing."""
    return {
        "action": "opened",
        "pull_request": {
            "number": 123,
            "title": "Test PR",
            "body": "Test description",
            "user": {"login": "testuser"},
            "head": {"ref": "feature-branch", "sha": "abc123"},
            "base": {"ref": "main", "repo": {"full_name": "owner/repo"}},
        },
        "repository": {
            "full_name": "owner/repo",
            "name": "repo",
            "owner": {"login": "owner"},
        },
        "installation": {"id": 12345},
    }


@pytest.fixture
def sample_issue_comment_payload() -> dict:
    """Sample GitHub issue comment webhook payload."""
    return {
        "action": "created",
        "issue": {
            "number": 456,
            "title": "Test Issue",
            "body": "Issue description",
            "user": {"login": "testuser"},
            "pull_request": {
                "url": "https://api.github.com/repos/owner/repo/pulls/456"
            },
        },
        "comment": {
            "id": 789,
            "body": "/agent review this PR",
            "user": {"login": "testuser"},
        },
        "repository": {"full_name": "owner/repo"},
        "installation": {"id": 12345},
    }
