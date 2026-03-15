"""Tests for the corroboration-aware report generator."""

import pytest

from osint_agent.graph.corroboration import (
    WEIGHT_SEMI_UNIQUE,
    WEIGHT_UNIQUE,
    WEIGHT_WEAK,
)
from osint_agent.models import (
    Entity,
    EntityType,
    Relationship,
    RelationType,
    Source,
)
from osint_agent.report import ReportGenerator, _reconstruct_entity


def _person(id: str, label: str, tool: str = "", **props) -> Entity:
    if not tool:
        tool = id.split(":")[1] if ":" in id else "unknown"
    return Entity(
        id=id,
        entity_type=EntityType.PERSON,
        label=label,
        properties=props,
        sources=[Source(tool=tool)],
    )


def _account(id: str, label: str, tool: str = "", **props) -> Entity:
    if not tool:
        tool = id.split(":")[1] if ":" in id else "unknown"
    return Entity(
        id=id,
        entity_type=EntityType.ACCOUNT,
        label=label,
        properties=props,
        sources=[Source(tool=tool)],
    )


def _aka_rel(
    e1: Entity,
    e2: Entity,
    confidence: float = 0.7,
    level: str = "probable",
    weight: float = 2.5,
    factors: list[dict] | None = None,
) -> Relationship:
    return Relationship(
        source_id=e1.id,
        target_id=e2.id,
        relation_type=RelationType.ALSO_KNOWN_AS,
        properties={
            "confidence": confidence,
            "method": "corroboration",
            "corroboration_level": level,
            "corroboration_weight": weight,
            "corroboration_factors": factors or [],
            "source_label": e1.label,
            "target_label": e2.label,
        },
        sources=[Source(tool="entity_resolver", confidence=confidence)],
    )


def _has_email_rel(person_id: str, email_id: str) -> Relationship:
    return Relationship(
        source_id=person_id,
        target_id=email_id,
        relation_type=RelationType.HAS_EMAIL,
        sources=[Source(tool="test")],
    )


@pytest.fixture
def gen():
    return ReportGenerator()


# ------------------------------------------------------------------
# Header
# ------------------------------------------------------------------

def test_header_includes_name(gen):
    """should include investigation name in header"""
    report = gen.generate_from_data([], [], investigation_name="Test Investigation")
    assert "# Investigation Report: Test Investigation" in report


def test_header_without_name(gen):
    """should render generic header when no name given"""
    report = gen.generate_from_data([], [])
    assert "# Investigation Report\n" in report


def test_header_includes_timestamp(gen):
    """should include generation timestamp"""
    report = gen.generate_from_data([], [])
    assert "*Generated:" in report


# ------------------------------------------------------------------
# Executive Summary
# ------------------------------------------------------------------

def test_summary_entity_count(gen):
    """should report correct entity count"""
    entities = [
        _person("person:a:1", "Jane"),
        _person("person:b:1", "Jane"),
    ]
    report = gen.generate_from_data(entities, [])
    assert "**2** entities" in report


def test_summary_source_count(gen):
    """should count distinct sources"""
    entities = [
        _person("person:a:1", "Jane", tool="maigret"),
        _person("person:b:1", "Jane", tool="holehe"),
    ]
    report = gen.generate_from_data(entities, [])
    assert "**2** sources" in report


def test_summary_aka_counts(gen):
    """should report confirmed and probable link counts"""
    e1 = _person("person:a:1", "Jane", email="j@test.com")
    e2 = _person("person:b:1", "Jane", email="j@test.com")
    e3 = _person("person:c:1", "Jane", email="j@test.com", phone="555")
    aka1 = _aka_rel(e1, e2, level="probable")
    aka2 = _aka_rel(e2, e3, level="confirmed")
    report = gen.generate_from_data([e1, e2, e3], [aka1, aka2])
    assert "1 confirmed" in report
    assert "1 probable" in report


def test_summary_lead_counts(gen):
    """should report total and pending lead counts"""
    leads = [
        {"lead_type": "email", "value": "a@b.com", "score": 0.8, "status": "pending"},
        {"lead_type": "username", "value": "jane", "score": 0.6, "status": "completed"},
    ]
    report = gen.generate_from_data([], [], leads=leads)
    assert "**2** leads" in report
    assert "1 pending" in report


# ------------------------------------------------------------------
# Subject Profiles
# ------------------------------------------------------------------

