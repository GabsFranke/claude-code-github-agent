"""Semantic search tools connecting to Qdrant and Google Gemini.

Provides semantic_search() for natural language code queries.
Connects to a self-hosted Qdrant instance and uses gemini-embedding-001
for query embedding.
"""

import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_repo_path: Path | None = None
_qdrant_url: str | None = None
_gemini_api_key: str | None = None
_collection_name: str | None = None
_embedding_dimension: int = 1024


def init_config() -> None:
    """Initialize module state from environment variables."""
    global _repo_path, _qdrant_url, _gemini_api_key, _collection_name, _embedding_dimension

    _repo_path = Path(os.getenv("REPO_PATH", ".")).resolve()
    _qdrant_url = os.getenv("QDRANT_URL")
    _gemini_api_key = os.getenv("GEMINI_API_KEY")
    _embedding_dimension = int(os.getenv("EMBEDDING_DIMENSION", "1024"))

    repo = os.getenv("GITHUB_REPOSITORY", "")
    if repo:
        _collection_name = repo.replace("/", "__")
    else:
        _collection_name = None


def semantic_search(
    query: str,
    max_results: int = 10,
    file_filter: str | None = None,
    kind_filter: str | None = None,
) -> list[dict[str, Any]]:
    """Search the codebase using semantic similarity.

    Embeds the query via gemini-embedding-001, searches the Qdrant collection,
    and returns ranked results.

    Args:
        query: Natural language or code query describing what to find.
        max_results: Maximum results to return (capped at 50).
        file_filter: Optional glob pattern to filter by filepath.
        kind_filter: Optional filter by chunk kind ("function", "class", "method").

    Returns:
        List of dicts with file, name, kind, start_line, end_line,
        content, score.
    """
    max_results = min(max(1, max_results), 50)

    if not _qdrant_url or not _gemini_api_key:
        return []

    if not _collection_name:
        return []

    try:
        from google import genai
        from google.genai import types
        from qdrant_client import QdrantClient
        from qdrant_client.models import FieldCondition, Filter, MatchText

        # Embed the query
        client = genai.Client(api_key=_gemini_api_key)
        response = client.models.embed_content(
            model="gemini-embedding-001",
            contents=[query],
            config=types.EmbedContentConfig(
                output_dimensionality=_embedding_dimension,
            ),
        )
        query_embedding = response.embeddings[0].values

        # Connect to Qdrant and search
        qdrant_client = QdrantClient(url=_qdrant_url, timeout=10)

        # Build filter conditions
        conditions = []
        if file_filter:
            conditions.append(
                FieldCondition(key="filepath", match=MatchText(text=file_filter))
            )
        if kind_filter:
            conditions.append(
                FieldCondition(key="kind", match=MatchText(text=kind_filter))
            )

        search_filter = Filter(must=conditions) if conditions else None

        results = qdrant_client.query_points(
            collection_name=_collection_name,
            query=query_embedding,
            limit=max_results,
            query_filter=search_filter,
            with_payload=True,
        )

        output: list[dict[str, Any]] = []
        for point in results.points:
            payload = point.payload or {}
            output.append(
                {
                    "file": payload.get("filepath", ""),
                    "name": payload.get("name", ""),
                    "kind": payload.get("kind", ""),
                    "start_line": payload.get("start_line", 0),
                    "end_line": payload.get("end_line", 0),
                    "content": payload.get("content", ""),
                    "score": point.score,
                }
            )

        return output

    except ImportError:
        logger.warning(
            "qdrant-client or google-genai not installed. "
            "Semantic search is unavailable."
        )
        return []
    except Exception as e:
        logger.error(f"Semantic search failed: {e}", exc_info=True)
        return []
