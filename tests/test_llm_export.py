"""Tests for LLM export/ingest — export_investigation() and ingest_extraction()."""

import json
from pathlib import Path

import pytest
import pytest_asyncio

from osint_agent.graph.sqlite_store import SqliteStore
from osint_agent.llm_export import export_investigation, ingest_extraction
from osint_agent.models import (
    Entity,
    EntityType,
    Finding,
    Relationship,
    RelationType,
    Source,
)


@pytest_asyncio.fixture
async def store(tmp_path):
    """Create a fresh SqliteStore for each test."""
    db_path = str(tmp_path / "test.db")
    s = SqliteStore(db_path=db_path)
    yield s
    await s.close()


def _make_person_entity(name: str, tool: str = "peoplesearch", **props) -> Entity:
    slug = name.lower().replace(" ", "_")
    return Entity(
        id=f"person:{tool}:{slug}",
        entity_type=EntityType.PERSON,
        label=name,
        properties={"source_system": tool, **props},
        sources=[Source(tool=tool, confidence=1.0)],
    )


def _make_org_entity(name: str, tool: str = "littlesis", **props) -> Entity:
    slug = name.lower().replace(" ", "_")
    return Entity(
        id=f"organization:{tool}:{slug}",
        entity_type=EntityType.ORGANIZATION,
        label=name,
        properties={"source_system": tool, **props},
        sources=[Source(tool=tool, confidence=1.0)],
    )


def _make_email_entity(email: str, tool: str = "holehe") -> Entity:
    return Entity(
        id=f"email:{tool}:{email}",
        entity_type=EntityType.EMAIL,
        label=email,
        sources=[Source(tool=tool, confidence=1.0)],
    )


# --- Export tests ---


@pytest.mark.asyncio
async def test_export_empty_investigation(store):
    """should produce valid JSON with zero counts when no data exists"""
    inv_id = await store.create_investigation("empty test")
    raw = await export_investigation(store, investigation_id=inv_id)
    data = json.loads(raw)

    assert data["meta"]["investigation_id"] == inv_id
    assert data["meta"]["entity_count"] == 0
    assert data["meta"]["relationship_count"] == 0
    assert data["meta"]["lead_count"] == 0
    assert data["entities"] == []
    assert data["relationships"] == []
    assert data["leads"] == []
    assert "schema_reference" in data


@pytest.mark.asyncio
async def test_export_with_entities_and_relationships(store):
    """should serialize entities, relationships, and properties correctly"""
    inv_id = await store.create_investigation("test inv")

    person = _make_person_entity("Jane Doe", employer="Acme Corp", city="Portland")
    email = _make_email_entity("jane@acme.com")
    rel = Relationship(
        source_id=person.id,
        target_id=email.id,
        relation_type=RelationType.HAS_EMAIL,
        sources=[Source(tool="holehe")],
    )
    finding = Finding(entities=[person, email], relationships=[rel])
    await store.ingest_finding(finding, investigation_id=inv_id)

    raw = await export_investigation(store, investigation_id=inv_id)
    data = json.loads(raw)

    assert data["meta"]["entity_count"] == 2
    assert data["meta"]["relationship_count"] == 1

    ids = {e["id"] for e in data["entities"]}
    assert person.id in ids
    assert email.id in ids

    # Properties should be present
    jane = next(e for e in data["entities"] if e["id"] == person.id)
    assert jane["properties"]["employer"] == "Acme Corp"
    assert jane["properties"]["city"] == "Portland"
    assert jane["entity_type"] == "person"

    # Sources should be serialized
    assert len(jane["sources"]) >= 1
    assert jane["sources"][0]["tool"] == "peoplesearch"

    # Relationship present
    assert data["relationships"][0]["relation_type"] == "has_email"
    assert data["relationships"][0]["source_id"] == person.id
    assert data["relationships"][0]["target_id"] == email.id


