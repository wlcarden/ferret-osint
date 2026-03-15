"""Tests for the weighted corroboration model."""

import pytest

from osint_agent.models import Entity, EntityType, Source
from osint_agent.graph.corroboration import (
    CONFIRMED_THRESHOLD,
    PROBABLE_THRESHOLD,
    WEIGHT_SEMI_UNIQUE,
    WEIGHT_UNIQUE,
    WEIGHT_WEAK,
    CorroborationPolicy,
)


def _person(id: str, label: str, **props) -> Entity:
    return Entity(
        id=id,
        entity_type=EntityType.PERSON,
        label=label,
        properties=props,
        sources=[Source(tool=id.split(":")[1])],
    )


@pytest.fixture
def policy():
    return CorroborationPolicy()


# ------------------------------------------------------------------
# Name scoring
# ------------------------------------------------------------------

def test_two_token_name_weight(policy):
    """should score 2-token name as weak (0.5)"""
    e1 = _person("person:a:1", "Jane Doe")
    e2 = _person("person:b:1", "Jane Doe")
    result = policy.evaluate(e1, e2)
    name_factors = [f for f in result.factors if f.field == "name"]
    assert len(name_factors) == 1
    assert name_factors[0].weight == WEIGHT_WEAK
    assert name_factors[0].category == "weak"


def test_three_token_name_weight(policy):
    """should score 3-token name as semi-unique (1.0)"""
    e1 = _person("person:a:1", "William Leighton Carden")
    e2 = _person("person:b:1", "William Leighton Carden")
    result = policy.evaluate(e1, e2)
    name_factors = [f for f in result.factors if f.field == "name"]
    assert len(name_factors) == 1
    assert name_factors[0].weight == WEIGHT_SEMI_UNIQUE
    assert name_factors[0].category == "semi_unique"


def test_fuzzy_name_scales_weight(policy):
    """should scale name weight by similarity"""
    e1 = _person("person:a:1", "Jane Doe")
    e2 = _person("person:b:1", "Jane Doe")
    result = policy.evaluate(e1, e2, name_similarity=0.8)
    name_factors = [f for f in result.factors if f.field == "name"]
    assert name_factors[0].weight == pytest.approx(WEIGHT_WEAK * 0.8, abs=0.01)


def test_low_similarity_no_name_factor(policy):
    """should not create a name factor for very low similarity"""
    e1 = _person("person:a:1", "Jane Doe")
    e2 = _person("person:b:1", "John Smith")
    result = policy.evaluate(e1, e2, name_similarity=0.3)
    name_factors = [f for f in result.factors if f.field == "name"]
    assert len(name_factors) == 0


# ------------------------------------------------------------------
# Property scoring
# ------------------------------------------------------------------

def test_unique_field_weight(policy):
    """should score email match as unique (2.0)"""
    e1 = _person("person:a:1", "X", email="jane@example.com")
    e2 = _person("person:b:1", "X", email="jane@example.com")
    result = policy.evaluate(e1, e2, name_similarity=0.0)
    email_factors = [f for f in result.factors if f.field == "email"]
    assert len(email_factors) == 1
    assert email_factors[0].weight == WEIGHT_UNIQUE
    assert email_factors[0].category == "unique"


def test_semi_unique_field_weight(policy):
    """should score employer match as semi-unique (1.0)"""
    e1 = _person("person:a:1", "X", employer="Acme Corp")
    e2 = _person("person:b:1", "X", employer="Acme Corp")
    result = policy.evaluate(e1, e2, name_similarity=0.0)
    emp_factors = [f for f in result.factors if f.field == "employer"]
    assert len(emp_factors) == 1
    assert emp_factors[0].weight == WEIGHT_SEMI_UNIQUE


def test_weak_field_weight(policy):
    """should score city match as weak (0.5)"""
    e1 = _person("person:a:1", "X", city="Vienna")
    e2 = _person("person:b:1", "X", city="Vienna")
    result = policy.evaluate(e1, e2, name_similarity=0.0)
    city_factors = [f for f in result.factors if f.field == "city"]
    assert len(city_factors) == 1
    assert city_factors[0].weight == WEIGHT_WEAK


def test_unknown_field_treated_as_weak(policy):
    """should treat unclassified fields as weak (0.5)"""
    e1 = _person("person:a:1", "X", custom_field="foobar")
    e2 = _person("person:b:1", "X", custom_field="foobar")
    result = policy.evaluate(e1, e2, name_similarity=0.0)
    custom_factors = [f for f in result.factors if f.field == "custom_field"]
    assert len(custom_factors) == 1
    assert custom_factors[0].weight == WEIGHT_WEAK


def test_mismatched_property_not_counted(policy):
    """should not create factors for non-matching properties"""
    e1 = _person("person:a:1", "X", city="Vienna")
    e2 = _person("person:b:1", "X", city="Reston")
    result = policy.evaluate(e1, e2, name_similarity=0.0)
    city_factors = [f for f in result.factors if f.field == "city"]
    assert len(city_factors) == 0


def test_empty_property_not_counted(policy):
    """should skip empty property values"""
    e1 = _person("person:a:1", "X", city="")
    e2 = _person("person:b:1", "X", city="")
    result = policy.evaluate(e1, e2, name_similarity=0.0)
    city_factors = [f for f in result.factors if f.field == "city"]
    assert len(city_factors) == 0


