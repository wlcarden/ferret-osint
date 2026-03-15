"""Integration tests — exercise the Finding → store → resolve → report pipeline.

These tests use a real SqliteStore (in-memory), real EntityResolver, and real
ReportGenerator. Only tool adapters are absent — we construct realistic Findings
directly, mimicking what adapters produce. This validates:
  - Entity/relationship persistence roundtrip (JSON serialize → SQLite → deserialize)
  - Cross-source entity resolution with weighted corroboration
  - Report generation from resolved data (sections, badges, attribution)
  - Investigation scoping across the pipeline
"""

import pytest
import pytest_asyncio

from osint_agent.graph.corroboration import (
    CONFIRMED_THRESHOLD,
    ORG_CONFIRMED_THRESHOLD,
)
from osint_agent.graph.resolver import EntityResolver
from osint_agent.graph.sqlite_store import SqliteStore
from osint_agent.models import (
    Entity,
    EntityType,
    Finding,
    Relationship,
    RelationType,
    Source,
)
from osint_agent.report import ReportGenerator, _reconstruct_entity

# ── Helpers ──────────────────────────────────────────────────────


async def ingest_and_resolve(
    store: SqliteStore,
    findings: list[Finding],
    investigation_id: int | None = None,
) -> tuple[list[Entity], list[Relationship]]:
    """Ingest findings, query back, resolve, and return entities + AKA rels."""
    for finding in findings:
        await store.ingest_finding(finding, investigation_id=investigation_id)

    if investigation_id is not None:
        rows = await store.query(f"inv:{investigation_id}:all_nodes")
    else:
        rows = await store.query("all_nodes")

    entities = [_reconstruct_entity(r) for r in rows]
    resolver = EntityResolver()
    aka_rels = resolver.resolve(entities)
    return entities, aka_rels


def assert_entities_linked_by_aka(
    aka_rels: list[Relationship],
    id_a: str,
    id_b: str,
) -> Relationship:
    """Assert two entities are linked by ALSO_KNOWN_AS and return the rel."""
    for rel in aka_rels:
        ids = {rel.source_id, rel.target_id}
        if ids == {id_a, id_b}:
            assert rel.relation_type == RelationType.ALSO_KNOWN_AS
            return rel
    raise AssertionError(
        f"No ALSO_KNOWN_AS link between {id_a} and {id_b}. "
        f"Links found: {[(r.source_id, r.target_id) for r in aka_rels]}"
    )


def assert_corroboration_level(
    rel: Relationship,
    expected_level: str,
) -> None:
    """Assert a relationship has the expected corroboration level."""
    actual = rel.properties.get("corroboration_level")
    assert actual == expected_level, (
        f"Expected corroboration_level={expected_level!r}, got {actual!r}. "
        f"Weight: {rel.properties.get('corroboration_weight')}, "
        f"Factors: {rel.properties.get('corroboration_factors')}"
    )


# ── Fixtures ─────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def store(tmp_path):
    """Create a temporary SQLite store."""
    db_path = tmp_path / "integration_test.db"
    s = SqliteStore(db_path=str(db_path))
    yield s
    await s.close()


# ── Cross-source person resolution ──────────────────────────────


@pytest.mark.asyncio
async def test_person_probable_link_with_matching_email(store):
    """should link same-name persons across sources when email matches"""
    finding_fec = Finding(
        entities=[
            Entity(
                id="person:fec:jane_doe",
                entity_type=EntityType.PERSON,
                label="Jane Doe",
                properties={
                    "email": "jane.doe@example.com",
                    "city": "Arlington",
                    "state": "VA",
                },
                sources=[Source(tool="openfec")],
            ),
        ],
        notes="FEC donation by Jane Doe",
    )
    finding_court = Finding(
        entities=[
            Entity(
                id="person:court:jane_doe",
                entity_type=EntityType.PERSON,
                label="Jane Doe",
                properties={
                    "email": "jane.doe@example.com",
                    "state": "VA",
                },
                sources=[Source(tool="courtlistener")],
            ),
        ],
        notes="Court filing for Jane Doe",
    )

    entities, aka_rels = await ingest_and_resolve(
        store, [finding_fec, finding_court],
    )

    assert len(entities) == 2
    assert len(aka_rels) == 1

    rel = assert_entities_linked_by_aka(
        aka_rels, "person:fec:jane_doe", "person:court:jane_doe",
    )
    # name (2-token, weak: 0.5) + email (unique: 2.0) + state (weak: 0.5) = 3.0
    assert_corroboration_level(rel, "confirmed")
    assert rel.properties["confidence"] >= 0.8


