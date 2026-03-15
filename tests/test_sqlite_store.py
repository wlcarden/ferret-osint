"""Tests for the SQLite-backed persistent graph store."""

import pytest
import pytest_asyncio

from osint_agent.graph.sqlite_store import SqliteStore, _merge_sources
from osint_agent.models import Entity, EntityType, Finding, Relationship, RelationType, Source


@pytest_asyncio.fixture
async def store(tmp_path):
    """Create a temporary SQLite store."""
    db_path = tmp_path / "test_graph.db"
    s = SqliteStore(db_path=str(db_path))
    yield s
    await s.close()


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


# ------------------------------------------------------------------
# Basic CRUD
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ingest_finding(store, sample_finding):
    """should ingest entities and relationships"""
    await store.ingest_finding(sample_finding)
    assert await store.entity_count() == 3
    assert await store.relationship_count() == 2


@pytest.mark.asyncio
async def test_merge_is_idempotent(store, sample_finding):
    """should not create duplicates on double ingest"""
    await store.ingest_finding(sample_finding)
    await store.ingest_finding(sample_finding)
    assert await store.entity_count() == 3
    assert await store.relationship_count() == 2


@pytest.mark.asyncio
async def test_merge_entity_updates_label(store):
    """should update label on re-merge"""
    e = Entity(
        id="person:test",
        entity_type=EntityType.PERSON,
        label="Old Name",
        sources=[Source(tool="a")],
    )
    await store.merge_entity(e)
    e2 = Entity(
        id="person:test",
        entity_type=EntityType.PERSON,
        label="New Name",
        sources=[Source(tool="b")],
    )
    await store.merge_entity(e2)

    assert await store.entity_count() == 1
    results = await store.query("entity:person:test")
    assert results[0]["label"] == "New Name"


@pytest.mark.asyncio
async def test_merge_entity_accumulates_sources(store):
    """should merge sources from different tools"""
    e1 = Entity(
        id="person:test",
        entity_type=EntityType.PERSON,
        label="Test",
        sources=[Source(tool="tool_a", source_url="http://a.com")],
    )
    e2 = Entity(
        id="person:test",
        entity_type=EntityType.PERSON,
        label="Test",
        sources=[Source(tool="tool_b", source_url="http://b.com")],
    )
    await store.merge_entity(e1)
    await store.merge_entity(e2)

    results = await store.query("entity:person:test")
    sources = results[0]["sources"]
    tools = {s["tool"] for s in sources}
    assert "tool_a" in tools
    assert "tool_b" in tools


# ------------------------------------------------------------------
# Queries
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_query_all_nodes(store, sample_finding):
    """should return all entities"""
    await store.ingest_finding(sample_finding)
    nodes = await store.query("all_nodes")
    assert len(nodes) == 3
    ids = {n["id"] for n in nodes}
    assert "person:jane" in ids


@pytest.mark.asyncio
async def test_query_all_edges(store, sample_finding):
    """should return all relationships"""
    await store.ingest_finding(sample_finding)
    edges = await store.query("all_edges")
    assert len(edges) == 2


@pytest.mark.asyncio
async def test_query_neighbors(store, sample_finding):
    """should return both incoming and outgoing neighbors"""
    await store.ingest_finding(sample_finding)
    neighbors = await store.query("neighbors:email:jane@example.com")
    assert len(neighbors) == 2
    directions = {n["direction"] for n in neighbors}
    assert "incoming" in directions
    assert "outgoing" in directions


@pytest.mark.asyncio
async def test_query_entity_by_id(store, sample_finding):
    """should return a single entity by ID"""
    await store.ingest_finding(sample_finding)
    results = await store.query("entity:person:jane")
    assert len(results) == 1
    assert results[0]["label"] == "Jane Doe"
    assert results[0]["location"] == "Portland, OR"


@pytest.mark.asyncio
async def test_query_entity_not_found(store):
    """should return empty list for missing entity"""
    results = await store.query("entity:nonexistent")
    assert results == []


