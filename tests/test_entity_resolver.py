"""Tests for the entity resolution system."""

import pytest

from osint_agent.graph.resolver import (
    EntityResolver,
    _extract_source,
    _normalize_for_blocking,
    _score_pair,
    _token_overlap,
)
from osint_agent.models import (
    Entity,
    EntityType,
    RelationType,
    Source,
)


@pytest.fixture
def resolver():
    return EntityResolver()


# ------------------------------------------------------------------
# Normalization
# ------------------------------------------------------------------

def test_normalize_basic():
    """should lowercase and strip"""
    assert _normalize_for_blocking("  Jane DOE  ") == "jane doe"


def test_normalize_removes_punctuation():
    """should remove non-alphanumeric chars except spaces"""
    assert _normalize_for_blocking("O'Brien-Smith") == "obriensmith"


def test_normalize_collapses_whitespace():
    """should collapse multiple spaces to one"""
    assert _normalize_for_blocking("Jane   Doe") == "jane doe"


def test_normalize_strips_corp_suffix():
    """should strip common corporate suffixes"""
    assert _normalize_for_blocking("Acme Corp.") == "acme"
    assert _normalize_for_blocking("Acme Corporation") == "acme"
    assert _normalize_for_blocking("Acme Inc.") == "acme"
    assert _normalize_for_blocking("Acme LLC") == "acme"
    assert _normalize_for_blocking("Acme Ltd.") == "acme"


def test_normalize_empty():
    """should return empty for empty input"""
    assert _normalize_for_blocking("") == ""
    assert _normalize_for_blocking(None) == ""


# ------------------------------------------------------------------
# Token overlap
# ------------------------------------------------------------------

def test_token_overlap_identical():
    """should return 1.0 for identical strings"""
    assert _token_overlap("jane doe", "jane doe") == 1.0


def test_token_overlap_subset():
    """should return partial for subset tokens"""
    result = _token_overlap("acme", "acme systems")
    assert 0.4 < result < 0.6  # 1/2 = 0.5


def test_token_overlap_no_match():
    """should return 0.0 for disjoint token sets"""
    assert _token_overlap("alice", "bob") == 0.0


def test_token_overlap_empty():
    """should return 0.0 for empty strings"""
    assert _token_overlap("", "bob") == 0.0
    assert _token_overlap("alice", "") == 0.0


# ------------------------------------------------------------------
# Source extraction
# ------------------------------------------------------------------

def test_extract_source_standard():
    """should extract middle segment from entity ID"""
    assert _extract_source("person:patent:jane_doe") == "patent"
    assert _extract_source("org:fec:C001234") == "fec"
    assert _extract_source("person:sbir:john_smith") == "sbir"


def test_extract_source_short_id():
    """should handle IDs with fewer segments"""
    assert _extract_source("email:foo@bar.com") == "foo@bar.com"
    assert _extract_source("nocolon") == ""


# ------------------------------------------------------------------
# Scoring
# ------------------------------------------------------------------

def _make_person(id: str, label: str, **props) -> Entity:
    return Entity(
        id=id,
        entity_type=EntityType.PERSON,
        label=label,
        properties=props,
        sources=[Source(tool=id.split(":")[1])],
    )


def _make_org(id: str, label: str, **props) -> Entity:
    return Entity(
        id=id,
        entity_type=EntityType.ORGANIZATION,
        label=label,
        properties=props,
        sources=[Source(tool=id.split(":")[1])],
    )


def test_score_exact_name_match():
    """should score 0.6 for exact normalized name, no properties"""
    e1 = _make_person("person:fec:jane_doe", "Jane Doe")
    e2 = _make_person("person:patent:jane_doe", "Jane Doe")
    score = _score_pair(e1, e2, EntityType.PERSON)
    assert score == pytest.approx(0.6, abs=0.01)


def test_score_name_match_with_property_boost():
    """should boost above 0.6 when properties match"""
    e1 = _make_person(
        "person:fec:jane_doe", "Jane Doe",
        city="Vienna", state="VA",
    )
    e2 = _make_person(
        "person:patent:jane_doe", "Jane Doe",
        city="Vienna", state="VA",
    )
    score = _score_pair(e1, e2, EntityType.PERSON)
    assert score > 0.8