def test_case_insensitive_property_match(policy):
    """should match properties case-insensitively"""
    e1 = _person("person:a:1", "X", city="VIENNA")
    e2 = _person("person:b:1", "X", city="vienna")
    result = policy.evaluate(e1, e2, name_similarity=0.0)
    city_factors = [f for f in result.factors if f.field == "city"]
    assert len(city_factors) == 1


# ------------------------------------------------------------------
# Classification thresholds
# ------------------------------------------------------------------

def test_insufficient_classification(policy):
    """should classify name-only common name as insufficient"""
    e1 = _person("person:a:1", "William Carden")
    e2 = _person("person:b:1", "William Carden")
    result = policy.evaluate(e1, e2)
    assert result.level == "insufficient"
    assert result.total_weight == 0.5


def test_probable_classification(policy):
    """should classify name + unique ID as probable"""
    e1 = _person("person:a:1", "Jane Doe", email="jane@example.com")
    e2 = _person("person:b:1", "Jane Doe", email="jane@example.com")
    result = policy.evaluate(e1, e2)
    assert result.level == "probable"
    assert result.total_weight == pytest.approx(2.5, abs=0.01)


def test_confirmed_classification(policy):
    """should classify name + email + phone as confirmed"""
    e1 = _person(
        "person:a:1", "Jane Doe",
        email="jane@example.com", phone="555-1234",
    )
    e2 = _person(
        "person:b:1", "Jane Doe",
        email="jane@example.com", phone="555-1234",
    )
    result = policy.evaluate(e1, e2)
    assert result.level == "confirmed"
    assert result.total_weight >= CONFIRMED_THRESHOLD


def test_three_token_name_plus_employer_is_probable(policy):
    """should classify specific name + employer as probable"""
    e1 = _person(
        "person:a:1", "William Leighton Carden",
        employer="Acme Corp",
    )
    e2 = _person(
        "person:b:1", "William Leighton Carden",
        employer="Acme Corp",
    )
    result = policy.evaluate(e1, e2)
    # name(1.0) + employer(1.0) = 2.0
    assert result.level == "probable"
    assert result.total_weight == pytest.approx(2.0, abs=0.01)


def test_name_plus_city_plus_state_insufficient(policy):
    """should classify common name + city + state as insufficient"""
    e1 = _person(
        "person:a:1", "William Carden",
        city="Sparks", state="NV",
    )
    e2 = _person(
        "person:b:1", "William Carden",
        city="Sparks", state="NV",
    )
    result = policy.evaluate(e1, e2)
    # name(0.5) + city(0.5) + state(0.5) = 1.5
    assert result.level == "insufficient"
    assert result.total_weight == pytest.approx(1.5, abs=0.01)


# ------------------------------------------------------------------
# Confidence mapping
# ------------------------------------------------------------------

def test_confidence_insufficient_below_06(policy):
    """should map insufficient weight to confidence < 0.6"""
    e1 = _person("person:a:1", "Jane Doe")
    e2 = _person("person:b:1", "Jane Doe")
    result = policy.evaluate(e1, e2)
    assert result.confidence < 0.6


def test_confidence_probable_between_06_08(policy):
    """should map probable weight to confidence 0.6-0.79"""
    e1 = _person("person:a:1", "Jane Doe", phone="555-1234")
    e2 = _person("person:b:1", "Jane Doe", phone="555-1234")
    result = policy.evaluate(e1, e2)
    assert 0.6 <= result.confidence < 0.8


def test_confidence_confirmed_above_08(policy):
    """should map confirmed weight to confidence >= 0.8"""
    e1 = _person(
        "person:a:1", "Jane Doe",
        email="j@test.com", phone="555-1234",
    )
    e2 = _person(
        "person:b:1", "Jane Doe",
        email="j@test.com", phone="555-1234",
    )
    result = policy.evaluate(e1, e2)
    assert result.confidence >= 0.8


def test_confidence_never_exceeds_1(policy):
    """should cap confidence at 1.0"""
    e1 = _person(
        "person:a:1", "Jane Doe",
        email="j@t.com", phone="555", dob="1990-01-01",
        phone_number="555", ssn="123",
    )
    e2 = _person(
        "person:b:1", "Jane Doe",
        email="j@t.com", phone="555", dob="1990-01-01",
        phone_number="555", ssn="123",
    )
    result = policy.evaluate(e1, e2)
    assert result.confidence <= 1.0


# ------------------------------------------------------------------
# Custom thresholds
# ------------------------------------------------------------------

def test_custom_thresholds():
    """should respect custom probable/confirmed thresholds"""
    policy = CorroborationPolicy(
        probable_threshold=1.0,
        confirmed_threshold=2.0,
    )
    e1 = _person("person:a:1", "William Carden", city="Vienna")
    e2 = _person("person:b:1", "William Carden", city="Vienna")
    result = policy.evaluate(e1, e2)
    # name(0.5) + city(0.5) = 1.0 → meets lowered probable threshold
    assert result.level == "probable"


# ------------------------------------------------------------------
# Multiple factors accumulate
# ------------------------------------------------------------------

def test_multiple_weak_factors_accumulate(policy):
    """should accumulate weak factors toward threshold"""
    e1 = _person(
        "person:a:1", "William Leighton Carden",
        city="Vienna", state="VA", location="Virginia",
    )
    e2 = _person(
        "person:b:1", "William Leighton Carden",
        city="Vienna", state="VA", location="Virginia",
    )
    result = policy.evaluate(e1, e2)
    # name(1.0) + city(0.5) + state(0.5) + location(0.5) = 2.5
    assert result.level == "probable"
    assert len(result.factors) == 4