@pytest.mark.asyncio
async def test_person_confirmed_link_with_multiple_unique_fields(store):
    """should confirm link when email + phone both match"""
    finding_a = Finding(
        entities=[
            Entity(
                id="person:patent:john_smith",
                entity_type=EntityType.PERSON,
                label="John Smith",
                properties={
                    "email": "jsmith@acme.com",
                    "phone": "555-123-4567",
                    "company": "Acme Corp",
                },
                sources=[Source(tool="patents")],
            ),
        ],
    )
    finding_b = Finding(
        entities=[
            Entity(
                id="person:fec:john_smith",
                entity_type=EntityType.PERSON,
                label="John Smith",
                properties={
                    "email": "jsmith@acme.com",
                    "phone": "555-123-4567",
                    "company": "Acme Corp",
                },
                sources=[Source(tool="openfec")],
            ),
        ],
    )

    entities, aka_rels = await ingest_and_resolve(
        store, [finding_a, finding_b],
    )

    rel = assert_entities_linked_by_aka(
        aka_rels, "person:patent:john_smith", "person:fec:john_smith",
    )
    # name(0.5) + email(2.0) + phone(2.0) + company(1.0) = 5.5 → confirmed
    assert_corroboration_level(rel, "confirmed")
    assert rel.properties["corroboration_weight"] >= CONFIRMED_THRESHOLD


@pytest.mark.asyncio
async def test_person_rejected_name_only_match(store):
    """should NOT link persons with only a common name and no corroboration"""
    finding_a = Finding(
        entities=[
            Entity(
                id="person:fec:john_smith_dc",
                entity_type=EntityType.PERSON,
                label="John Smith",
                properties={"city": "Washington", "state": "DC"},
                sources=[Source(tool="openfec")],
            ),
        ],
    )
    finding_b = Finding(
        entities=[
            Entity(
                id="person:court:john_smith_ca",
                entity_type=EntityType.PERSON,
                label="John Smith",
                properties={"city": "Los Angeles", "state": "CA"},
                sources=[Source(tool="courtlistener")],
            ),
        ],
    )

    entities, aka_rels = await ingest_and_resolve(
        store, [finding_a, finding_b],
    )

    # Name alone (0.5) is below probable threshold (2.0) — no link
    assert len(aka_rels) == 0


@pytest.mark.asyncio
async def test_person_three_token_name_with_employer_probable(store):
    """should create probable link for 3-token name + matching employer"""
    finding_a = Finding(
        entities=[
            Entity(
                id="person:patent:jane_m_doe",
                entity_type=EntityType.PERSON,
                label="Jane Marie Doe",
                properties={"employer": "Northrop Grumman"},
                sources=[Source(tool="patents")],
            ),
        ],
    )
    finding_b = Finding(
        entities=[
            Entity(
                id="person:congress:jane_m_doe",
                entity_type=EntityType.PERSON,
                label="Jane Marie Doe",
                properties={"employer": "Northrop Grumman"},
                sources=[Source(tool="congress")],
            ),
        ],
    )

    entities, aka_rels = await ingest_and_resolve(
        store, [finding_a, finding_b],
    )

    assert len(aka_rels) == 1
    rel = aka_rels[0]
    # name (3-token, semi_unique: 1.0) + employer (semi_unique: 1.0) = 2.0 → probable
    assert_corroboration_level(rel, "probable")