def test_score_name_match_with_property_conflict():
    """should penalize when properties conflict"""
    e1 = _make_person(
        "person:fec:jane_doe", "Jane Doe",
        city="Vienna", state="VA",
    )
    e2 = _make_person(
        "person:patent:jane_doe", "Jane Doe",
        city="Reston", state="VA",
    )
    score = _score_pair(e1, e2, EntityType.PERSON)
    # One match (state), one conflict (city)
    assert 0.5 < score < 0.8


def test_score_fuzzy_name():
    """should score lower for fuzzy name match"""
    e1 = _make_org("org:sbir:acme", "Acme Corp")
    e2 = _make_org("org:usaspending:acme_systems", "Acme Systems Inc")
    score = _score_pair(e1, e2, EntityType.ORGANIZATION, name_similarity=0.5)
    assert score < 0.6


def test_score_clamps_to_01():
    """should never return below 0 or above 1"""
    e1 = _make_person(
        "person:a:x", "X",
        city="A", state="B", title="C", phone="D", company="E",
    )
    e2 = _make_person(
        "person:b:x", "X",
        city="Z", state="Y", title="W", phone="V", company="U",
    )
    score = _score_pair(e1, e2, EntityType.PERSON)
    assert 0.0 <= score <= 1.0


# ------------------------------------------------------------------
# Full resolution
# ------------------------------------------------------------------

def test_resolve_links_same_person_across_sources(resolver):
    """should create ALSO_KNOWN_AS between same person with corroboration"""
    # Under corroboration model, name(0.5) + city(0.5) + email(2.0) = 3.0
    entities = [
        _make_person(
            "person:patent:jane_doe", "Jane Doe",
            city="Vienna", email="jane@example.com",
        ),
        _make_person(
            "person:fec:jane_doe", "Jane Doe",
            city="Vienna", email="jane@example.com",
        ),
        _make_person(
            "person:sbir:jane_doe", "Jane Doe",
            email="jane@example.com",
        ),
    ]
    rels = resolver.resolve(entities)
    assert len(rels) == 3  # 3 pairs: patent↔fec, patent↔sbir, fec↔sbir
    for r in rels:
        assert r.relation_type == RelationType.ALSO_KNOWN_AS
        assert r.properties["confidence"] >= 0.6


def test_resolve_person_name_only_no_link(resolver):
    """should NOT link persons with only a common name match"""
    entities = [
        _make_person("person:fec:william_carden", "William Carden"),
        _make_person("person:courtlistener:william_carden", "William Carden"),
    ]
    rels = resolver.resolve(entities)
    assert len(rels) == 0  # name-only = weight 0.5, below threshold 2.0


def test_resolve_person_name_plus_city_no_link(resolver):
    """should NOT link persons with just name + city (too weak)"""
    entities = [
        _make_person(
            "person:fec:william_carden", "William Carden",
            city="Sparks",
        ),
        _make_person(
            "person:courtlistener:william_carden", "William Carden",
            city="Sparks",
        ),
    ]
    rels = resolver.resolve(entities)
    assert len(rels) == 0  # name(0.5) + city(0.5) = 1.0, below 2.0


def test_resolve_person_name_plus_unique_id_links(resolver):
    """should link persons when name + unique identifier match"""
    entities = [
        _make_person(
            "person:fec:jane_doe", "Jane Doe",
            email="jane@example.com",
        ),
        _make_person(
            "person:patent:jane_doe", "Jane Doe",
            email="jane@example.com",
        ),
    ]
    rels = resolver.resolve(entities)
    assert len(rels) == 1  # name(0.5) + email(2.0) = 2.5 → probable
    assert rels[0].properties["corroboration_level"] == "probable"


def test_resolve_person_three_token_name_plus_property(resolver):
    """should link persons with specific 3-token name + property from different sources"""
    entities = [
        _make_person(
            "person:fec:wlc", "William Leighton Carden",
            state="VA",
        ),
        _make_person(
            "person:patent:wlc", "William Leighton Carden",
            state="VA",
        ),
    ]
    rels = resolver.resolve(entities)
    # name(1.0) + source_diversity(1.0) + state(0.5) = 2.5 → probable
    assert len(rels) == 1
    assert rels[0].properties["corroboration_level"] == "probable"


