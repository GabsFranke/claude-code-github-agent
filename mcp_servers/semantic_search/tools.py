"""Semantic search tools connecting to Qdrant and Google Gemini.

Provides semantic_search() for natural language code queries.
Connects to a self-hosted Qdrant instance and uses gemini-embedding-001
for query embedding.
"""

import fnmatch
import logging
import os
from pathlib import Path
from typing import Any

from shared.file_tree import collection_name_for_repo

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_repo_path: Path | None = None
_qdrant_url: str | None = None
_gemini_api_key: str | None = None
_collection_name: str | None = None
_embedding_dimension: int = 1024

# Singleton clients (initialized once in init_config, reused across calls)
_genai_client = None
_qdrant_client = None


def init_config() -> None:
    """Initialize module state from environment variables.

    Creates singleton clients for Gemini and Qdrant that are reused
    across all search requests to avoid connection leaks.
    """
    global _repo_path, _qdrant_url, _gemini_api_key, _collection_name
    global _embedding_dimension, _genai_client, _qdrant_client

    _repo_path = Path(os.getenv("REPO_PATH", ".")).resolve()
    _qdrant_url = os.getenv("QDRANT_URL")
    _gemini_api_key = os.getenv("GEMINI_API_KEY")
    _embedding_dimension = int(os.getenv("EMBEDDING_DIMENSION", "1024"))

    repo = os.getenv("GITHUB_REPOSITORY", "")
    if repo:
        _collection_name = collection_name_for_repo(repo)
    else:
        _collection_name = None

    # Initialize singleton clients if both services are configured
    if _qdrant_url and _gemini_api_key:
        try:
            from google import genai

            _genai_client = genai.Client(api_key=_gemini_api_key)
        except ImportError:
            logger.warning("google-genai not installed. Semantic search unavailable.")

        try:
            from qdrant_client import QdrantClient

            _qdrant_client = QdrantClient(url=_qdrant_url, timeout=10)
        except ImportError:
            logger.warning("qdrant-client not installed. Semantic search unavailable.")


def cleanup() -> None:
    """Close singleton clients. Called on server shutdown."""
    global _qdrant_client
    if _qdrant_client is not None:
        try:
            _qdrant_client.close()
        except Exception:
            pass
        _qdrant_client = None


def semantic_search(
    query: str,
    max_results: int = 10,
    file_filter: str | None = None,
    kind_filter: str | None = None,
) -> dict[str, Any]:
    """Search the codebase using semantic similarity.

    Embeds the query via gemini-embedding-001, searches the Qdrant collection,
    and returns ranked results.

    Args:
        query: Natural language or code query describing what to find.
        max_results: Maximum results to return (capped at 50).
        file_filter: Optional glob pattern to filter by filepath (e.g. "shared/*.py").
        kind_filter: Optional filter by chunk kind ("function", "class", "method").

    Returns:
        Dict with "results" list on success, or "error" key with message on failure.
        Each result has: file, name, kind, start_line, end_line, content, score.
    """
    max_results = min(max(1, max_results), 50)

    if not _qdrant_url or not _gemini_api_key:
        return {
            "error": "Semantic search is not configured (missing QDRANT_URL or GEMINI_API_KEY).",
            "results": [],
        }

    if not _collection_name:
        return {
            "error": "Semantic search is not configured (missing GITHUB_REPOSITORY).",
            "results": [],
        }

    if _genai_client is None or _qdrant_client is None:
        return {
            "error": "Semantic search clients not initialized (check import availability).",
            "results": [],
        }

    try:
        from google.genai import types
        from qdrant_client.models import FieldCondition, Filter, MatchText

        # Embed the query using singleton client
        response = _genai_client.models.embed_content(
            model="gemini-embedding-001",
            contents=[query],
            config=types.EmbedContentConfig(
                output_dimensionality=_embedding_dimension,
            ),
        )

        if not response.embeddings or not response.embeddings[0].values:
            return {
                "error": "Query embedding returned no results.",
                "results": [],
            }

        query_embedding = response.embeddings[0].values

        # Build filter conditions (only kind_filter uses Qdrant-side filtering;
        # file_filter is done client-side with glob matching since Qdrant's
        # MatchText does token-based matching, not glob patterns)
        conditions = []
        if kind_filter:
            conditions.append(
                FieldCondition(key="kind", match=MatchText(text=kind_filter))
            )

        search_filter = Filter(must=conditions) if conditions else None

        # Over-fetch to compensate for client-side file_filter
        fetch_limit = max_results * 3 if file_filter else max_results

        results = _qdrant_client.query_points(
            collection_name=_collection_name,
            query=query_embedding,
            limit=fetch_limit,
            query_filter=search_filter,
            with_payload=True,
        )

        output: list[dict[str, Any]] = []
        for point in results.points:
            payload = point.payload or {}
            filepath = payload.get("filepath", "")

            # Client-side glob filtering for file_filter
            if file_filter and not fnmatch.fnmatch(filepath, file_filter):
                continue

            output.append(
                {
                    "file": filepath,
                    "name": payload.get("name", ""),
                    "kind": payload.get("kind", ""),
                    "start_line": payload.get("start_line", 0),
                    "end_line": payload.get("end_line", 0),
                    "content": payload.get("content", ""),
                    "score": point.score,
                }
            )

            if len(output) >= max_results:
                break

        return {"results": output}

    except ImportError:
        return {
            "error": "qdrant-client or google-genai not installed. Semantic search is unavailable.",
            "results": [],
        }
    except Exception as e:
        logger.error(f"Semantic search failed: {e}", exc_info=True)
        return {
            "error": f"Semantic search failed: {type(e).__name__}: {e}",
            "results": [],
        }