# ── Cross-source organization resolution ────────────────────────


@pytest.mark.asyncio
async def test_org_confirmed_with_matching_ein(store):
    """should confirm org link when EIN matches across sources"""
    finding_a = Finding(
        entities=[
            Entity(
                id="org:sbir:acme_corp",
                entity_type=EntityType.ORGANIZATION,
                label="Acme Corp",
                properties={
                    "ein": "12-3456789",
                    "city": "Arlington",
                    "state": "VA",
                },
                sources=[Source(tool="sbir")],
            ),
        ],
    )
    finding_b = Finding(
        entities=[
            Entity(
                id="org:fec:acme_corporation",
                entity_type=EntityType.ORGANIZATION,
                label="Acme Corporation",
                properties={
                    "ein": "12-3456789",
                    "state": "VA",
                },
                sources=[Source(tool="openfec")],
            ),
        ],
    )

    entities, aka_rels = await ingest_and_resolve(
        store, [finding_a, finding_b],
    )

    assert len(aka_rels) == 1
    rel = aka_rels[0]
    # Org names normalize: "acme corp" → "acme", "acme corporation" → "acme"
    # name (semi_unique, ~1.5 for near-exact) + ein (unique: 2.0) + state (weak: 0.5) = ~4.0
    assert_corroboration_level(rel, "confirmed")
    assert rel.properties["corroboration_weight"] >= ORG_CONFIRMED_THRESHOLD


@pytest.mark.asyncio
async def test_org_probable_with_name_and_address(store):
    """should create probable org link with matching name + address"""
    finding_a = Finding(
        entities=[
            Entity(
                id="org:edgar:megacorp",
                entity_type=EntityType.ORGANIZATION,
                label="MegaCorp LLC",
                properties={"address": "100 Main St, Suite 200"},
                sources=[Source(tool="edgar")],
            ),
        ],
    )
    finding_b = Finding(
        entities=[
            Entity(
                id="org:fara:megacorp",
                entity_type=EntityType.ORGANIZATION,
                label="MegaCorp LLC",
                properties={"address": "100 Main St, Suite 200"},
                sources=[Source(tool="fara")],
            ),
        ],
    )

    entities, aka_rels = await ingest_and_resolve(
        store, [finding_a, finding_b],
    )

    assert len(aka_rels) == 1
    rel = aka_rels[0]
    # org name (exact match, ~1.5) + address (semi_unique: 1.0) = ~2.5 → probable or confirmed
    assert rel.properties["corroboration_level"] in ("probable", "confirmed")


# ── Same-source entities should NOT be linked ────────────────────


@pytest.mark.asyncio
async def test_same_source_entities_not_linked(store):
    """should not create AKA links between entities from the same source"""
    finding = Finding(
        entities=[
            Entity(
                id="person:fec:jane_doe_1",
                entity_type=EntityType.PERSON,
                label="Jane Doe",
                properties={"email": "jane@example.com"},
                sources=[Source(tool="openfec")],
            ),
            Entity(
                id="person:fec:jane_doe_2",
                entity_type=EntityType.PERSON,
                label="Jane Doe",
                properties={"email": "jane@example.com"},
                sources=[Source(tool="openfec")],
            ),
        ],
    )

    entities, aka_rels = await ingest_and_resolve(store, [finding])

    # Both from "fec" source — _extract_source returns "fec" for both
    assert len(aka_rels) == 0


# ── Store roundtrip fidelity ─────────────────────────────────────


