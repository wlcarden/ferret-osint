"""Tests for the ToolCache — SQLite-backed result caching."""

import pytest
import pytest_asyncio

from osint_agent.cache import ToolCache, _make_input_hash, _cache_key
from osint_agent.models import Entity, EntityType, Finding, Source


@pytest_asyncio.fixture
async def cache(tmp_path):
    """Provide a ToolCache backed by a temporary database."""
    c = ToolCache(db_path=tmp_path / "test_cache.db")
    yield c
    await c.close()


def _sample_finding(label: str = "Test Entity") -> Finding:
    return Finding(
        entities=[
            Entity(
                id="person:test:1",
                entity_type=EntityType.PERSON,
                label=label,
                properties={"city": "Portland"},
                sources=[Source(tool="test_tool")],
            ),
        ],
        notes=f"Test finding: {label}",
    )


# ------------------------------------------------------------------
# Helper functions
# ------------------------------------------------------------------

def test_make_input_hash_deterministic():
    """Same kwargs should always produce the same hash."""
    h1 = _make_input_hash({"username": "alice", "mode": "full"})
    h2 = _make_input_hash({"username": "alice", "mode": "full"})
    assert h1 == h2


def test_make_input_hash_key_order_irrelevant():
    """Key ordering should not change the hash (json sort_keys=True)."""
    h1 = _make_input_hash({"b": 2, "a": 1})
    h2 = _make_input_hash({"a": 1, "b": 2})
    assert h1 == h2


def test_make_input_hash_different_values():
    """Different inputs should produce different hashes."""
    h1 = _make_input_hash({"username": "alice"})
    h2 = _make_input_hash({"username": "bob"})
    assert h1 != h2


def test_make_input_hash_length():
    """Hash should be truncated to 16 hex chars."""
    h = _make_input_hash({"key": "value"})
    assert len(h) == 16


def test_cache_key_format():
    assert _cache_key("maigret", "abc123") == "maigret:abc123"


# ------------------------------------------------------------------
# Cache get/set round-trip
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_set_and_get(cache):
    """should store and retrieve a Finding."""
    finding = _sample_finding()
    await cache.set("maigret", {"username": "alice"}, finding)

    result = await cache.get("maigret", {"username": "alice"})
    assert result is not None
    assert result.notes == finding.notes
    assert len(result.entities) == 1
    assert result.entities[0].label == "Test Entity"


@pytest.mark.asyncio
async def test_get_miss(cache):
    """should return None for uncached queries."""
    result = await cache.get("maigret", {"username": "nonexistent"})
    assert result is None


@pytest.mark.asyncio
async def test_different_kwargs_miss(cache):
    """should not return cached result for different kwargs."""
    await cache.set("maigret", {"username": "alice"}, _sample_finding("Alice"))
    result = await cache.get("maigret", {"username": "bob"})
    assert result is None


@pytest.mark.asyncio
async def test_different_tool_miss(cache):
    """should not return cached result for different tool name."""
    await cache.set("maigret", {"username": "alice"}, _sample_finding())
    result = await cache.get("holehe", {"username": "alice"})
    assert result is None


@pytest.mark.asyncio
async def test_overwrite_existing(cache):
    """should overwrite when same key is set again."""
    await cache.set("maigret", {"username": "alice"}, _sample_finding("v1"))
    await cache.set("maigret", {"username": "alice"}, _sample_finding("v2"))
    result = await cache.get("maigret", {"username": "alice"})
    assert result.entities[0].label == "v2"


# ------------------------------------------------------------------
# TTL and expiration
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_expired_entry_returns_none(cache):
    """should return None for entries past their TTL."""
    await cache.set("maigret", {"username": "alice"}, _sample_finding(), ttl_hours=0)
    result = await cache.get("maigret", {"username": "alice"})
    # ttl_hours=0 means expires_at == created_at, so it's already expired
    assert result is None


@pytest.mark.asyncio
async def test_clear_expired(cache):
    """should remove expired entries and return the count."""
    await cache.set("tool_a", {"k": "1"}, _sample_finding(), ttl_hours=0)
    await cache.set("tool_b", {"k": "2"}, _sample_finding(), ttl_hours=0)
    await cache.set("tool_c", {"k": "3"}, _sample_finding(), ttl_hours=24)

    count = await cache.clear_expired()
    assert count == 2

    # Valid entry should survive
    result = await cache.get("tool_c", {"k": "3"})
    assert result is not None


# ------------------------------------------------------------------
# Invalidation
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_invalidate(cache):
    """should remove a specific cache entry."""
    await cache.set("maigret", {"username": "alice"}, _sample_finding())
    await cache.invalidate("maigret", {"username": "alice"})
    result = await cache.get("maigret", {"username": "alice"})
    assert result is None


@pytest.mark.asyncio
async def test_invalidate_nonexistent(cache):
    """should not error when invalidating a missing key."""
    await cache.invalidate("maigret", {"username": "ghost"})


# ------------------------------------------------------------------
# Clear all
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_clear_all(cache):
    """should remove all entries and return the count."""
    await cache.set("tool_a", {"k": "1"}, _sample_finding())
    await cache.set("tool_b", {"k": "2"}, _sample_finding())

    count = await cache.clear_all()
    assert count == 2

    result = await cache.get("tool_a", {"k": "1"})
    assert result is None


# ------------------------------------------------------------------
# Stats
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stats(cache):
    """should return accurate cache statistics."""
    await cache.set("maigret", {"k": "1"}, _sample_finding(), ttl_hours=24)
    await cache.set("maigret", {"k": "2"}, _sample_finding(), ttl_hours=24)
    await cache.set("holehe", {"k": "3"}, _sample_finding(), ttl_hours=0)

    stats = await cache.stats()
    assert stats["total"] == 3
    assert stats["valid"] == 2
    assert stats["expired"] == 1
    assert stats["by_tool"]["maigret"] == 2
    assert stats["by_tool"]["holehe"] == 1


@pytest.mark.asyncio
async def test_stats_empty(cache):
    """should return zeroes for empty cache."""
    stats = await cache.stats()
    assert stats["total"] == 0
    assert stats["valid"] == 0
    assert stats["by_tool"] == {}
