"""Tests for the in-memory graph store."""

import pytest

from osint_agent.graph.memory_store import MemoryStore
from osint_agent.models import Entity, EntityType, Finding, Relationship, RelationType, Source


@pytest.fixture
def sample_finding():
    person = Entity(
        id="person:jane",
        entity_type=EntityType.PERSON,
        label="Jane Doe",
        properties={"location": "Portland, OR"},
        sources=[Source(tool="manual")],
    )
    email = Entity(
        id="email:jane@example.com",
        entity_type=EntityType.EMAIL,
        label="jane@example.com",
        sources=[Source(tool="manual")],
    )
    account = Entity(
        id="account:github:janedoe",
        entity_type=EntityType.ACCOUNT,
        label="janedoe on GitHub",
        properties={"platform": "GitHub", "url": "https://github.com/janedoe"},
        sources=[Source(tool="maigret")],
    )
    return Finding(
        entities=[person, email, account],
        relationships=[
            Relationship(
                source_id="person:jane",
                target_id="email:jane@example.com",
                relation_type=RelationType.HAS_EMAIL,
                sources=[Source(tool="manual")],
            ),
            Relationship(
                source_id="email:jane@example.com",
                target_id="account:github:janedoe",
                relation_type=RelationType.HAS_ACCOUNT,
                sources=[Source(tool="holehe")],
            ),
        ],
    )


@pytest.mark.asyncio
async def test_ingest_finding(sample_finding):
    store = MemoryStore()
    await store.ingest_finding(sample_finding)
    assert await store.entity_count() == 3
    assert await store.relationship_count() == 2


@pytest.mark.asyncio
async def test_merge_is_idempotent(sample_finding):
    store = MemoryStore()
    await store.ingest_finding(sample_finding)
    await store.ingest_finding(sample_finding)
    # Merging twice should not create duplicates
    assert await store.entity_count() == 3
    assert await store.relationship_count() == 2


@pytest.mark.asyncio
async def test_query_all_nodes(sample_finding):
    store = MemoryStore()
    await store.ingest_finding(sample_finding)
    nodes = await store.query("all_nodes")
    assert len(nodes) == 3
    ids = {n["id"] for n in nodes}
    assert "person:jane" in ids


@pytest.mark.asyncio
async def test_query_neighbors(sample_finding):
    store = MemoryStore()
    await store.ingest_finding(sample_finding)
    neighbors = await store.query("neighbors:email:jane@example.com")
    assert len(neighbors) == 2  # person (incoming) + github account (outgoing)


@pytest.mark.asyncio
async def test_summary_output(sample_finding):
    store = MemoryStore()
    await store.ingest_finding(sample_finding)
    summary = store.summary()
    assert "3 entities" in summary
    assert "2 relationships" in summary
    assert "person" in summary
    assert "email" in summary