@pytest.mark.asyncio
async def test_entity_properties_survive_roundtrip(store):
    """should preserve all entity properties through SQL serialize/deserialize"""
    original = Entity(
        id="person:test:roundtrip",
        entity_type=EntityType.PERSON,
        label="Test Person",
        properties={
            "email": "test@example.com",
            "phone": "555-0000",
            "city": "Portland",
            "tags": ["activist", "organizer"],
            "donation_amount": 2700,
        },
        sources=[Source(tool="test_tool", source_url="https://example.com/test")],
    )

    await store.ingest_finding(Finding(entities=[original]))
    rows = await store.query("all_nodes")
    reconstructed = _reconstruct_entity(rows[0])

    assert reconstructed.id == original.id
    assert reconstructed.entity_type == original.entity_type
    assert reconstructed.label == original.label
    assert reconstructed.properties["email"] == "test@example.com"
    assert reconstructed.properties["phone"] == "555-0000"
    assert reconstructed.properties["tags"] == ["activist", "organizer"]
    assert reconstructed.properties["donation_amount"] == 2700
    assert len(reconstructed.sources) == 1
    assert reconstructed.sources[0].tool == "test_tool"
    assert reconstructed.sources[0].source_url == "https://example.com/test"


@pytest.mark.asyncio
async def test_source_accumulation_on_merge(store):
    """should accumulate sources when ingesting the same entity ID twice"""
    entity_v1 = Entity(
        id="person:fec:accumulate",
        entity_type=EntityType.PERSON,
        label="Merged Person",
        properties={"city": "DC"},
        sources=[Source(tool="openfec")],
    )
    entity_v2 = Entity(
        id="person:fec:accumulate",
        entity_type=EntityType.PERSON,
        label="Merged Person",
        properties={"city": "DC"},
        sources=[Source(tool="openfec", source_url="https://fec.gov/2")],
    )

    await store.ingest_finding(Finding(entities=[entity_v1]))
    await store.ingest_finding(Finding(entities=[entity_v2]))

    rows = await store.query("entity:person:fec:accumulate")
    assert len(rows) == 1
    entity = _reconstruct_entity(rows[0])
    # Two distinct source entries (different source_url values)
    assert len(entity.sources) == 2


# ── Investigation-scoped pipeline ────────────────────────────────


@pytest.mark.asyncio
async def test_investigation_scoped_resolution(store):
    """should scope entity resolution to investigation-linked entities"""
    inv_id = await store.create_investigation("Scoped Test")

    finding = Finding(
        entities=[
            Entity(
                id="person:fec:scoped_jane",
                entity_type=EntityType.PERSON,
                label="Jane Scoped",
                properties={"email": "scoped@test.com"},
                sources=[Source(tool="openfec")],
            ),
            Entity(
                id="person:court:scoped_jane",
                entity_type=EntityType.PERSON,
                label="Jane Scoped",
                properties={"email": "scoped@test.com"},
                sources=[Source(tool="courtlistener")],
            ),
        ],
    )

    await store.ingest_finding(finding, investigation_id=inv_id)

    # Query scoped to investigation
    rows = await store.query(f"inv:{inv_id}:all_nodes")
    assert len(rows) == 2

    entities = [_reconstruct_entity(r) for r in rows]
    resolver = EntityResolver()
    aka_rels = resolver.resolve(entities)

    assert len(aka_rels) == 1
    # name (2-token, weak: 0.5) + email (unique: 2.0) = 2.5 → probable
    assert_corroboration_level(aka_rels[0], "probable")


# ── Full pipeline → report ───────────────────────────────────────