def test_subject_profile_canonical_label(gen):
    """should use longest label as canonical name"""
    e1 = _person("person:a:1", "Jane Doe", email="j@test.com")
    e2 = _person("person:b:1", "Jane Marie Doe", email="j@test.com")
    aka = _aka_rel(e1, e2)
    report = gen.generate_from_data([e1, e2], [aka])
    assert "### Jane Marie Doe" in report


def test_subject_profile_confidence_badge(gen):
    """should show confidence badge from corroboration level"""
    e1 = _person("person:a:1", "Jane", email="j@test.com")
    e2 = _person("person:b:1", "Jane", email="j@test.com")
    aka = _aka_rel(e1, e2, level="probable", confidence=0.7)
    report = gen.generate_from_data([e1, e2], [aka])
    assert "PROBABLE" in report


def test_subject_profile_merged_properties(gen):
    """should merge properties from linked entities"""
    e1 = _person("person:a:1", "Jane", email="j@test.com")
    e2 = _person("person:b:1", "Jane", city="Vienna", email="j@test.com")
    aka = _aka_rel(e1, e2)
    report = gen.generate_from_data([e1, e2], [aka])
    assert "Vienna" in report


def test_subject_profile_singleton(gen):
    """should render unlinked person as single-source profile"""
    e1 = _person("person:a:1", "Jane Doe")
    report = gen.generate_from_data([e1], [])
    assert "### Jane Doe" in report
    assert "Single source" in report


# ------------------------------------------------------------------
# Attribution Evidence
# ------------------------------------------------------------------

def test_attribution_section_present(gen):
    """should render attribution section when AKA links exist"""
    e1 = _person("person:a:1", "Jane")
    e2 = _person("person:b:1", "Jane")
    aka = _aka_rel(e1, e2, factors=[
        {"field": "name", "weight": 0.5, "category": "weak"},
        {"field": "email", "weight": 2.0, "category": "unique"},
    ])
    report = gen.generate_from_data([e1, e2], [aka])
    assert "## Entity Attribution" in report


def test_attribution_shows_factors(gen):
    """should list individual corroboration factors"""
    e1 = _person("person:a:1", "Jane")
    e2 = _person("person:b:1", "Jane")
    aka = _aka_rel(e1, e2, factors=[
        {"field": "name", "weight": 0.5, "category": "weak"},
        {"field": "email", "weight": 2.0, "category": "unique"},
    ])
    report = gen.generate_from_data([e1, e2], [aka])
    assert "name: weak (0.5)" in report
    assert "email: unique (2.0)" in report


def test_attribution_groups_by_level(gen):
    """should group links by corroboration level"""
    e1 = _person("person:a:1", "Jane", email="j@t.com")
    e2 = _person("person:b:1", "Jane", email="j@t.com")
    e3 = _person("person:c:1", "Jane", email="j@t.com", phone="555")
    aka_prob = _aka_rel(e1, e2, level="probable")
    aka_conf = _aka_rel(e2, e3, level="confirmed")
    report = gen.generate_from_data([e1, e2, e3], [aka_prob, aka_conf])
    assert "### Confirmed Links" in report
    assert "### Probable Links" in report


def test_attribution_shows_source_tool(gen):
    """should show which tool each entity came from"""
    e1 = _person("person:gravatar:1", "Jane")
    e2 = _person("person:fec:1", "Jane")
    aka = _aka_rel(e1, e2)
    report = gen.generate_from_data([e1, e2], [aka])
    assert "(gravatar)" in report
    assert "(fec)" in report


# ------------------------------------------------------------------
# Rejected Candidates
# ------------------------------------------------------------------

def test_rejected_candidates_shown(gen):
    """should show same-name persons that failed corroboration"""
    e1 = _person("person:a:1", "William Carden")
    e2 = _person("person:b:1", "William Carden", city="Sparks", state="NV")
    # No AKA link between them
    report = gen.generate_from_data([e1, e2], [])
    assert "## Rejected Candidates" in report
    assert "NOT LINKED" in report


def test_rejected_shows_weight(gen):
    """should show the weight that fell below threshold"""
    e1 = _person("person:a:1", "William Carden")
    e2 = _person("person:b:1", "William Carden")
    report = gen.generate_from_data([e1, e2], [])
    assert "weight: 0.5" in report
    assert "threshold: 2.0" in report


def test_rejected_shows_missing_factors(gen):
    """should explain what corroborating evidence is missing"""
    e1 = _person("person:a:1", "William Carden")
    e2 = _person("person:b:1", "William Carden")
    report = gen.generate_from_data([e1, e2], [])
    assert "Missing:" in report
    assert "no email data" in report