@pytest.mark.asyncio
async def test_export_investigation_scoped(store):
    """should only include entities linked to the specified investigation"""
    inv1 = await store.create_investigation("inv 1")
    inv2 = await store.create_investigation("inv 2")

    person1 = _make_person_entity("Alice", tool="tool1")
    person2 = _make_person_entity("Bob", tool="tool2")

    await store.ingest_finding(
        Finding(entities=[person1]), investigation_id=inv1,
    )
    await store.ingest_finding(
        Finding(entities=[person2]), investigation_id=inv2,
    )

    raw = await export_investigation(store, investigation_id=inv1)
    data = json.loads(raw)

    assert data["meta"]["entity_count"] == 1
    assert data["entities"][0]["label"] == "Alice"


@pytest.mark.asyncio
async def test_export_includes_leads(store):
    """should include leads in the export"""
    inv_id = await store.create_investigation("lead test")
    await store.add_lead(
        lead_type="email",
        value="test@example.com",
        score=0.8,
        investigation_id=inv_id,
        notes="Discovered email",
    )

    raw = await export_investigation(store, investigation_id=inv_id)
    data = json.loads(raw)

    assert data["meta"]["lead_count"] == 1
    lead = data["leads"][0]
    assert lead["lead_type"] == "email"
    assert lead["value"] == "test@example.com"
    assert lead["score"] == 0.8


@pytest.mark.asyncio
async def test_export_schema_reference_completeness(store):
    """should include all EntityType and RelationType values in schema_reference"""
    raw = await export_investigation(store)
    data = json.loads(raw)
    ref = data["schema_reference"]

    for et in EntityType:
        assert et.value in ref["entity_types"]
    for rt in RelationType:
        assert rt.value in ref["relation_types"]

    assert "entity_id_convention" in ref
    assert ref["lead_types"] == [
        "username", "email", "domain", "phone",
        "person_name", "organization", "url",
    ]


# --- Ingest tests ---


def _write_extraction(tmp_path, data: dict) -> str:
    """Write extraction JSON to a temp file and return the path."""
    path = str(tmp_path / "extraction.json")
    Path(path).write_text(json.dumps(data))
    return path


@pytest.mark.asyncio
async def test_ingest_valid_extraction(store, tmp_path):
    """should ingest entities, relationships, and leads into the store"""
    inv_id = await store.create_investigation("ingest test")

    # Pre-populate a person so the relationship target exists
    person = _make_person_entity("Thomas Jacob")
    await store.ingest_finding(
        Finding(entities=[person]), investigation_id=inv_id,
    )

    extraction = {
        "extracted_entities": [
            {
                "id": "organization:llm:acme_corp",
                "entity_type": "organization",
                "label": "Acme Corp",
                "properties": {"industry": "defense"},
                "confidence": 0.7,
                "reasoning": "Employer property on Thomas Jacob entity",
            },
        ],
        "extracted_relationships": [
            {
                "source_id": person.id,
                "target_id": "organization:llm:acme_corp",
                "relation_type": "works_at",
                "properties": {},
                "confidence": 0.7,
                "reasoning": "Thomas Jacob has employer=Acme Corp",
            },
        ],
        "extracted_leads": [
            {
                "lead_type": "organization",
                "value": "Acme Corp",
                "score": 0.6,
                "notes": "Employer worth investigating",
            },
        ],
        "analysis_notes": "Test extraction",
    }
    path = _write_extraction(tmp_path, extraction)
    result = await ingest_extraction(store, path, investigation_id=inv_id)

    assert result["entities"] == 1
    assert result["relationships"] == 1
    assert result["leads"] >= 1

    # Verify entity exists in store
    nodes = await store.query("all_nodes")
    ids = {n["id"] for n in nodes}
    assert "organization:llm:acme_corp" in ids

    # Verify relationship exists
    edges = await store.query("all_edges")
    assert any(
        e["source"] == person.id and e["target"] == "organization:llm:acme_corp"
        for e in edges
    )