def test_resolve_person_corroboration_details_in_properties(resolver):
    """should include corroboration factors in relationship properties"""
    entities = [
        _make_person(
            "person:fec:jd", "Jane Doe",
            phone="555-1234",
        ),
        _make_person(
            "person:patent:jd", "Jane Doe",
            phone="555-1234",
        ),
    ]
    rels = resolver.resolve(entities)
    assert len(rels) == 1
    props = rels[0].properties
    assert props["method"] == "corroboration"
    assert props["corroboration_level"] in ("probable", "confirmed")
    assert props["corroboration_weight"] >= 2.0
    assert isinstance(props["corroboration_factors"], list)
    factor_fields = {f["field"] for f in props["corroboration_factors"]}
    assert "name" in factor_fields
    assert "phone" in factor_fields


def test_resolve_no_link_within_same_source(resolver):
    """should not link two entities from the same tool source"""
    entities = [
        _make_person("person:fec:jane_doe", "Jane Doe"),
        _make_person("person:fec:jane_smith", "Jane Smith"),
    ]
    rels = resolver.resolve(entities)
    assert len(rels) == 0


def test_resolve_no_link_across_types(resolver):
    """should not link PERSON to ORGANIZATION"""
    entities = [
        _make_person("person:fec:acme", "Acme"),
        _make_org("org:sbir:acme", "Acme"),
    ]
    rels = resolver.resolve(entities)
    assert len(rels) == 0


def test_resolve_different_names_no_link(resolver):
    """should not link entities with different names"""
    entities = [
        _make_person("person:patent:jane_doe", "Jane Doe"),
        _make_person("person:fec:john_smith", "John Smith"),
    ]
    rels = resolver.resolve(entities)
    assert len(rels) == 0


def test_resolve_org_with_suffix_variations(resolver):
    """should link 'Acme Corp' to 'Acme Corporation' via normalization"""
    entities = [
        _make_org("org:sbir:acme_corp", "Acme Corp"),
        _make_org("org:usaspending:acme_corporation", "Acme Corporation"),
    ]
    rels = resolver.resolve(entities)
    assert len(rels) == 1
    assert rels[0].properties["confidence"] >= 0.6


def test_resolve_org_llc_vs_inc(resolver):
    """should link 'Acme LLC' to 'Acme Inc' (both normalize to 'acme')"""
    entities = [
        _make_org("org:sbir:acme_llc", "Acme LLC"),
        _make_org("org:contracts:acme_inc", "Acme Inc."),
    ]
    rels = resolver.resolve(entities)
    assert len(rels) == 1


def test_resolve_skips_documents(resolver):
    """should not attempt to resolve DOCUMENT entities"""
    entities = [
        Entity(
            id="document:patent:123",
            entity_type=EntityType.DOCUMENT,
            label="Patent 123",
            sources=[Source(tool="patents")],
        ),
        Entity(
            id="document:sbir:123",
            entity_type=EntityType.DOCUMENT,
            label="SBIR Award 123",
            sources=[Source(tool="sbir")],
        ),
    ]
    rels = resolver.resolve(entities)
    assert len(rels) == 0


def test_resolve_single_entity_no_links(resolver):
    """should return empty for a single entity"""
    entities = [
        _make_person("person:patent:jane_doe", "Jane Doe"),
    ]
    rels = resolver.resolve(entities)
    assert len(rels) == 0


def test_resolve_confidence_in_properties(resolver):
    """should include confidence score in relationship properties"""
    # Need enough corroboration for persons to link
    entities = [
        _make_person(
            "person:patent:jane_doe", "Jane Doe",
            state="VA", phone="555-1234",
        ),
        _make_person(
            "person:fec:jane_doe", "Jane Doe",
            state="VA", phone="555-1234",
        ),
    ]
    rels = resolver.resolve(entities)
    assert len(rels) == 1
    assert "confidence" in rels[0].properties
    assert 0.0 <= rels[0].properties["confidence"] <= 1.0