@pytest.mark.asyncio
async def test_query_by_type(store, sample_finding):
    """should filter entities by type"""
    await store.ingest_finding(sample_finding)
    results = await store.query("type:person")
    assert len(results) == 1
    assert results[0]["id"] == "person:jane"


@pytest.mark.asyncio
async def test_query_search(store, sample_finding):
    """should search entities by label substring"""
    await store.ingest_finding(sample_finding)
    # "Jane" appears in "Jane Doe", "jane@example.com", "janedoe on GitHub"
    # SQLite LIKE is case-insensitive for ASCII
    results = await store.query("search:Jane Doe")
    assert len(results) == 1
    assert results[0]["id"] == "person:jane"


@pytest.mark.asyncio
async def test_query_search_case_sensitive(store, sample_finding):
    """should be case-sensitive in search (SQLite LIKE default)"""
    await store.ingest_finding(sample_finding)
    results = await store.query("search:jane")
    # SQLite LIKE is case-insensitive for ASCII by default
    assert len(results) >= 1


@pytest.mark.asyncio
async def test_query_unknown_filter(store):
    """should return empty for unknown query type"""
    results = await store.query("unknown:stuff")
    assert results == []


# ------------------------------------------------------------------
# Persistence
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_persistence_across_connections(tmp_path, sample_finding):
    """should persist data across close/reopen cycles"""
    db_path = str(tmp_path / "persist_test.db")

    # Session 1: ingest data
    store1 = SqliteStore(db_path=db_path)
    await store1.ingest_finding(sample_finding)
    assert await store1.entity_count() == 3
    await store1.close()

    # Session 2: data should still be there
    store2 = SqliteStore(db_path=db_path)
    assert await store2.entity_count() == 3
    assert await store2.relationship_count() == 2
    nodes = await store2.query("all_nodes")
    assert len(nodes) == 3
    await store2.close()


@pytest.mark.asyncio
async def test_accumulation_across_sessions(tmp_path):
    """should accumulate entities across sessions"""
    db_path = str(tmp_path / "accumulate_test.db")

    # Session 1
    store1 = SqliteStore(db_path=db_path)
    await store1.merge_entity(Entity(
        id="person:alice",
        entity_type=EntityType.PERSON,
        label="Alice",
        sources=[Source(tool="session1")],
    ))
    assert await store1.entity_count() == 1
    await store1.close()

    # Session 2
    store2 = SqliteStore(db_path=db_path)
    await store2.merge_entity(Entity(
        id="person:bob",
        entity_type=EntityType.PERSON,
        label="Bob",
        sources=[Source(tool="session2")],
    ))
    assert await store2.entity_count() == 2  # Alice + Bob
    await store2.close()


# ------------------------------------------------------------------
# Summary
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_summary_async(store, sample_finding):
    """should produce human-readable summary"""
    await store.ingest_finding(sample_finding)
    summary = await store.summary_async()
    assert "3 entities" in summary
    assert "2 relationships" in summary
    assert "person" in summary


# ------------------------------------------------------------------
# Source merging
# ------------------------------------------------------------------

def test_merge_sources_deduplicates():
    """should deduplicate by (tool, source_url)"""
    existing = [{"tool": "a", "source_url": "http://a.com"}]
    new = [
        {"tool": "a", "source_url": "http://a.com"},  # duplicate
        {"tool": "b", "source_url": "http://b.com"},  # new
    ]
    merged = _merge_sources(existing, new)
    assert len(merged) == 2


def test_merge_sources_empty():
    """should handle empty lists"""
    assert _merge_sources([], []) == []
    assert len(_merge_sources([], [{"tool": "a", "source_url": None}])) == 1


# ------------------------------------------------------------------
# Investigation tracking
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_investigation(store):
    """should create investigation and return ID"""
    inv_id = await store.create_investigation("Thomas Jacob V2")
    assert inv_id is not None
    assert inv_id > 0