@pytest.mark.asyncio
async def test_full_pipeline_produces_report_with_all_sections(store):
    """should produce a report with profiles, attribution, and source index"""
    inv_id = await store.create_investigation("Full Pipeline Test")

    # Simulate findings from three tools for the same person
    findings = [
        Finding(
            entities=[
                Entity(
                    id="person:fec:maria_garcia",
                    entity_type=EntityType.PERSON,
                    label="Maria Garcia",
                    properties={
                        "email": "mgarcia@lobbyco.com",
                        "employer": "LobbyCo International",
                        "city": "Washington",
                        "state": "DC",
                    },
                    sources=[Source(tool="openfec")],
                ),
                Entity(
                    id="org:fec:lobbyco",
                    entity_type=EntityType.ORGANIZATION,
                    label="LobbyCo International",
                    properties={"fec_id": "C00123456"},
                    sources=[Source(tool="openfec")],
                ),
            ],
            relationships=[
                Relationship(
                    source_id="person:fec:maria_garcia",
                    target_id="org:fec:lobbyco",
                    relation_type=RelationType.WORKS_AT,
                    sources=[Source(tool="openfec")],
                ),
            ],
            notes="FEC records for Maria Garcia",
        ),
        Finding(
            entities=[
                Entity(
                    id="person:fara:maria_garcia",
                    entity_type=EntityType.PERSON,
                    label="Maria Garcia",
                    properties={
                        "email": "mgarcia@lobbyco.com",
                        "phone": "202-555-0199",
                        "employer": "LobbyCo International",
                    },
                    sources=[Source(tool="fara")],
                ),
                Entity(
                    id="org:fara:lobbyco",
                    entity_type=EntityType.ORGANIZATION,
                    label="LobbyCo International",
                    properties={
                        "fara_registration_number": "6789",
                        "city": "Washington",
                    },
                    sources=[Source(tool="fara")],
                ),
            ],
            relationships=[
                Relationship(
                    source_id="person:fara:maria_garcia",
                    target_id="org:fara:lobbyco",
                    relation_type=RelationType.WORKS_AT,
                    sources=[Source(tool="fara")],
                ),
            ],
            notes="FARA registration for Maria Garcia",
        ),
        Finding(
            entities=[
                Entity(
                    id="person:court:maria_garcia",
                    entity_type=EntityType.PERSON,
                    label="Maria Garcia",
                    properties={
                        "state": "DC",
                    },
                    sources=[Source(tool="courtlistener")],
                ),
            ],
            notes="Court filings for Maria Garcia",
        ),
    ]

    for f in findings:
        await store.ingest_finding(f, investigation_id=inv_id)

    # Add a lead
    await store.add_lead(
        lead_type="social_media",
        value="@mgarcia_dc",
        score=3.5,
        investigation_id=inv_id,
        notes="Possible Twitter handle",
    )

    # Query entities and relationships
    entity_rows = await store.query(f"inv:{inv_id}:all_nodes")
    rel_rows = await store.query(f"inv:{inv_id}:all_edges")
    leads = await store.get_leads(investigation_id=inv_id)

    entities = [_reconstruct_entity(r) for r in entity_rows]
    relationships = [
        Relationship(
            source_id=r["source"],
            target_id=r["target"],
            relation_type=RelationType(r["relation_type"]),
            properties={
                k: v for k, v in r.items()
                if k not in {"source", "target", "relation_type", "sources"}
            },
            sources=[
                Source(tool=s.get("tool", "unknown"))
                for s in r.get("sources", [])
            ],
        )
        for r in rel_rows
    ]

    # Resolve entities
    resolver = EntityResolver()
    aka_rels = resolver.resolve(entities)
    all_rels = relationships + aka_rels

    # Generate report
    gen = ReportGenerator(resolver=resolver)
    report = gen.generate_from_data(
        entities=entities,
        relationships=all_rels,
        leads=leads,
        investigation_name="Full Pipeline Test",
    )

    # Verify report structure
    assert "# Investigation Report: Full Pipeline Test" in report
    assert "## Executive Summary" in report
    assert "## Subject Profiles" in report
    assert "Maria Garcia" in report
    assert "## Entity Attribution" in report
    assert "## Entities by Type" in report
    assert "## Relationships" in report
    assert "## Lead Queue" in report
    assert "## Source Index" in report

    # Verify cross-source linking is documented
    assert "openfec" in report
    assert "fara" in report
    assert "courtlistener" in report

    # Verify corroboration evidence appears
    # Maria Garcia has: name(0.5) + email(2.0) + employer(1.0) = 3.5 for fec↔fara → confirmed
    assert "CONFIRMED" in report

    # Verify lead appears
    assert "@mgarcia_dc" in report
    assert "3.5" in report


