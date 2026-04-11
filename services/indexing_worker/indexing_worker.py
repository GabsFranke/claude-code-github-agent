"""Background worker that chunks repos, generates embeddings, and stores in Qdrant.

Subscribes to two event sources:
  1. agent:sync:events (pub/sub) — triggers full repo indexing on sync completion
  2. agent:indexing:requests (list) — processes explicit indexing requests

Supports incremental indexing via git diff and an embedding cache to avoid
re-embedding unchanged content.
"""

import asyncio
import hashlib
import json
import logging
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

from shared import setup_graceful_shutdown
from shared.chunker import chunk_repo
from shared.logging_utils import setup_logging
from shared.queue import RedisQueue

# Configure logging
setup_logging(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

# Configuration
INDEXING_ENABLED = os.getenv("INDEXING_ENABLED", "true").lower() == "true"
QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "gemini-embedding-001")
EMBEDDING_DIMENSION = int(os.getenv("EMBEDDING_DIMENSION", "1024"))
EMBEDDING_BATCH_SIZE = int(os.getenv("EMBEDDING_BATCH_SIZE", "20"))

# Global state
shutdown_event = asyncio.Event()

# Redis key patterns
_META_KEY = "agent:indexing:meta:{repo}"  # Hash: field=ref, value=JSON
_CACHE_KEY = (
    f"agent:indexing:cache:{EMBEDDING_MODEL}"  # Hash: field=content_hash, value=JSON
)


# ---------------------------------------------------------------------------
# Qdrant helpers
# ---------------------------------------------------------------------------


def _collection_name(repo: str) -> str:
    """Convert repo slug to Qdrant collection name."""
    return repo.replace("/", "__")


async def ensure_collection(repo: str) -> str:
    """Create or verify a Qdrant collection for the given repo.

    Returns the collection name.
    """
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, VectorParams

    collection = _collection_name(repo)
    client = QdrantClient(url=QDRANT_URL, timeout=10)

    try:
        collections = client.get_collections().collections
        existing = {c.name for c in collections}

        if collection not in existing:
            client.create_collection(
                collection_name=collection,
                vectors_config=VectorParams(
                    size=EMBEDDING_DIMENSION,
                    distance=Distance.COSINE,
                ),
            )
            logger.info(f"Created Qdrant collection: {collection}")
    except Exception as e:
        logger.error(f"Failed to ensure collection for {repo}: {e}")
        raise
    finally:
        client.close()

    return collection


# ---------------------------------------------------------------------------
# Embedding cache helpers
# ---------------------------------------------------------------------------


def _content_hash(content: str) -> str:
    """SHA-256 hash of chunk content for cache key."""
    return hashlib.sha256(content.encode()).hexdigest()


async def _get_cached_embeddings(
    redis_client, contents: list[str]
) -> tuple[list[list[float] | None], list[int]]:
    """Check embedding cache, return results and indices of misses.

    Uses Redis pipeline for batch lookup.
    """
    hashes = [_content_hash(c) for c in contents]
    results: list[list[float] | None] = [None] * len(contents)
    miss_indices: list[int] = []

    try:
        pipe = redis_client.pipeline()
        for h in hashes:
            pipe.hget(_CACHE_KEY, h)
        cached = await pipe.execute()

        for i, raw in enumerate(cached):
            if raw is not None:
                results[i] = json.loads(raw)
            else:
                miss_indices.append(i)
    except Exception as e:
        logger.warning(f"Embedding cache lookup failed: {e}")
        return [None] * len(contents), list(range(len(contents)))

    return results, miss_indices


async def _cache_embeddings(
    redis_client, contents: list[str], embeddings: list[list[float]]
) -> None:
    """Store embeddings in Redis hash cache."""
    try:
        mapping = {}
        for content, embedding in zip(contents, embeddings):
            mapping[_content_hash(content)] = json.dumps(embedding)
        if mapping:
            await redis_client.hset(_CACHE_KEY, mapping=mapping)  # type: ignore[misc]
    except Exception as e:
        logger.warning(f"Embedding cache store failed: {e}")