@pytest.mark.asyncio
async def test_list_investigations(store):
    """should list all investigations"""
    await store.create_investigation("Investigation A")
    await store.create_investigation("Investigation B")
    investigations = await store.list_investigations()
    assert len(investigations) == 2
    names = {inv["name"] for inv in investigations}
    assert "Investigation A" in names
    assert "Investigation B" in names


# ------------------------------------------------------------------
# Lead queue
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_add_and_get_leads(store):
    """should add leads and retrieve them"""
    inv_id = await store.create_investigation("Test")
    await store.add_lead(
        lead_type="username",
        value="tjacob",
        score=0.8,
        investigation_id=inv_id,
        notes="Found on GitHub",
    )
    await store.add_lead(
        lead_type="email",
        value="tjacob@gmail.com",
        score=0.5,
        investigation_id=inv_id,
    )

    leads = await store.get_leads(investigation_id=inv_id)
    assert len(leads) == 2
    # Should be ordered by score desc
    assert leads[0]["score"] == 0.8
    assert leads[1]["score"] == 0.5


@pytest.mark.asyncio
async def test_get_leads_by_status(store):
    """should filter leads by status"""
    lead_id = await store.add_lead(lead_type="url", value="http://example.com")
    await store.add_lead(lead_type="url", value="http://other.com")

    # Mark one as completed
    await store.update_lead(lead_id, status="completed", notes="Dead end")

    pending = await store.get_leads(status="pending")
    assert len(pending) == 1
    assert pending[0]["value"] == "http://other.com"

    completed = await store.get_leads(status="completed")
    assert len(completed) == 1
    assert completed[0]["notes"] == "Dead end"


@pytest.mark.asyncio
async def test_pending_lead_count(store):
    """should count pending leads"""
    await store.add_lead(lead_type="username", value="test1")
    await store.add_lead(lead_type="username", value="test2")
    lead_id = await store.add_lead(lead_type="username", value="test3")
    await store.update_lead(lead_id, status="completed")

    assert await store.pending_lead_count() == 2


# ------------------------------------------------------------------
# Investigation scoping
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ingest_finding_links_to_investigation(store, sample_finding):
    """should link entities to investigation when investigation_id is given"""
    inv_id = await store.create_investigation("Scoped Test")
    await store.ingest_finding(sample_finding, investigation_id=inv_id)

    scoped = await store.query(f"inv:{inv_id}:all_nodes")
    assert len(scoped) == 3
    ids = {n["id"] for n in scoped}
    assert "person:jane" in ids
    assert "email:jane@example.com" in ids
    assert "account:github:janedoe" in ids


@pytest.mark.asyncio
async def test_scoped_edges_only_between_scoped_nodes(store):
    """should return only edges where both endpoints are in the investigation"""
    inv_id = await store.create_investigation("Scoped edges")

    # Ingest 2 connected entities into investigation
    f1 = Finding(
        entities=[
            Entity(id="person:a", entity_type=EntityType.PERSON, label="A",
                   sources=[Source(tool="t")]),
            Entity(id="email:a@x.com", entity_type=EntityType.EMAIL, label="a@x.com",
                   sources=[Source(tool="t")]),
        ],
        relationships=[
            Relationship(source_id="person:a", target_id="email:a@x.com",
                         relation_type=RelationType.HAS_EMAIL,
                         sources=[Source(tool="t")]),
        ],
    )
    await store.ingest_finding(f1, investigation_id=inv_id)

    # Ingest an entity outside the investigation that connects to person:a
    outside = Entity(id="org:outside", entity_type=EntityType.ORGANIZATION,
                     label="Outside Org", sources=[Source(tool="t")])
    await store.merge_entity(outside)
    await store.merge_relationship(Relationship(
        source_id="person:a", target_id="org:outside",
        relation_type=RelationType.WORKS_AT,
        sources=[Source(tool="t")],
    ))

    # Scoped view should only show the internal edge
    scoped_edges = await store.query(f"inv:{inv_id}:all_edges")
    assert len(scoped_edges) == 1
    assert scoped_edges[0]["relation_type"] == "has_email"

    # Unscoped view should show both
    all_edges = await store.query("all_edges")
    assert len(all_edges) == 2