@pytest.mark.asyncio
async def test_report_shows_rejected_candidates(store):
    """should render rejected candidates section for same-name no-corroboration pairs"""
    findings = [
        Finding(
            entities=[
                Entity(
                    id="person:fec:common_name",
                    entity_type=EntityType.PERSON,
                    label="James Wilson",
                    properties={"city": "New York", "state": "NY"},
                    sources=[Source(tool="openfec")],
                ),
            ],
        ),
        Finding(
            entities=[
                Entity(
                    id="person:court:common_name",
                    entity_type=EntityType.PERSON,
                    label="James Wilson",
                    properties={"city": "Houston", "state": "TX"},
                    sources=[Source(tool="courtlistener")],
                ),
            ],
        ),
    ]

    entities, aka_rels = await ingest_and_resolve(store, findings)
    # Name-only match (0.5) → insufficient → no AKA link
    assert len(aka_rels) == 0

    gen = ReportGenerator()
    report = gen.generate_from_data(
        entities=entities,
        relationships=aka_rels,
        investigation_name="Rejection Test",
    )

    assert "## Rejected Candidates" in report
    assert "James Wilson" in report
    assert "NOT LINKED" in report


# ── Non-resolvable entity types ──────────────────────────────────


@pytest.mark.asyncio
async def test_document_entities_not_resolved(store):
    """should not attempt resolution on DOCUMENT entities"""
    findings = [
        Finding(
            entities=[
                Entity(
                    id="document:edgar:filing_001",
                    entity_type=EntityType.DOCUMENT,
                    label="10-K Annual Report",
                    properties={"filing_date": "2023-06-15"},
                    sources=[Source(tool="edgar")],
                ),
                Entity(
                    id="document:court:filing_001",
                    entity_type=EntityType.DOCUMENT,
                    label="10-K Annual Report",
                    properties={"date_filed": "2023-06-15"},
                    sources=[Source(tool="courtlistener")],
                ),
            ],
        ),
    ]

    entities, aka_rels = await ingest_and_resolve(store, findings)
    # DOCUMENTs are not in RESOLVABLE_TYPES
    assert len(aka_rels) == 0


# ── Multi-cluster resolution ─────────────────────────────────────


@pytest.mark.asyncio
async def test_multiple_distinct_persons_resolved_independently(store):
    """should create separate AKA clusters for distinct persons"""
    findings = [
        # Jane cluster: fec + court
        Finding(
            entities=[
                Entity(
                    id="person:fec:jane_doe",
                    entity_type=EntityType.PERSON,
                    label="Jane Doe",
                    properties={"email": "jane@example.com", "phone": "555-1111"},
                    sources=[Source(tool="openfec")],
                ),
            ],
        ),
        Finding(
            entities=[
                Entity(
                    id="person:court:jane_doe",
                    entity_type=EntityType.PERSON,
                    label="Jane Doe",
                    properties={"email": "jane@example.com", "phone": "555-1111"},
                    sources=[Source(tool="courtlistener")],
                ),
            ],
        ),
        # Bob cluster: patent + congress
        Finding(
            entities=[
                Entity(
                    id="person:patent:bob_jones",
                    entity_type=EntityType.PERSON,
                    label="Bob Jones",
                    properties={"email": "bob@acme.com"},
                    sources=[Source(tool="patents")],
                ),
            ],
        ),
        Finding(
            entities=[
                Entity(
                    id="person:congress:bob_jones",
                    entity_type=EntityType.PERSON,
                    label="Bob Jones",
                    properties={"email": "bob@acme.com"},
                    sources=[Source(tool="congress")],
                ),
            ],
        ),
    ]

    entities, aka_rels = await ingest_and_resolve(store, findings)

    assert len(entities) == 4
    assert len(aka_rels) == 2

    # Jane linked
    jane_rel = assert_entities_linked_by_aka(
        aka_rels, "person:fec:jane_doe", "person:court:jane_doe",
    )
    assert jane_rel.properties["corroboration_level"] in ("confirmed", "probable")

    # Bob linked
    bob_rel = assert_entities_linked_by_aka(
        aka_rels, "person:patent:bob_jones", "person:congress:bob_jones",
    )
    assert bob_rel.properties["corroboration_level"] in ("confirmed", "probable")


