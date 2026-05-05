"""SurrealDB singleton connection management.

Provides sync and async clients for the code intelligence pipeline.
SurrealDB replaces Qdrant (vector search), JSON file cache (persistence),
and in-memory lookup dicts (graph traversal) with a single multi-model
database.

Usage:
    from shared.surrealdb_client import get_surreal, init_surrealdb

    init_surrealdb("ws://localhost:8000/rpc", "root", "root", "bot", "codebase")
    db = get_surreal()
    db.query("SELECT * FROM symbol WHERE name = 'App'")
"""

import asyncio
import logging
import os
import threading
from typing import Any

from surrealdb import AsyncSurreal, Surreal

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_surreal: Any = None
_async_surreal: Any = None
_initialized: bool = False

# Thread-safety locks for initialization
_init_lock = threading.Lock()
_async_init_lock: asyncio.Lock | None = None


def _get_async_init_lock() -> asyncio.Lock:
    """Lazily create the async init lock (cannot be created before an event loop exists)."""
    global _async_init_lock
    if _async_init_lock is None:
        _async_init_lock = asyncio.Lock()
    return _async_init_lock


# Default connection parameters
DEFAULT_URL = os.getenv("SURREALDB_URL", "ws://localhost:8000/rpc")
DEFAULT_USER = os.getenv("SURREALDB_USER", "root")
DEFAULT_PASS = os.getenv("SURREALDB_PASS", "root")
DEFAULT_NS = os.getenv("SURREALDB_NS", "bot")
DEFAULT_DB = os.getenv("SURREALDB_DB", "codebase")

# Schema version — bump when schema changes
SCHEMA_VERSION = 3


def init_surrealdb(
    url: str | None = None,
    user: str | None = None,
    password: str | None = None,
    ns: str | None = None,
    db: str | None = None,
) -> Any:
    """Initialize the SurrealDB connection and return a connected client.

    Must be called once before using get_surreal() or get_async_surreal().
    Idempotent — subsequent calls return the existing client.
    Thread-safe — uses a lock to prevent concurrent initialization.

    Args:
        url: WebSocket URL (default: SURREALDB_URL env or ws://localhost:8000/rpc)
        user: Username (default: SURREALDB_USER env or "root")
        password: Password (default: SURREALDB_PASS env or "root")
        ns: Namespace (default: SURREALDB_NS env or "bot")
        db: Database name (default: SURREALDB_DB env or "codebase")

    Returns:
        Connected sync Surreal client.
    """
    global _surreal, _initialized  # noqa: PLW0603

    with _init_lock:
        if _surreal is not None:
            return _surreal

        _url = url or DEFAULT_URL
        _user = user or DEFAULT_USER
        _pass = password or DEFAULT_PASS
        _ns = ns or DEFAULT_NS
        _db = db or DEFAULT_DB

        _surreal = Surreal(_url)
        _surreal.signin({"username": _user, "password": _pass})
        _surreal.use(_ns, _db)
        _initialized = True
        logger.info("SurrealDB connected: %s ns=%s db=%s", _url, _ns, _db)
        return _surreal


async def init_async_surrealdb(
    url: str | None = None,
    user: str | None = None,
    password: str | None = None,
    ns: str | None = None,
    db: str | None = None,
) -> Any:
    """Initialize the async SurrealDB client.

    Thread-safe — uses an async lock to prevent concurrent initialization.

    Args:
        url: WebSocket URL
        user: Username
        password: Password
        ns: Namespace
        db: Database name

    Returns:
        Connected AsyncSurreal client.
    """
    global _async_surreal  # noqa: PLW0603

    async with _get_async_init_lock():
        if _async_surreal is not None:
            return _async_surreal

        _url = url or DEFAULT_URL
        _user = user or DEFAULT_USER
        _pass = password or DEFAULT_PASS
        _ns = ns or DEFAULT_NS
        _db = db or DEFAULT_DB

        _async_surreal = AsyncSurreal(_url)
        await _async_surreal.signin({"username": _user, "password": _pass})
        await _async_surreal.use(_ns, _db)
        logger.info("SurrealDB async connected: %s ns=%s db=%s", _url, _ns, _db)
        return _async_surreal


def get_surreal() -> Any:
    """Get the singleton sync SurrealDB client.

    Returns:
        Connected Surreal client.

    Raises:
        RuntimeError: If init_surrealdb() has not been called yet.
    """
    if _surreal is None:
        raise RuntimeError("SurrealDB not initialized. Call init_surrealdb() first.")
    return _surreal


async def get_async_surreal() -> Any:
    """Get the singleton async SurrealDB client.

    Returns:
        Connected AsyncSurreal client.

    Raises:
        RuntimeError: If init_async_surrealdb() has not been called yet.
    """
    if _async_surreal is None:
        raise RuntimeError(
            "SurrealDB async not initialized. " "Call init_async_surrealdb() first."
        )
    return _async_surreal


def is_initialized() -> bool:
    """Check whether a SurrealDB connection has been established."""
    return _initialized and _surreal is not None


def close_surreal() -> None:
    """Close all singleton SurrealDB clients."""
    global _surreal, _async_surreal, _initialized  # noqa: PLW0603

    if _surreal is not None:
        try:
            _surreal.close()
        except Exception as e:
            logger.debug("Error closing SurrealDB connection: %s", e)
        _surreal = None
    if _async_surreal is not None:
        _async_surreal = None
    _initialized = False


# ---------------------------------------------------------------------------
# Schema management
# ---------------------------------------------------------------------------