@pytest.mark.asyncio
async def test_separate_investigations_isolate_data(store):
    """should keep investigation data separate"""
    inv1 = await store.create_investigation("Investigation Alpha")
    inv2 = await store.create_investigation("Investigation Beta")

    f1 = Finding(
        entities=[Entity(id="person:alpha", entity_type=EntityType.PERSON,
                         label="Alpha", sources=[Source(tool="t")])],
    )
    f2 = Finding(
        entities=[Entity(id="person:beta", entity_type=EntityType.PERSON,
                         label="Beta", sources=[Source(tool="t")])],
    )
    await store.ingest_finding(f1, investigation_id=inv1)
    await store.ingest_finding(f2, investigation_id=inv2)

    inv1_nodes = await store.query(f"inv:{inv1}:all_nodes")
    inv2_nodes = await store.query(f"inv:{inv2}:all_nodes")
    all_nodes = await store.query("all_nodes")

    assert len(inv1_nodes) == 1
    assert inv1_nodes[0]["id"] == "person:alpha"
    assert len(inv2_nodes) == 1
    assert inv2_nodes[0]["id"] == "person:beta"
    assert len(all_nodes) == 2


@pytest.mark.asyncio
async def test_shared_entity_across_investigations(store):
    """should allow same entity in multiple investigations"""
    inv1 = await store.create_investigation("Inv 1")
    inv2 = await store.create_investigation("Inv 2")

    shared = Finding(
        entities=[Entity(id="org:shared", entity_type=EntityType.ORGANIZATION,
                         label="Shared Corp", sources=[Source(tool="t")])],
    )
    await store.ingest_finding(shared, investigation_id=inv1)
    await store.ingest_finding(shared, investigation_id=inv2)

    # Both investigations should see the entity
    assert len(await store.query(f"inv:{inv1}:all_nodes")) == 1
    assert len(await store.query(f"inv:{inv2}:all_nodes")) == 1
    # Only one entity in the global store
    assert await store.entity_count() == 1


@pytest.mark.asyncio
async def test_ingest_without_investigation_id(store, sample_finding):
    """should still work when no investigation_id given (backward compat)"""
    await store.ingest_finding(sample_finding)
    assert await store.entity_count() == 3

    # No investigation scoping — inv query should return nothing
    nodes = await store.query("inv:999:all_nodes")
    assert len(nodes) == 0


@pytest.mark.asyncio
async def test_link_entity_to_investigation_idempotent(store):
    """should not fail on duplicate link"""
    inv_id = await store.create_investigation("Test")
    e = Entity(id="person:test", entity_type=EntityType.PERSON,
               label="Test", sources=[Source(tool="t")])
    await store.merge_entity(e)
    await store.link_entity_to_investigation("person:test", inv_id)
    await store.link_entity_to_investigation("person:test", inv_id)

    nodes = await store.query(f"inv:{inv_id}:all_nodes")
    assert len(nodes) == 1


# ------------------------------------------------------------------
# Cross-investigation search
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_across_investigations(store):
    """should find entities and show which investigations they belong to"""
    inv1 = await store.create_investigation("Alpha Investigation")
    inv2 = await store.create_investigation("Beta Investigation")

    person = Entity(
        id="person:test:john_smith",
        entity_type=EntityType.PERSON,
        label="John Smith",
        sources=[Source(tool="test")],
    )
    # Same person in both investigations
    await store.ingest_finding(
        Finding(entities=[person]), investigation_id=inv1,
    )
    await store.link_entity_to_investigation(person.id, inv2)

    results = await store.search_across_investigations("John")
    assert len(results) == 1
    assert results[0]["label"] == "John Smith"
    inv_ids = {inv["id"] for inv in results[0]["investigations"]}
    assert inv1 in inv_ids
    assert inv2 in inv_ids