def test_resolve_relationship_sources(resolver):
    """should set tool='entity_resolver' in relationship sources"""
    entities = [
        _make_person(
            "person:patent:jane_doe", "Jane Doe",
            email="jane@example.com",
        ),
        _make_person(
            "person:fec:jane_doe", "Jane Doe",
            email="jane@example.com",
        ),
    ]
    rels = resolver.resolve(entities)
    assert rels[0].sources[0].tool == "entity_resolver"


# ------------------------------------------------------------------
# Canonical profile
# ------------------------------------------------------------------

def test_canonical_profile_merges_properties(resolver):
    """should merge properties from all linked entities"""
    entities = [
        _make_person(
            "person:patent:jane_doe", "Jane Doe",
            city="Vienna", state="VA", email="jane@example.com",
        ),
        _make_person(
            "person:fec:jane_doe", "Jane Doe",
            phone="555-1234", email="jane@example.com",
        ),
    ]
    rels = resolver.resolve(entities)
    profile = resolver.get_canonical_profile(
        "person:patent:jane_doe", entities, rels,
    )
    assert profile["merged_properties"]["city"] == "Vienna"
    assert profile["merged_properties"]["state"] == "VA"
    assert profile["merged_properties"]["phone"] == "555-1234"


def test_canonical_profile_lists_aliases(resolver):
    """should list all linked entity IDs as aliases"""
    entities = [
        _make_person(
            "person:patent:jane_doe", "Jane Doe",
            email="jane@example.com",
        ),
        _make_person(
            "person:fec:jane_doe", "Jane Doe",
            email="jane@example.com",
        ),
    ]
    rels = resolver.resolve(entities)
    profile = resolver.get_canonical_profile(
        "person:patent:jane_doe", entities, rels,
    )
    assert "person:fec:jane_doe" in profile["aliases"]


def test_canonical_profile_prefers_longest_label(resolver):
    """should use the longest label as preferred"""
    entities = [
        _make_person("person:patent:r_w_beckwith", "R. W. Beckwith"),
        _make_person(
            "person:fec:reynolds_william_beckwith",
            "Reynolds William Beckwith",
        ),
    ]
    # These won't auto-link (different normalized names), so manually
    # create the link
    from osint_agent.models import Relationship, RelationType, Source
    rels = [Relationship(
        source_id="person:patent:r_w_beckwith",
        target_id="person:fec:reynolds_william_beckwith",
        relation_type=RelationType.ALSO_KNOWN_AS,
        properties={"confidence": 0.7},
        sources=[Source(tool="entity_resolver")],
    )]
    profile = resolver.get_canonical_profile(
        "person:patent:r_w_beckwith", entities, rels,
    )
    assert profile["label"] == "Reynolds William Beckwith"


def test_canonical_profile_collects_sources(resolver):
    """should list all contributing sources with their entity IDs"""
    entities = [
        _make_person(
            "person:patent:jane_doe", "Jane Doe",
            phone="555-1234",
        ),
        _make_person(
            "person:fec:jane_doe", "Jane Doe",
            phone="555-1234",
        ),
    ]
    rels = resolver.resolve(entities)
    profile = resolver.get_canonical_profile(
        "person:patent:jane_doe", entities, rels,
    )
    tools = {s["tool"] for s in profile["sources"]}
    assert "patent" in tools
    assert "fec" in tools


def test_canonical_profile_no_aliases(resolver):
    """should handle entities with no AKA links"""
    entities = [
        _make_person("person:patent:jane_doe", "Jane Doe"),
    ]
    profile = resolver.get_canonical_profile(
        "person:patent:jane_doe", entities, [],
    )
    assert profile["aliases"] == []
    assert profile["label"] == "Jane Doe"


# ------------------------------------------------------------------
# Token-overlap matching (fuzzy)
# ------------------------------------------------------------------

def test_resolve_fuzzy_org_overlap(resolver):
    """should link orgs with high token overlap from different sources"""
    entities = [
        _make_org(
            "org:sbir:objective_interface_systems",
            "Objective Interface Systems",
            state="VA",
        ),
        _make_org(
            "org:usaspending:objective_interface_systems_inc",
            "Objective Interface Systems Inc",
            state="VA",
        ),
    ]
    rels = resolver.resolve(entities)
    # "objective interface systems" normalizes the same after stripping "inc"
    assert len(rels) == 1
    assert rels[0].properties["confidence"] >= 0.6