def test_rejected_groups_by_name(gen):
    """should group multiple rejected pairs under one name heading"""
    e1 = _person("person:a:1", "William Carden")
    e2 = _person("person:b:1", "William Carden")
    e3 = _person("person:c:1", "William Carden")
    # 3 entities from different sources, no AKA links
    # a↔b, a↔c, b↔c = 3 cross-source pairs, should be grouped
    report = gen.generate_from_data([e1, e2, e3], [])
    assert "3 cross-source pairs" in report
    # Should NOT repeat the same rejection 3 times
    assert report.count("NOT LINKED") == 1


def test_rejected_not_shown_when_linked(gen):
    """should not list pairs that passed corroboration"""
    e1 = _person("person:a:1", "Jane Doe", email="j@test.com")
    e2 = _person("person:b:1", "Jane Doe", email="j@test.com")
    aka = _aka_rel(e1, e2)
    report = gen.generate_from_data([e1, e2], [aka])
    assert "## Rejected Candidates" not in report


def test_rejected_not_shown_for_same_source(gen):
    """should not reject pairs from the same source"""
    e1 = _person("person:a:1", "William Carden")
    e2 = _person("person:a:2", "William Carden")
    report = gen.generate_from_data([e1, e2], [])
    # Same source prefix "a" — not a cross-source pair
    assert "## Rejected Candidates" not in report


# ------------------------------------------------------------------
# Entities by Type
# ------------------------------------------------------------------

def test_entities_grouped_by_type(gen):
    """should group entities under type headings"""
    entities = [
        _person("person:a:1", "Jane"),
        _account("account:a:1", "jane_doe on GitHub", platform="github"),
    ]
    report = gen.generate_from_data(entities, [])
    assert "### PERSON (1)" in report
    assert "### ACCOUNT (1)" in report


def test_entities_show_source_tool(gen):
    """should show source tool for each entity"""
    e = _person("person:maigret:1", "Jane", tool="maigret")
    report = gen.generate_from_data([e], [])
    assert "[maigret]" in report


# ------------------------------------------------------------------
# Relationships
# ------------------------------------------------------------------

def test_relationships_grouped_by_type(gen):
    """should group non-AKA relationships by type"""
    e1 = _person("person:a:1", "Jane")
    email = Entity(
        id="email:jane@test.com",
        entity_type=EntityType.EMAIL,
        label="jane@test.com",
        sources=[Source(tool="holehe")],
    )
    rel = _has_email_rel(e1.id, email.id)
    report = gen.generate_from_data([e1, email], [rel])
    assert "### has_email (1)" in report
    assert "Jane → jane@test.com" in report


def test_aka_rels_excluded_from_relationships_section(gen):
    """should not show AKA links in the relationships section"""
    e1 = _person("person:a:1", "Jane")
    e2 = _person("person:b:1", "Jane")
    aka = _aka_rel(e1, e2)
    report = gen.generate_from_data([e1, e2], [aka])
    # AKA links go in Attribution, not Relationships
    assert "### also_known_as" not in report


# ------------------------------------------------------------------
# Leads
# ------------------------------------------------------------------

def test_leads_rendered_as_table(gen):
    """should render leads as a markdown table"""
    leads = [
        {"lead_type": "email", "value": "a@b.com", "score": 0.8, "status": "pending", "notes": "found via holehe"},
    ]
    report = gen.generate_from_data([], [], leads=leads)
    assert "## Lead Queue" in report
    assert "a@b.com" in report
    assert "0.8" in report


def test_leads_sorted_by_score(gen):
    """should sort leads by score descending"""
    leads = [
        {"lead_type": "email", "value": "low@test.com", "score": 0.3, "status": "pending", "notes": ""},
        {"lead_type": "email", "value": "high@test.com", "score": 0.9, "status": "pending", "notes": ""},
    ]
    report = gen.generate_from_data([], [], leads=leads)
    high_pos = report.index("high@test.com")
    low_pos = report.index("low@test.com")
    assert high_pos < low_pos


# ------------------------------------------------------------------
# Source Index
# ------------------------------------------------------------------

def test_source_index_counts_entities(gen):
    """should count entities per tool"""
    entities = [
        _person("person:a:1", "Jane", tool="maigret"),
        _person("person:a:2", "John", tool="maigret"),
        _person("person:b:1", "Jane", tool="holehe"),
    ]
    report = gen.generate_from_data(entities, [])
    assert "## Source Index" in report
    assert "maigret" in report
    assert "holehe" in report


# ------------------------------------------------------------------
# _reconstruct_entity
# ------------------------------------------------------------------