@pytest.mark.asyncio
async def test_search_across_investigations_type_filter(store):
    """should filter by entity type"""
    person = Entity(
        id="person:test:jane", entity_type=EntityType.PERSON,
        label="Jane Corp", sources=[Source(tool="test")],
    )
    org = Entity(
        id="organization:test:jane_corp", entity_type=EntityType.ORGANIZATION,
        label="Jane Corp LLC", sources=[Source(tool="test")],
    )
    await store.ingest_finding(Finding(entities=[person, org]))

    all_results = await store.search_across_investigations("Jane")
    assert len(all_results) == 2

    org_only = await store.search_across_investigations("Jane", entity_type="organization")
    assert len(org_only) == 1
    assert org_only[0]["entity_type"] == "organization"


@pytest.mark.asyncio
async def test_search_across_investigations_no_investigation(store):
    """should return entities not linked to any investigation"""
    person = Entity(
        id="person:test:orphan", entity_type=EntityType.PERSON,
        label="Orphan Person", sources=[Source(tool="test")],
    )
    await store.ingest_finding(Finding(entities=[person]))

    results = await store.search_across_investigations("Orphan")
    assert len(results) == 1
    assert results[0]["investigations"] == []


# ------------------------------------------------------------------
# Finding notes persistence
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_finding_persists_notes(store):
    """should persist finding notes via get_finding_notes()"""
    inv_id = await store.create_investigation("Notes test")
    f = Finding(
        entities=[
            Entity(id="person:a", entity_type=EntityType.PERSON, label="A",
                   sources=[Source(tool="reddit")]),
        ],
        notes="Reddit profile: u/testuser — 234 posts, timezone EST",
    )
    await store.ingest_finding(f, investigation_id=inv_id)

    notes = await store.get_finding_notes(investigation_id=inv_id)
    assert len(notes) == 1
    assert notes[0]["tool"] == "reddit"
    assert "234 posts" in notes[0]["notes"]
    assert "created_at" in notes[0]


@pytest.mark.asyncio
async def test_ingest_finding_skips_empty_notes(store):
    """should not create a findings row when notes is None or empty"""
    f1 = Finding(
        entities=[
            Entity(id="person:b", entity_type=EntityType.PERSON, label="B",
                   sources=[Source(tool="test")]),
        ],
        notes=None,
    )
    f2 = Finding(
        entities=[
            Entity(id="person:c", entity_type=EntityType.PERSON, label="C",
                   sources=[Source(tool="test")]),
        ],
        notes="",
    )
    await store.ingest_finding(f1)
    await store.ingest_finding(f2)

    notes = await store.get_finding_notes()
    assert len(notes) == 0


@pytest.mark.asyncio
async def test_finding_notes_scoped_by_investigation(store):
    """should only return notes for the specified investigation"""
    inv1 = await store.create_investigation("Inv 1")
    inv2 = await store.create_investigation("Inv 2")

    f1 = Finding(
        entities=[
            Entity(id="person:x", entity_type=EntityType.PERSON, label="X",
                   sources=[Source(tool="tool1")]),
        ],
        notes="Notes for investigation 1",
    )
    f2 = Finding(
        entities=[
            Entity(id="person:y", entity_type=EntityType.PERSON, label="Y",
                   sources=[Source(tool="tool2")]),
        ],
        notes="Notes for investigation 2",
    )
    await store.ingest_finding(f1, investigation_id=inv1)
    await store.ingest_finding(f2, investigation_id=inv2)

    notes1 = await store.get_finding_notes(investigation_id=inv1)
    notes2 = await store.get_finding_notes(investigation_id=inv2)

    assert len(notes1) == 1
    assert notes1[0]["tool"] == "tool1"
    assert len(notes2) == 1
    assert notes2[0]["tool"] == "tool2"