@pytest.mark.asyncio
async def test_ingest_sets_llm_source(store, tmp_path):
    """should set tool='llm_extraction' with correct confidence on sources"""
    extraction = {
        "extracted_entities": [
            {
                "id": "person:llm:jane_doe",
                "entity_type": "person",
                "label": "Jane Doe",
                "properties": {},
                "confidence": 0.85,
            },
        ],
    }
    path = _write_extraction(tmp_path, extraction)
    await ingest_extraction(store, path)

    nodes = await store.query("all_nodes")
    jane = next(n for n in nodes if n["id"] == "person:llm:jane_doe")
    sources = jane.get("sources", [])
    assert any(s["tool"] == "llm_extraction" for s in sources)
    llm_source = next(s for s in sources if s["tool"] == "llm_extraction")
    assert llm_source["confidence"] == 0.85


@pytest.mark.asyncio
async def test_ingest_stores_reasoning(store, tmp_path):
    """should store reasoning in entity properties as llm_reasoning"""
    extraction = {
        "extracted_entities": [
            {
                "id": "person:llm:bob",
                "entity_type": "person",
                "label": "Bob",
                "properties": {},
                "confidence": 0.6,
                "reasoning": "Found as treasurer_name on FEC committee",
            },
        ],
    }
    path = _write_extraction(tmp_path, extraction)
    await ingest_extraction(store, path)

    nodes = await store.query("all_nodes")
    bob = next(n for n in nodes if n["id"] == "person:llm:bob")
    assert bob.get("llm_reasoning") == "Found as treasurer_name on FEC committee"


@pytest.mark.asyncio
async def test_ingest_idempotent(store, tmp_path):
    """should not duplicate entities when ingested twice"""
    extraction = {
        "extracted_entities": [
            {
                "id": "person:llm:alice",
                "entity_type": "person",
                "label": "Alice",
                "properties": {},
                "confidence": 0.7,
            },
        ],
    }
    path = _write_extraction(tmp_path, extraction)

    await ingest_extraction(store, path)
    count1 = await store.entity_count()

    await ingest_extraction(store, path)
    count2 = await store.entity_count()

    assert count1 == count2


@pytest.mark.asyncio
async def test_ingest_invalid_entity_type_skipped(store, tmp_path):
    """should skip entities with invalid entity_type and continue"""
    extraction = {
        "extracted_entities": [
            {
                "id": "thing:llm:invalid",
                "entity_type": "invalid_type",
                "label": "Invalid",
                "properties": {},
                "confidence": 0.5,
            },
            {
                "id": "person:llm:valid",
                "entity_type": "person",
                "label": "Valid Person",
                "properties": {},
                "confidence": 0.7,
            },
        ],
    }
    path = _write_extraction(tmp_path, extraction)
    result = await ingest_extraction(store, path)

    assert result["entities"] == 1
    assert result["errors"] == 1

    nodes = await store.query("all_nodes")
    ids = {n["id"] for n in nodes}
    assert "person:llm:valid" in ids
    assert "thing:llm:invalid" not in ids


@pytest.mark.asyncio
async def test_ingest_invalid_relation_type_skipped(store, tmp_path):
    """should skip relationships with invalid relation_type"""
    extraction = {
        "extracted_entities": [
            {
                "id": "person:llm:a",
                "entity_type": "person",
                "label": "A",
                "confidence": 0.7,
            },
        ],
        "extracted_relationships": [
            {
                "source_id": "person:llm:a",
                "target_id": "person:llm:b",
                "relation_type": "fake_relation",
                "confidence": 0.5,
            },
        ],
    }
    path = _write_extraction(tmp_path, extraction)
    result = await ingest_extraction(store, path)

    assert result["relationships"] == 0
    assert result["errors"] == 1