SCHEMA_SURREALQL = """
-- Schema version tracking
DEFINE TABLE _schema_meta SCHEMAFULL;
DEFINE FIELD version ON _schema_meta TYPE int;
DEFINE FIELD created_at ON _schema_meta TYPE string;
DEFINE FIELD repo_commit ON _schema_meta TYPE option<string>;

-- Symbol definitions (functions, classes, methods, variables)
DEFINE TABLE symbol SCHEMAFULL;
DEFINE FIELD name ON symbol TYPE string;
DEFINE FIELD kind ON symbol TYPE string;
DEFINE FIELD category ON symbol TYPE string;
DEFINE FIELD filepath ON symbol TYPE string;
DEFINE FIELD line ON symbol TYPE int;
DEFINE FIELD end_line ON symbol TYPE int;
DEFINE FIELD language ON symbol TYPE string;
DEFINE FIELD repo ON symbol TYPE option<string>;
DEFINE FIELD signature ON symbol TYPE option<string>;
DEFINE FIELD embedding ON symbol TYPE option<array<float>>;
DEFINE FIELD content ON symbol TYPE option<string>;

DEFINE INDEX idx_symbol_name ON symbol FIELDS name;
DEFINE INDEX idx_symbol_file ON symbol FIELDS filepath;
DEFINE INDEX idx_symbol_kind ON symbol FIELDS kind;
DEFINE INDEX idx_symbol_lang ON symbol FIELDS language;
DEFINE INDEX idx_symbol_repo ON symbol FIELDS repo;
DEFINE INDEX idx_symbol_embedding ON symbol FIELDS embedding HNSW DIMENSION 1024 DIST COSINE;

-- Graph edges (TYPE RELATION enables -> and <- graph traversal)
DEFINE TABLE calls SCHEMAFULL TYPE RELATION IN symbol OUT symbol;
DEFINE FIELD source_line ON calls TYPE int;

DEFINE TABLE imports SCHEMAFULL TYPE RELATION IN symbol OUT symbol;
DEFINE FIELD source_line ON imports TYPE int;
DEFINE FIELD resolved_file ON imports TYPE option<string>;

DEFINE TABLE inherits SCHEMAFULL TYPE RELATION IN symbol OUT symbol;
DEFINE FIELD source_line ON inherits TYPE int;

DEFINE TABLE contains_edge SCHEMAFULL TYPE RELATION IN symbol OUT symbol;

-- API route definitions (extracted from FastAPI/Flask/Django decorators)
DEFINE TABLE route SCHEMAFULL;
DEFINE FIELD path ON route TYPE string;
DEFINE FIELD method ON route TYPE string;
DEFINE FIELD handler ON route TYPE string;
DEFINE FIELD filepath ON route TYPE string;
DEFINE FIELD line ON route TYPE int;
DEFINE FIELD framework ON route TYPE string;
DEFINE FIELD description ON route TYPE option<string>;
DEFINE INDEX idx_route_framework ON route FIELDS framework;
DEFINE INDEX idx_route_filepath ON route FIELDS filepath;

-- MCP tool definitions (extracted from MCP server JSON schemas)
DEFINE TABLE tool_def SCHEMAFULL;
DEFINE FIELD name ON tool_def TYPE string;
DEFINE FIELD description ON tool_def TYPE string;
DEFINE FIELD server_file ON tool_def TYPE string;
DEFINE FIELD server_name ON tool_def TYPE string;
DEFINE FIELD required_params ON tool_def TYPE option<array<string>>;
DEFINE INDEX idx_tool_server ON tool_def FIELDS server_name;
"""


def apply_schema(db: Any = None) -> None:
    """Apply the code intelligence schema to SurrealDB.

    Idempotent — skips if schema version already matches SCHEMA_VERSION.
    """
    if db is None:
        db = get_surreal()

    # Check existing schema version
    try:
        result = db.query("SELECT version FROM _schema_meta LIMIT 1")
        rows = _raw_result_rows(result)
        if rows and len(rows) > 0 and rows[0].get("version") == SCHEMA_VERSION:
            return
    except Exception as e:
        logger.warning("Schema version check failed, will re-apply schema: %s", e)

    # Apply schema statement by statement
    try:
        for statement in SCHEMA_SURREALQL.strip().split(";\n"):
            stmt = statement.strip()
            if stmt:
                db.query(stmt + ";")

        db.query(
            "INSERT INTO _schema_meta { version: $ver, created_at: time::now() };",
            {"ver": SCHEMA_VERSION},
        )
        logger.info("Applied schema version %d", SCHEMA_VERSION)
    except Exception as e:
        logger.warning("Schema application failed: %s", e)


def reset_schema(db: Any = None) -> None:
    """Drop all code intelligence tables. Used in tests."""
    if db is None:
        db = get_surreal()

    tables = [
        "symbol",
        "calls",
        "imports",
        "inherits",
        "contains_edge",
        "route",
        "tool_def",
        "_schema_meta",
    ]
    for table in tables:
        try:
            db.query(f"REMOVE TABLE {table}")
        except Exception:
            pass


def _raw_result_rows(result: object) -> list[dict]:
    """Extract rows from a SurrealDB query result across known response shapes."""
    if result is None:
        return []
    if isinstance(result, list):
        items = result
    elif isinstance(result, dict) and "result" in result:
        items = result["result"]
    else:
        return []
    if isinstance(items, list):
        return [r for r in items if isinstance(r, dict)]
    return []  # type: ignore[unreachable]