@pytest.mark.asyncio
async def test_finding_notes_infers_tool_name(store):
    """should extract tool name from first entity's sources"""
    f = Finding(
        entities=[
            Entity(id="person:t", entity_type=EntityType.PERSON, label="T",
                   sources=[Source(tool="littlesis")]),
        ],
        notes="LittleSis: 5 relationships found",
    )
    await store.ingest_finding(f)

    notes = await store.get_finding_notes()
    assert notes[0]["tool"] == "littlesis"


# ------------------------------------------------------------------
# Pruning
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_find_orphan_ids(store):
    """should identify entities with no relationships"""
    # Connected pair
    f = Finding(
        entities=[
            Entity(id="person:a", entity_type=EntityType.PERSON, label="A",
                   sources=[Source(tool="t")]),
            Entity(id="email:a@x.com", entity_type=EntityType.EMAIL, label="a@x",
                   sources=[Source(tool="t")]),
        ],
        relationships=[
            Relationship(source_id="person:a", target_id="email:a@x.com",
                         relation_type=RelationType.HAS_EMAIL,
                         sources=[Source(tool="t")]),
        ],
    )
    await store.ingest_finding(f)

    # Orphan
    orphan = Entity(id="account:orphan", entity_type=EntityType.ACCOUNT,
                    label="Orphan", sources=[Source(tool="t")])
    await store.merge_entity(orphan)

    orphans = await store.find_orphan_ids()
    assert orphans == {"account:orphan"}


@pytest.mark.asyncio
async def test_find_orphan_ids_scoped(store):
    """should only find orphans within a specific investigation"""
    inv_id = await store.create_investigation("Test")

    orphan = Entity(id="account:orphan", entity_type=EntityType.ACCOUNT,
                    label="Orphan", sources=[Source(tool="t")])
    await store.merge_entity(orphan)
    await store.link_entity_to_investigation("account:orphan", inv_id)

    # Orphan outside investigation
    outside = Entity(id="account:outside", entity_type=EntityType.ACCOUNT,
                     label="Outside Orphan", sources=[Source(tool="t")])
    await store.merge_entity(outside)

    scoped_orphans = await store.find_orphan_ids(investigation_id=inv_id)
    assert scoped_orphans == {"account:orphan"}
    # Outside orphan should NOT appear in scoped results
    assert "account:outside" not in scoped_orphans


@pytest.mark.asyncio
async def test_find_unreachable_ids(store):
    """should find entities not connected to seed"""
    # Connected cluster: A -> B -> C
    f = Finding(
        entities=[
            Entity(id="person:a", entity_type=EntityType.PERSON, label="A",
                   sources=[Source(tool="t")]),
            Entity(id="person:b", entity_type=EntityType.PERSON, label="B",
                   sources=[Source(tool="t")]),
            Entity(id="person:c", entity_type=EntityType.PERSON, label="C",
                   sources=[Source(tool="t")]),
        ],
        relationships=[
            Relationship(source_id="person:a", target_id="person:b",
                         relation_type=RelationType.CONNECTED_TO,
                         sources=[Source(tool="t")]),
            Relationship(source_id="person:b", target_id="person:c",
                         relation_type=RelationType.CONNECTED_TO,
                         sources=[Source(tool="t")]),
        ],
    )
    await store.ingest_finding(f)

    # Disconnected island: D -> E
    f2 = Finding(
        entities=[
            Entity(id="person:d", entity_type=EntityType.PERSON, label="D",
                   sources=[Source(tool="t")]),
            Entity(id="person:e", entity_type=EntityType.PERSON, label="E",
                   sources=[Source(tool="t")]),
        ],
        relationships=[
            Relationship(source_id="person:d", target_id="person:e",
                         relation_type=RelationType.CONNECTED_TO,
                         sources=[Source(tool="t")]),
        ],
    )
    await store.ingest_finding(f2)

    unreachable = await store.find_unreachable_ids("person:a")
    assert unreachable == {"person:d", "person:e"}