@pytest.mark.asyncio
async def test_ingest_auto_extracts_leads(store, tmp_path):
    """should auto-extract leads from entities even without explicit leads"""
    extraction = {
        "extracted_entities": [
            {
                "id": "email:llm:test@example.com",
                "entity_type": "email",
                "label": "test@example.com",
                "properties": {},
                "confidence": 0.8,
            },
        ],
    }
    path = _write_extraction(tmp_path, extraction)
    result = await ingest_extraction(store, path)

    # EMAIL entities auto-generate email leads
    assert result["leads"] >= 1
    leads = await store.get_leads()
    assert any(l["value"] == "test@example.com" for l in leads)


@pytest.mark.asyncio
async def test_ingest_deduplicates_leads(store, tmp_path):
    """should not create duplicate leads when explicit and auto-extracted overlap"""
    extraction = {
        "extracted_entities": [
            {
                "id": "email:llm:dupe@test.com",
                "entity_type": "email",
                "label": "dupe@test.com",
                "properties": {},
                "confidence": 0.8,
            },
        ],
        "extracted_leads": [
            {
                "lead_type": "email",
                "value": "dupe@test.com",
                "score": 0.9,
                "notes": "Explicit lead with custom score",
            },
        ],
    }
    path = _write_extraction(tmp_path, extraction)
    _result = await ingest_extraction(store, path)

    # Should have the explicit lead but not a duplicate auto-extracted one
    leads = await store.get_leads()
    email_leads = [l for l in leads if l["value"] == "dupe@test.com"]
    assert len(email_leads) == 1
    # The explicit lead should have the custom score
    assert email_leads[0]["score"] == 0.9


@pytest.mark.asyncio
async def test_ingest_links_to_investigation(store, tmp_path):
    """should link ingested entities to the specified investigation"""
    inv_id = await store.create_investigation("link test")

    extraction = {
        "extracted_entities": [
            {
                "id": "person:llm:linked",
                "entity_type": "person",
                "label": "Linked Person",
                "properties": {},
                "confidence": 0.7,
            },
        ],
    }
    path = _write_extraction(tmp_path, extraction)
    await ingest_extraction(store, path, investigation_id=inv_id)

    # Entity should be scoped to the investigation
    scoped = await store.query(f"inv:{inv_id}:all_nodes")
    ids = {n["id"] for n in scoped}
    assert "person:llm:linked" in ids


@pytest.mark.asyncio
async def test_export_includes_finding_notes(store):
    """should include finding notes in the export JSON"""
    inv_id = await store.create_investigation("notes test")

    person = _make_person_entity("Note Person", tool="reddit")
    finding = Finding(
        entities=[person],
        notes="Reddit: u/noteperson — 50 posts, timezone PST, top sub r/python",
    )
    await store.ingest_finding(finding, investigation_id=inv_id)

    raw = await export_investigation(store, investigation_id=inv_id)
    data = json.loads(raw)

    assert data["meta"]["finding_notes_count"] == 1
    assert len(data["finding_notes"]) == 1
    assert data["finding_notes"][0]["tool"] == "reddit"
    assert "50 posts" in data["finding_notes"][0]["notes"]


@pytest.mark.asyncio
async def test_export_finding_notes_empty(store):
    """should have empty finding_notes for investigation with no notes"""
    inv_id = await store.create_investigation("empty notes")

    raw = await export_investigation(store, investigation_id=inv_id)
    data = json.loads(raw)

    assert data["meta"]["finding_notes_count"] == 0
    assert data["finding_notes"] == []


@pytest.mark.asyncio
async def test_export_unscoped(store):
    """should export all entities when no investigation_id is given"""
    person = _make_person_entity("Global Person")
    await store.ingest_finding(Finding(entities=[person]))

    raw = await export_investigation(store)
    data = json.loads(raw)

    assert data["meta"]["investigation_id"] is None
    assert data["meta"]["entity_count"] == 1