# ---------------------------------------------------------------------------
# Embedding helpers
# ---------------------------------------------------------------------------


async def _embed_texts(texts: list[str]) -> list[list[float]]:
    """Call Gemini embedding API for a batch of texts.

    Handles batching at EMBEDDING_BATCH_SIZE items per API call,
    with retry + exponential backoff for rate limits (429).
    """
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=GEMINI_API_KEY)
    all_embeddings: list[list[float]] = []
    max_retries = 5

    for i in range(0, len(texts), EMBEDDING_BATCH_SIZE):
        batch = texts[i : i + EMBEDDING_BATCH_SIZE]
        for attempt in range(max_retries):
            try:
                result = await asyncio.to_thread(
                    client.models.embed_content,
                    model=EMBEDDING_MODEL,
                    contents=batch,
                    config=types.EmbedContentConfig(
                        output_dimensionality=EMBEDDING_DIMENSION,
                    ),
                )
                embeddings = [e.values for e in (result.embeddings or []) if e.values]
                all_embeddings.extend(embeddings)
                logger.debug(
                    f"Embedded batch {i // EMBEDDING_BATCH_SIZE + 1}: {len(batch)} texts"
                )
                break
            except Exception as e:
                is_rate_limit = "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e)
                if is_rate_limit and attempt < max_retries - 1:
                    delay = 2 ** (attempt + 1)  # 2s, 4s, 8s, 16s
                    logger.warning(
                        f"Rate limited on batch at offset {i}, "
                        f"retrying in {delay}s (attempt {attempt + 1}/{max_retries})"
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.error(f"Embedding batch failed at offset {i}: {e}")
                    raise

    return all_embeddings


async def batch_embed(texts: list[str], redis_client=None) -> list[list[float]]:
    """Embed texts with caching. Checks Redis cache first, only calls API for misses."""
    if not texts:
        return []

    # Check cache
    if redis_client:
        cached_results, miss_indices = await _get_cached_embeddings(redis_client, texts)
    else:
        cached_results = [None] * len(texts)
        miss_indices = list(range(len(texts)))

    cache_hits = len(texts) - len(miss_indices)
    if cache_hits:
        logger.info(f"Embedding cache: {cache_hits}/{len(texts)} hits")

    if not miss_indices:
        # Everything cached
        return cached_results  # type: ignore[return-value]

    # Embed only the misses
    miss_texts = [texts[i] for i in miss_indices]

    # Log which chunks missed (for diagnosing persistent misses)
    for idx in miss_indices:
        preview = texts[idx][:80].replace("\n", "\\n")
        logger.debug(
            f"Cache miss [{idx}]: hash={_content_hash(texts[idx])[:12]}... "
            f"preview={preview!r}..."
        )
    new_embeddings = await _embed_texts(miss_texts)

    # Store new embeddings in cache
    if redis_client and new_embeddings:
        await _cache_embeddings(redis_client, miss_texts, new_embeddings)

    # Merge cached + new
    results: list[list[float]] = []
    miss_idx = 0
    for i in range(len(texts)):
        if cached_results[i] is not None:
            results.append(cached_results[i])  # type: ignore[arg-type]
        else:
            results.append(new_embeddings[miss_idx])
            miss_idx += 1

    return results


# ---------------------------------------------------------------------------
# Upsert helpers
# ---------------------------------------------------------------------------


def _point_id(filepath: str, start_line: int, kind: str, name: str) -> str:
    """Deterministic UUID v5 point ID for deduplication."""
    raw = f"{filepath}:{start_line}:{kind}:{name}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, raw))


async def upsert_chunks(
    collection: str,
    chunks: list,
    embeddings: list[list[float]],
    commit_hash: str,
    removed_files: list[str] | None = None,
    full_index: bool = True,
) -> int:
    """Upsert chunk embeddings into Qdrant.

    Args:
        collection: Qdrant collection name.
        chunks: List of Chunk objects.
        embeddings: Corresponding embedding vectors.
        commit_hash: Current commit hash.
        removed_files: Files to delete from index (incremental mode).
        full_index: If True, delete all stale points after upsert.
    """
    from qdrant_client import QdrantClient
    from qdrant_client.models import FieldCondition, Filter, MatchValue, PointStruct

    client = QdrantClient(url=QDRANT_URL, timeout=30)

    try:
        from datetime import UTC, datetime

        # For incremental mode: delete all existing chunks for files being re-indexed.
        # This handles line shifts and renamed functions within changed files.
        if not full_index:
            changed_filepaths = list({c.filepath for c in chunks})
            for filepath in changed_filepaths:
                client.delete(
                    collection_name=collection,
                    points_selector=Filter(
                        must=[
                            FieldCondition(
                                key="filepath",
                                match=MatchValue(value=filepath),
                            )
                        ]
                    ),
                )

        points = []
        for chunk, embedding in zip(chunks, embeddings):
            point = PointStruct(
                id=_point_id(chunk.filepath, chunk.start_line, chunk.kind, chunk.name),
                vector=embedding,
                payload={
                    "filepath": chunk.filepath,
                    "name": chunk.name,
                    "kind": chunk.kind,
                    "language": chunk.language,
                    "parent": chunk.parent,
                    "start_line": chunk.start_line,
                    "end_line": chunk.end_line,
                    "content": chunk.content[:2000],
                    "commit_hash": commit_hash,
                    "indexed_at": datetime.now(UTC).isoformat(),
                },
            )
            points.append(point)

        # Upsert in batches of 100
        for i in range(0, len(points), 100):
            batch = points[i : i + 100]
            client.upsert(collection_name=collection, points=batch)

        logger.info(f"Upserted {len(points)} points into {collection}")

        # Cleanup stale points
        try:
            if full_index:
                # Full index: delete everything from previous commits
                client.delete(
                    collection_name=collection,
                    points_selector=Filter(
                        must_not=[
                            FieldCondition(
                                key="commit_hash",
                                match=MatchValue(value=commit_hash),
                            )
                        ]
                    ),
                )
                logger.info(
                    f"Cleaned up stale points in {collection} "
                    f"(keeping commit {commit_hash[:8]})"
                )
            elif removed_files:
                # Incremental: only delete chunks for removed files
                for filepath in removed_files:
                    client.delete(
                        collection_name=collection,
                        points_selector=Filter(
                            must=[
                                FieldCondition(
                                    key="filepath",
                                    match=MatchValue(value=filepath),
                                )
                            ]
                        ),
                    )
                logger.info(
                    f"Deleted chunks for {len(removed_files)} removed files in {collection}"
                )
        except Exception as e:
            logger.warning(f"Stale point cleanup failed for {collection}: {e}")

        return len(points)
    finally:
        client.close()


# ---------------------------------------------------------------------------
# Git diff helpers
# ---------------------------------------------------------------------------


async def _git_diff_files(
    worktree: str,
    old_commit: str,
    new_commit: str,
    deleted_only: bool = False,
) -> list[str]:
    """Get changed or deleted files between two commits.

    For deleted_only=True, uses --name-status with --diff-filter=DR to also
    catch old paths of renamed files. For changed files, uses --diff-filter=ACMR.

    Args:
        worktree: Path to git worktree.
        old_commit: Previous commit hash.
        new_commit: Current commit hash.
        deleted_only: If True, return old paths of deleted AND renamed files.

    Returns:
        List of relative file paths.
    """
    if deleted_only:
        # Use --name-status to parse old paths from renames (R\told\tnew)
        # and pure deletions (D\tpath)
        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                "diff",
                "--name-status",
                "--diff-filter=DR",
                old_commit,
                new_commit,
                cwd=worktree,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            if proc.returncode == 0 and stdout:
                files = []
                for line in stdout.decode().splitlines():
                    parts = line.split("\t")
                    if not parts:
                        continue
                    status = parts[0][0]  # First char: D or R
                    if status == "D":
                        files.append(parts[1].strip())
                    elif status == "R":
                        # Rename: old_path is the SECOND field
                        files.append(parts[1].strip())
                return [f for f in files if f]
            if stderr:
                logger.warning(f"git diff failed: {stderr.decode().strip()}")
        except Exception as e:
            logger.warning(f"git diff error: {e}")
        return []

    # Changed files: Added, Copied, Modified, Renamed
    diff_filter = "ACMR"
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "diff",
            "--name-only",
            f"--diff-filter={diff_filter}",
            old_commit,
            new_commit,
            cwd=worktree,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode == 0 and stdout:
            files = [f.strip() for f in stdout.decode().splitlines() if f.strip()]
            return files
        if stderr:
            logger.warning(f"git diff failed: {stderr.decode().strip()}")
    except Exception as e:
        logger.warning(f"git diff error: {e}")
    return []


# ---------------------------------------------------------------------------
# Metadata helpers
# ---------------------------------------------------------------------------


async def _migrate_meta_key(redis_client, key: str) -> None:
    """Delete old string-format metadata key so it can be recreated as a hash."""
    try:
        await redis_client.delete(key)
        logger.info(f"Migrated metadata key {key} from string to hash")
    except Exception:
        pass


async def _get_previous_commit(redis_client, repo: str, ref: str) -> str | None:
    """Get the last indexed commit hash for this (repo, ref)."""
    if not redis_client:
        return None
    key = _META_KEY.format(repo=repo)
    try:
        raw = await redis_client.hget(key, ref)
        if raw:
            meta = json.loads(raw)
            return str(meta.get("indexed_commit", "")) or None
    except Exception as e:
        if "WRONGTYPE" in str(e):
            await _migrate_meta_key(redis_client, key)
        else:
            logger.warning(f"Failed to read indexing metadata: {e}")
    return None


async def _update_indexing_metadata(
    repo: str,
    collection: str,
    commit_hash: str,
    chunk_count: int,
    ref: str,
    redis_client=None,
) -> None:
    """Store indexing metadata in Redis hash keyed by (repo, ref)."""
    if not redis_client:
        return
    try:
        meta = json.dumps(
            {
                "collection_name": collection,
                "indexed_commit": commit_hash,
                "chunk_count": chunk_count,
            }
        )
        key = _META_KEY.format(repo=repo)
        await redis_client.hset(key, ref, meta)  # type: ignore[misc]
    except Exception as e:
        if "WRONGTYPE" in str(e):
            await _migrate_meta_key(redis_client, key)
            await redis_client.hset(key, ref, meta)  # type: ignore[misc]
        else:
            logger.warning(f"Failed to update indexing metadata: {e}")


# ---------------------------------------------------------------------------
# Indexing pipeline
# ---------------------------------------------------------------------------


async def process_indexing_job(message: dict, redis_client=None) -> None:
    """Process a single indexing request.

    Supports incremental indexing via git diff when previous metadata exists.
    """
    repo = message.get("repo")
    ref = message.get("ref", "main")

    if not repo:
        logger.warning("Indexing job missing 'repo', skipping")
        return

    if not GEMINI_API_KEY:
        logger.warning("GEMINI_API_KEY not set, skipping indexing")
        return

    logger.info(
        f"Processing indexing job for {repo} (trigger: {message.get('trigger', 'unknown')})"
    )

    # Ensure Qdrant collection exists
    collection = await ensure_collection(repo)

    # Create worktree from bare repo cache
    worktree = None
    try:
        worktree = await _create_worktree(repo, ref)
        if not worktree:
            logger.error(f"Could not create worktree for {repo}")
            return

        # Get commit hash
        commit_hash = await _get_commit_hash(worktree)

        # Check for previous index to determine incremental vs full
        previous_commit = await _get_previous_commit(redis_client, repo, ref)
        full_index = True
        changed_files: list[str] | None = None
        removed_files: list[str] = []

        if previous_commit and previous_commit != commit_hash:
            # Incremental: diff to find changed + removed files
            changed_files = await _git_diff_files(
                worktree, previous_commit, commit_hash
            )
            removed_files = await _git_diff_files(
                worktree, previous_commit, commit_hash, deleted_only=True
            )

            if not changed_files and not removed_files:
                logger.info(
                    f"No changes detected for {repo} on {ref} "
                    f"({previous_commit[:8]}..{commit_hash[:8]}), skipping"
                )
                # Still update metadata to refresh the commit hash
                await _update_indexing_metadata(
                    repo, collection, commit_hash, 0, ref, redis_client
                )
                return

            full_index = False
            logger.info(
                f"Incremental index for {repo} on {ref}: "
                f"{len(changed_files)} changed, {len(removed_files)} removed "
                f"({previous_commit[:8]}..{commit_hash[:8]})"
            )
        else:
            logger.info(f"Full index for {repo} on {ref} (commit {commit_hash[:8]})")

        # Chunk the repo (full or incremental)
        chunks = await asyncio.to_thread(chunk_repo, Path(worktree), changed_files)

        if not chunks:
            logger.info(f"No chunks produced for {repo}, skipping")
            return

        logger.info(f"Produced {len(chunks)} chunks for {repo}")

        # Batch embed with cache (includes context headers)
        texts = [c.embed_text for c in chunks]
        embeddings = await batch_embed(texts, redis_client=redis_client)

        # Upsert into Qdrant
        count = await upsert_chunks(
            collection,
            chunks,
            embeddings,
            commit_hash,
            removed_files=removed_files,
            full_index=full_index,
        )

        # Update metadata in Redis
        await _update_indexing_metadata(
            repo, collection, commit_hash, count, ref, redis_client
        )

        logger.info(f"Indexing complete for {repo}: {count} points")

    except Exception as e:
        logger.error(f"Indexing failed for {repo}: {e}", exc_info=True)
    finally:
        if worktree:
            await _cleanup_worktree(repo, worktree)


# ---------------------------------------------------------------------------
# Worktree management
# ---------------------------------------------------------------------------


async def _create_worktree(repo: str, ref: str) -> str | None:
    """Create a temporary worktree from the bare repo cache."""
    cache_base = "/var/cache/repos"
    repo_dir = os.path.join(cache_base, f"{repo}.git")

    if not os.path.isdir(repo_dir):
        logger.warning(f"Bare repo not found: {repo_dir}")
        return None

    # Create temp directory for worktree
    worktree = tempfile.mkdtemp(
        prefix=f"idx_{repo.replace('/', '_')}_", dir="/tmp"  # nosec B108
    )

    # Convert ref for worktree
    if ref.startswith("refs/pull/") or ref.startswith("refs/tags/"):
        bare_ref = ref
    else:
        base_ref = (
            ref.replace("refs/heads/", "") if ref.startswith("refs/heads/") else ref
        )
        bare_ref = f"refs/remotes/origin/{base_ref}"

    cmd = f"git --git-dir={repo_dir} worktree add --detach {worktree} {bare_ref}"

    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)

        if proc.returncode != 0:
            logger.warning(f"Worktree creation failed: {stderr.decode().strip()}")
            # Try with default branch
            cmd_fb = f"git --git-dir={repo_dir} worktree add --detach {worktree} refs/remotes/origin/main"
            proc = await asyncio.create_subprocess_shell(
                cmd_fb,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            if proc.returncode != 0:
                shutil.rmtree(worktree, ignore_errors=True)
                return None

        return worktree
    except Exception as e:
        logger.error(f"Worktree creation error: {e}")
        shutil.rmtree(worktree, ignore_errors=True)
        return None


async def _cleanup_worktree(repo: str, worktree: str) -> None:
    """Remove the worktree and clean up."""
    cache_base = "/var/cache/repos"
    repo_dir = os.path.join(cache_base, f"{repo}.git")

    try:
        cmd = f"git --git-dir={repo_dir} worktree remove --force {worktree}"
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
    except Exception:
        pass

    shutil.rmtree(worktree, ignore_errors=True)


async def _get_commit_hash(worktree: str) -> str:
    """Get the HEAD commit hash from a worktree."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "rev-parse",
            "HEAD",
            cwd=worktree,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        if proc.returncode == 0:
            return stdout.decode().strip()
    except Exception:
        pass
    return "unknown"


# ---------------------------------------------------------------------------
# Sync event listener
# ---------------------------------------------------------------------------


async def listen_for_sync_events(redis_client) -> None:
    """Subscribe to agent:sync:events and enqueue indexing jobs on completion."""
    pubsub = redis_client.pubsub()
    await pubsub.subscribe("agent:sync:events")
    logger.info("Listening for repo sync completion events...")

    while not shutdown_event.is_set():
        try:
            message = await pubsub.get_message(
                ignore_subscribe_messages=True, timeout=5.0
            )
            if message and message["type"] == "message":
                data = json.loads(message["data"])
                if data.get("status") == "complete":
                    repo = data.get("repo")
                    ref = data.get("ref", "main")
                    if repo:
                        logger.info(f"Sync complete for {repo}, enqueuing indexing job")
                        job = json.dumps(
                            {
                                "repo": repo,
                                "ref": ref,
                                "trigger": "repo_sync",
                            }
                        )
                        await redis_client.rpush("agent:indexing:requests", job)
        except json.JSONDecodeError:
            continue
        except Exception as e:
            if not shutdown_event.is_set():
                logger.error(f"Sync event listener error: {e}")
                await asyncio.sleep(5)

    await pubsub.unsubscribe("agent:sync:events")
    await pubsub.close()


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


async def main() -> None:
    """Main indexing worker loop."""
    logger.info("Starting indexing worker")

    if not INDEXING_ENABLED:
        logger.info("Indexing is disabled, exiting")
        return

    if not GEMINI_API_KEY:
        logger.warning("GEMINI_API_KEY not set, indexing worker cannot function")
        return

    setup_graceful_shutdown(shutdown_event, logger)

    # Initialize Redis connection
    redis_url = os.getenv("REDIS_URL", "redis://redis:6379")
    redis_password = os.getenv("REDIS_PASSWORD")

    queue = RedisQueue(
        redis_url=redis_url,
        queue_name="agent:indexing:requests",
        password=redis_password,
    )
    await queue._connect()
    redis_client = queue.redis

    # Start sync event listener in parallel
    sync_listener = asyncio.create_task(listen_for_sync_events(redis_client))

    logger.info("Indexing worker ready, waiting for jobs...")

    try:
        while not shutdown_event.is_set():
            try:
                # Pull next job with timeout
                result = await redis_client.blpop("agent:indexing:requests", timeout=5)
                if not result:
                    continue

                _, raw_message = result
                message = json.loads(raw_message)
                logger.info(
                    f"Processing indexing job for {message.get('repo', 'unknown')}"
                )

                await process_indexing_job(message, redis_client)

            except json.JSONDecodeError as e:
                logger.error(f"Invalid JSON in indexing request: {e}")
            except Exception as e:
                logger.error(f"Error in worker loop: {e}", exc_info=True)
                await asyncio.sleep(5)
    finally:
        sync_listener.cancel()
        try:
            await sync_listener
        except asyncio.CancelledError:
            pass
        await queue.close()
        logger.info("Indexing worker shutdown complete")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