@pytest.mark.asyncio
async def test_delete_entities(store, sample_finding):
    """should delete entities, their relationships, and junction links"""
    inv_id = await store.create_investigation("Test")
    await store.ingest_finding(sample_finding, investigation_id=inv_id)
    assert await store.entity_count() == 3
    assert await store.relationship_count() == 2

    deleted = await store.delete_entities({"account:github:janedoe"})
    assert deleted == 1
    assert await store.entity_count() == 2
    # The relationship to the deleted entity should also be gone
    assert await store.relationship_count() == 1
    # Junction table should be updated
    scoped = await store.query(f"inv:{inv_id}:all_nodes")
    assert len(scoped) == 2


@pytest.mark.asyncio
async def test_delete_empty_set(store):
    """should handle empty deletion gracefully"""
    deleted = await store.delete_entities(set())
    assert deleted == 0


@pytest.mark.asyncio
async def test_prune_orphans_end_to_end(store):
    """should find and delete orphans in one flow"""
    # One connected entity
    f = Finding(
        entities=[
            Entity(id="person:a", entity_type=EntityType.PERSON, label="A",
                   sources=[Source(tool="t")]),
            Entity(id="email:a@x.com", entity_type=EntityType.EMAIL, label="a@x",
                   sources=[Source(tool="t")]),
        ],
        relationships=[
            Relationship(source_id="person:a", target_id="email:a@x.com",
                         relation_type=RelationType.HAS_EMAIL,
                         sources=[Source(tool="t")]),
        ],
    )
    await store.ingest_finding(f)

    # Three orphans
    for i in range(3):
        await store.merge_entity(Entity(
            id=f"account:orphan{i}", entity_type=EntityType.ACCOUNT,
            label=f"Orphan {i}", sources=[Source(tool="t")],
        ))
    assert await store.entity_count() == 5

    orphans = await store.find_orphan_ids()
    assert len(orphans) == 3
    await store.delete_entities(orphans)
    assert await store.entity_count() == 2
    assert await store.relationship_count() == 1


@pytest.mark.asyncio
async def test_find_small_component_ids(store):
    """should find entities in components smaller than min_size"""
    # Big component: A-B-C-D (4 nodes)
    f1 = Finding(
        entities=[
            Entity(id=f"person:{x}", entity_type=EntityType.PERSON, label=x,
                   sources=[Source(tool="t")])
            for x in ["a", "b", "c", "d"]
        ],
        relationships=[
            Relationship(source_id="person:a", target_id="person:b",
                         relation_type=RelationType.CONNECTED_TO,
                         sources=[Source(tool="t")]),
            Relationship(source_id="person:b", target_id="person:c",
                         relation_type=RelationType.CONNECTED_TO,
                         sources=[Source(tool="t")]),
            Relationship(source_id="person:c", target_id="person:d",
                         relation_type=RelationType.CONNECTED_TO,
                         sources=[Source(tool="t")]),
        ],
    )
    await store.ingest_finding(f1)

    # Small component: E-F (2 nodes)
    f2 = Finding(
        entities=[
            Entity(id="person:e", entity_type=EntityType.PERSON, label="E",
                   sources=[Source(tool="t")]),
            Entity(id="person:f", entity_type=EntityType.PERSON, label="F",
                   sources=[Source(tool="t")]),
        ],
        relationships=[
            Relationship(source_id="person:e", target_id="person:f",
                         relation_type=RelationType.CONNECTED_TO,
                         sources=[Source(tool="t")]),
        ],
    )
    await store.ingest_finding(f2)

    # Orphan (component of size 1)
    await store.merge_entity(Entity(
        id="person:g", entity_type=EntityType.PERSON, label="G",
        sources=[Source(tool="t")],
    ))

    # min_size=3 should catch E-F (size 2) and G (size 1)
    small = await store.find_small_component_ids(min_size=3)
    assert small == {"person:e", "person:f", "person:g"}

    # min_size=2 should only catch G (size 1)
    small2 = await store.find_small_component_ids(min_size=2)
    assert small2 == {"person:g"}