# ── Relationship persistence ─────────────────────────────────────


@pytest.mark.asyncio
async def test_relationships_persist_through_pipeline(store):
    """should preserve WORKS_AT and HAS_EMAIL relationships through ingest"""
    inv_id = await store.create_investigation("Rel Test")

    finding = Finding(
        entities=[
            Entity(
                id="person:test:emp_1",
                entity_type=EntityType.PERSON,
                label="Employee One",
                sources=[Source(tool="test")],
            ),
            Entity(
                id="org:test:company",
                entity_type=EntityType.ORGANIZATION,
                label="Test Company",
                sources=[Source(tool="test")],
            ),
            Entity(
                id="email:emp1@test.com",
                entity_type=EntityType.EMAIL,
                label="emp1@test.com",
                sources=[Source(tool="test")],
            ),
        ],
        relationships=[
            Relationship(
                source_id="person:test:emp_1",
                target_id="org:test:company",
                relation_type=RelationType.WORKS_AT,
                sources=[Source(tool="test")],
            ),
            Relationship(
                source_id="person:test:emp_1",
                target_id="email:emp1@test.com",
                relation_type=RelationType.HAS_EMAIL,
                sources=[Source(tool="test")],
            ),
        ],
    )

    await store.ingest_finding(finding, investigation_id=inv_id)

    rel_rows = await store.query(f"inv:{inv_id}:all_edges")
    assert len(rel_rows) == 2

    rel_types = {r["relation_type"] for r in rel_rows}
    assert "works_at" in rel_types
    assert "has_email" in rel_types


# ── Finding notes audit trail ────────────────────────────────────


@pytest.mark.asyncio
async def test_finding_notes_recorded(store):
    """should persist finding notes as an audit trail"""
    inv_id = await store.create_investigation("Notes Test")

    finding = Finding(
        entities=[
            Entity(
                id="person:test:notes_person",
                entity_type=EntityType.PERSON,
                label="Notes Person",
                sources=[Source(tool="openfec")],
            ),
        ],
        notes="Found 3 FEC donations totaling $8,100",
    )

    await store.ingest_finding(finding, investigation_id=inv_id)

    notes = await store.get_finding_notes(investigation_id=inv_id)
    assert len(notes) == 1
    assert notes[0]["tool"] == "openfec"
    assert "$8,100" in notes[0]["notes"]


# ── Corroboration factor detail in report ────────────────────────


@pytest.mark.asyncio
async def test_report_attribution_shows_corroboration_factors(store):
    """should list individual corroboration factors in the attribution section"""
    findings = [
        Finding(
            entities=[
                Entity(
                    id="person:fec:factor_test",
                    entity_type=EntityType.PERSON,
                    label="Factor Person",
                    properties={
                        "email": "factor@test.com",
                        "employer": "TestCorp",
                    },
                    sources=[Source(tool="openfec")],
                ),
            ],
        ),
        Finding(
            entities=[
                Entity(
                    id="person:court:factor_test",
                    entity_type=EntityType.PERSON,
                    label="Factor Person",
                    properties={
                        "email": "factor@test.com",
                        "employer": "TestCorp",
                    },
                    sources=[Source(tool="courtlistener")],
                ),
            ],
        ),
    ]

    entities, aka_rels = await ingest_and_resolve(store, findings)
    assert len(aka_rels) == 1

    gen = ReportGenerator()
    report = gen.generate_from_data(
        entities=entities,
        relationships=aka_rels,
        investigation_name="Factor Test",
    )

    # Report should show factor breakdown
    assert "email" in report.lower()
    assert "employer" in report.lower()
    assert "Total weight:" in report