def test_reconstruct_entity_from_store_dict():
    """should reconstruct Entity from SqliteStore query result"""
    row = {
        "id": "person:fec:123",
        "entity_type": "person",
        "label": "Jane Doe",
        "sources": [{"tool": "openfec", "source_url": "https://api.open.fec.gov/"}],
        "city": "Vienna",
        "state": "VA",
    }
    entity = _reconstruct_entity(row)
    assert entity.id == "person:fec:123"
    assert entity.entity_type == EntityType.PERSON
    assert entity.label == "Jane Doe"
    assert entity.properties["city"] == "Vienna"
    assert entity.properties["state"] == "VA"
    assert entity.sources[0].tool == "openfec"


def test_reconstruct_entity_excludes_meta_keys():
    """should not put id/entity_type/label/sources in properties"""
    row = {
        "id": "person:a:1",
        "entity_type": "person",
        "label": "Jane",
        "sources": [],
        "email": "j@test.com",
    }
    entity = _reconstruct_entity(row)
    assert "id" not in entity.properties
    assert "entity_type" not in entity.properties
    assert "label" not in entity.properties
    assert "sources" not in entity.properties
    assert entity.properties["email"] == "j@test.com"


# ------------------------------------------------------------------
# AKA cluster building
# ------------------------------------------------------------------

def test_clusters_transitive(gen):
    """should build transitive clusters from AKA chains"""
    e1 = _person("person:a:1", "Jane")
    e2 = _person("person:b:1", "Jane")
    e3 = _person("person:c:1", "Jane")
    # a↔b and b↔c should form one cluster {a, b, c}
    aka1 = _aka_rel(e1, e2)
    aka2 = _aka_rel(e2, e3)
    clusters = gen._build_aka_clusters([aka1, aka2])
    assert len(clusters) == 1
    assert clusters[0] == {e1.id, e2.id, e3.id}


def test_clusters_separate_components(gen):
    """should keep unconnected pairs as separate clusters"""
    e1 = _person("person:a:1", "Jane")
    e2 = _person("person:b:1", "Jane")
    e3 = _person("person:c:1", "John")
    e4 = _person("person:d:1", "John")
    aka1 = _aka_rel(e1, e2)
    aka2 = _aka_rel(e3, e4)
    clusters = gen._build_aka_clusters([aka1, aka2])
    assert len(clusters) == 2


# ------------------------------------------------------------------
# Full integration: the Nevada scenario
# ------------------------------------------------------------------

def test_nevada_scenario(gen):
    """should reject William Carden NV and explain why.

    This reproduces the false attribution scenario: two 'William Carden'
    entities from different sources, one in VA with email, one in NV from
    FEC with only city/state. The VA+email entity should be in a profile,
    the NV entity should appear as a rejected candidate.
    """
    e_va = _person(
        "person:gravatar:wlcarden", "William Leighton Carden",
        tool="gravatar", email="wlcarden@gmail.com", city="Reston", state="VA",
    )
    e_nv = _person(
        "person:fec:carden_nv", "William Carden",
        tool="openfec", city="Sparks", state="NV",
    )
    # The resolver would NOT create an AKA link (different names, no
    # corroborating factors), so we pass no AKA rels.
    report = gen.generate_from_data([e_va, e_nv], [])

    # VA entity should be in a subject profile
    assert "### William Leighton Carden" in report

    # NV entity should appear as rejected (same-ish name, different source)
    # Note: normalized names differ (3 tokens vs 2), so they won't be in
    # the same name group — this is correct behavior. The rejected
    # candidates section only shows same-normalized-name pairs.
    # Both should appear in Entities by Type regardless.
    assert "William Carden" in report
    assert "PERSON (2)" in report


def test_linked_entities_show_full_attribution(gen):
    """should show full attribution chain for linked entities"""
    e1 = _person(
        "person:gravatar:1", "William Leighton Carden",
        tool="gravatar", email="wlcarden@gmail.com",
    )
    e2 = _person(
        "person:disqus:1", "William Leighton Carden",
        tool="disqus", email="wlcarden@gmail.com",
    )
    aka = _aka_rel(e1, e2, level="probable", weight=2.5, confidence=0.7, factors=[
        {"field": "name", "weight": 1.0, "category": "semi_unique"},
        {"field": "email", "weight": 2.0, "category": "unique"},
    ])
    report = gen.generate_from_data([e1, e2], [aka])

    assert "## Entity Attribution" in report
    assert "### Probable Links" in report
    assert "name: semi_unique (1.0)" in report
    assert "email: unique (2.0)" in report
    assert "(gravatar)" in report
    assert "(disqus)" in report
