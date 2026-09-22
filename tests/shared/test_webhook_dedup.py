"""Tests for GitHub webhook delivery deduplication."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.constants import webhook_dedup_key
from shared.webhook_dedup import WebhookDeduplicator


class FakeRedis:
    """Minimal Redis stand-in implementing SET NX EX / DELETE semantics."""

    def __init__(self):
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        if ex is not None:
            self.ttls[key] = ex
        return True

    async def delete(self, key):
        self.ttls.pop(key, None)
        return 1 if self.store.pop(key, None) is not None else 0

    async def aclose(self):
        pass


@pytest.fixture
def dedup():
    """Deduplicator backed by a fake Redis, with connection short-circuited."""
    d = WebhookDeduplicator(redis_url="redis://localhost:6379")
    d.redis = FakeRedis()
    return d


class TestClaim:
    """Claiming a delivery id."""

    @pytest.mark.asyncio
    async def test_first_delivery_is_claimed(self, dedup):
        assert await dedup.claim("d-1") is True

    @pytest.mark.asyncio
    async def test_replayed_delivery_is_rejected(self, dedup):
        await dedup.claim("d-1")
        assert await dedup.claim("d-1") is False

    @pytest.mark.asyncio
    async def test_distinct_deliveries_are_independent(self, dedup):
        assert await dedup.claim("d-1") is True
        assert await dedup.claim("d-2") is True

    @pytest.mark.asyncio
    async def test_claim_uses_namespaced_key(self, dedup):
        await dedup.claim("d-1")
        assert webhook_dedup_key("d-1") in dedup.redis.store

    @pytest.mark.asyncio
    async def test_claim_sets_expiry(self, dedup):
        dedup.ttl_seconds = 3600
        await dedup.claim("d-1")
        assert dedup.redis.ttls[webhook_dedup_key("d-1")] == 3600

    @pytest.mark.asyncio
    async def test_missing_delivery_id_is_not_deduplicated(self, dedup):
        """A caller without the header (curl, a test) is always processed."""
        assert await dedup.claim("") is True
        assert await dedup.claim("") is True
        assert dedup.redis.store == {}

    @pytest.mark.asyncio
    async def test_concurrent_claims_have_exactly_one_winner(self, dedup):
        """SETNX decides the race; replicas cannot both queue the job."""
        import asyncio

        results = await asyncio.gather(*(dedup.claim("d-race") for _ in range(5)))
        assert results.count(True) == 1
        assert results.count(False) == 4


class TestFailOpen:
    """Redis outages must not drop genuine deliveries."""

    @pytest.mark.asyncio
    async def test_connection_failure_processes_delivery(self):
        d = WebhookDeduplicator()
        d._connect = AsyncMock(side_effect=OSError("redis down"))
        assert await d.claim("d-1") is True

    @pytest.mark.asyncio
    async def test_set_failure_processes_delivery(self, dedup):
        dedup.redis.set = AsyncMock(side_effect=OSError("connection reset"))
        assert await dedup.claim("d-1") is True

    @pytest.mark.asyncio
    async def test_failure_discards_stale_client_for_reconnect(self, dedup):
        dedup.redis.set = AsyncMock(side_effect=OSError("connection reset"))
        await dedup.claim("d-1")
        assert dedup.redis is None

    @pytest.mark.asyncio
    async def test_outage_is_logged_once_not_per_delivery(self, dedup, caplog):
        broken = MagicMock()
        broken.set = AsyncMock(side_effect=OSError("redis down"))
        dedup._connect = AsyncMock(side_effect=lambda: setattr(dedup, "redis", broken))

        with caplog.at_level("WARNING"):
            for i in range(3):
                await dedup.claim(f"d-{i}")

        warnings = [r for r in caplog.records if "dedup unavailable" in r.message]
        assert len(warnings) == 1


class TestRelease:
    """Releasing a claim so GitHub's retry is processed."""

    @pytest.mark.asyncio
    async def test_release_allows_retry_to_be_claimed(self, dedup):
        await dedup.claim("d-1")
        await dedup.release("d-1")
        assert await dedup.claim("d-1") is True

    @pytest.mark.asyncio
    async def test_release_without_connection_is_noop(self):
        d = WebhookDeduplicator()
        await d.release("d-1")  # must not raise

    @pytest.mark.asyncio
    async def test_release_ignores_empty_delivery_id(self, dedup):
        await dedup.release("")
        assert dedup.redis.store == {}

    @pytest.mark.asyncio
    async def test_release_failure_is_swallowed(self, dedup):
        dedup.redis.delete = AsyncMock(side_effect=OSError("redis down"))
        await dedup.release("d-1")  # key expires on its own; must not raise


class TestKeyBuilder:
    """The Redis key format is part of the contract."""

    def test_key_is_namespaced_by_delivery_id(self):
        assert webhook_dedup_key("abc-123") == "agent:webhook:delivery:abc-123"
