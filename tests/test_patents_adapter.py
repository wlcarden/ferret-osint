"""Tests for the PatentsView adapter's parsing and query-building logic."""

import os
from unittest.mock import patch

import pytest

from osint_agent.models import EntityType, RelationType
from osint_agent.tools.patents import PatentsAdapter


@pytest.fixture
def adapter():
    return PatentsAdapter()


# ------------------------------------------------------------------
# Availability
# ------------------------------------------------------------------

def test_is_available_with_key():
    """should return True when PATENTSVIEW_API_KEY is set"""
    with patch.dict(os.environ, {"PATENTSVIEW_API_KEY": "test-key"}):
        a = PatentsAdapter()
        assert a.is_available() is True


def test_is_not_available_without_key():
    """should return False when PATENTSVIEW_API_KEY is empty"""
    with patch.dict(os.environ, {"PATENTSVIEW_API_KEY": ""}, clear=False):
        a = PatentsAdapter()
        assert a.is_available() is False


def test_adapter_name(adapter):
    """should expose 'patents' as its registry name"""
    assert adapter.name == "patents"


# ------------------------------------------------------------------
# Query building
# ------------------------------------------------------------------

def test_build_inventor_query_last_name_only(adapter):
    """should search last name only when query has no space"""
    q = adapter._build_inventor_query("Tesla")
    assert q == {"_contains": {"inventors.inventor_name_last": "Tesla"}}


def test_build_inventor_query_first_and_last(adapter):
    """should split into first/last when query contains a space"""
    q = adapter._build_inventor_query("Nikola Tesla")
    assert q == {
        "_and": [
            {"_contains": {"inventors.inventor_name_last": "Tesla"}},
            {"_contains": {"inventors.inventor_name_first": "Nikola"}},
        ]
    }


def test_build_inventor_query_strips_whitespace(adapter):
    """should strip leading/trailing whitespace from the query"""
    q = adapter._build_inventor_query("  Tesla  ")
    assert q == {"_contains": {"inventors.inventor_name_last": "Tesla"}}


# ------------------------------------------------------------------
# Response parsing (new PatentSearch API format)
# ------------------------------------------------------------------

SAMPLE_RESPONSE = {
    "patents": [
        {
            "patent_id": "12345678",
            "patent_title": "Method for oscillating widgets",
            "patent_date": "2024-01-15",
            "patent_abstract": "A method and apparatus for oscillating widgets.",
            "inventors": [
                {
                    "inventor_name_first": "Jane",
                    "inventor_name_last": "Doe",
                    "inventor_city": "Vienna",
                    "inventor_state": "VA",
                },
            ],
            "assignees": [
                {"assignee_organization": "WidgetCorp"},
            ],
        },
        {
            "patent_id": "87654321",
            "patent_title": "Improved gizmo interface",
            "patent_date": "2023-06-01",
            "patent_abstract": "An interface for gizmos." + ("x" * 600),
            "inventors": [
                {
                    "inventor_name_first": "Jane",
                    "inventor_name_last": "Doe",
                    "inventor_city": "Vienna",
                    "inventor_state": "VA",
                },
                {
                    "inventor_name_first": "John",
                    "inventor_name_last": "Smith",
                    "inventor_city": "Reston",
                    "inventor_state": "VA",
                },
            ],
            "assignees": [
                {"assignee_organization": "WidgetCorp"},
                {"assignee_organization": "GizmoCo"},
            ],
        },
    ],
    "total_hits": 2,
}


def test_parse_creates_document_entities(adapter):
    """should create one DOCUMENT entity per patent"""
    finding = adapter._parse_patents(SAMPLE_RESPONSE)
    docs = [e for e in finding.entities if e.entity_type == EntityType.DOCUMENT]
    assert len(docs) == 2
    assert docs[0].id == "document:patent:12345678"
    assert docs[1].id == "document:patent:87654321"


def test_parse_document_properties(adapter):
    """should populate patent_number, patent_date, and truncated abstract"""
    finding = adapter._parse_patents(SAMPLE_RESPONSE)
    doc = next(
        e for e in finding.entities
        if e.id == "document:patent:87654321"
    )
    assert doc.properties["patent_number"] == "87654321"
    assert doc.properties["patent_date"] == "2023-06-01"
    assert len(doc.properties["abstract"]) <= 500


def test_parse_document_source_url(adapter):
    """should link to Google Patents with US prefix"""
    finding = adapter._parse_patents(SAMPLE_RESPONSE)
    doc = next(
        e for e in finding.entities
        if e.id == "document:patent:12345678"
    )
    assert doc.sources[0].source_url == (
        "https://patents.google.com/patent/US12345678"
    )
    assert doc.sources[0].tool == "patents"


def test_parse_creates_person_entities_deduplicated(adapter):
    """should deduplicate inventors across multiple patents"""
    finding = adapter._parse_patents(SAMPLE_RESPONSE)
    persons = [
        e for e in finding.entities
        if e.entity_type == EntityType.PERSON
    ]
    assert len(persons) == 2
    names = {p.label for p in persons}
    assert "Jane Doe" in names
    assert "John Smith" in names


def test_parse_person_id_format(adapter):
    """should lowercase and underscore-separate the person id"""
    finding = adapter._parse_patents(SAMPLE_RESPONSE)
    person = next(
        e for e in finding.entities
        if e.label == "Jane Doe"
    )
    assert person.id == "person:patent:jane_doe"


def test_parse_person_properties(adapter):
    """should include city and state when available"""
    finding = adapter._parse_patents(SAMPLE_RESPONSE)
    person = next(
        e for e in finding.entities
        if e.label == "Jane Doe"
    )
    assert person.properties["city"] == "Vienna"
    assert person.properties["state"] == "VA"


def test_parse_creates_org_entities_deduplicated(adapter):
    """should deduplicate assignee orgs across patents"""
    finding = adapter._parse_patents(SAMPLE_RESPONSE)
    orgs = [
        e for e in finding.entities
        if e.entity_type == EntityType.ORGANIZATION
    ]
    assert len(orgs) == 2
    org_names = {o.label for o in orgs}
    assert "WidgetCorp" in org_names
    assert "GizmoCo" in org_names


def test_parse_org_id_format(adapter):
    """should normalize org id to lowercase with underscores"""
    finding = adapter._parse_patents(SAMPLE_RESPONSE)
    org = next(
        e for e in finding.entities
        if e.label == "WidgetCorp"
    )
    assert org.id == "org:patent:widgetcorp"


def test_parse_filed_relationships(adapter):
    """should create FILED relationships from inventors to patents"""
    finding = adapter._parse_patents(SAMPLE_RESPONSE)
    filed = [
        r for r in finding.relationships
        if r.relation_type == RelationType.FILED
    ]
    # Patent 1: Jane Doe -> patent 12345678
    # Patent 2: Jane Doe -> patent 87654321, John Smith -> patent 87654321
    assert len(filed) == 3


def test_parse_owns_relationships(adapter):
    """should create OWNS relationships from assignees to patents"""
    finding = adapter._parse_patents(SAMPLE_RESPONSE)
    owns = [
        r for r in finding.relationships
        if r.relation_type == RelationType.OWNS
    ]
    # Patent 1: WidgetCorp -> patent 12345678
    # Patent 2: WidgetCorp -> patent 87654321, GizmoCo -> patent 87654321
    assert len(owns) == 3


def test_parse_relationship_ids(adapter):
    """should reference correct entity ids in relationships"""
    finding = adapter._parse_patents(SAMPLE_RESPONSE)
    filed = [
        r for r in finding.relationships
        if r.relation_type == RelationType.FILED
    ]
    jane_to_patent1 = [
        r for r in filed
        if r.source_id == "person:patent:jane_doe"
        and r.target_id == "document:patent:12345678"
    ]
    assert len(jane_to_patent1) == 1


def test_parse_empty_response(adapter):
    """should handle empty patent list gracefully"""
    finding = adapter._parse_patents({"patents": [], "total_hits": 0})
    assert len(finding.entities) == 0
    assert len(finding.relationships) == 0
    assert finding.notes is not None


def test_parse_null_patents(adapter):
    """should handle null patents field gracefully"""
    finding = adapter._parse_patents({"patents": None, "total_hits": 0})
    assert len(finding.entities) == 0
    assert len(finding.relationships) == 0


def test_parse_missing_inventor_fields(adapter):
    """should skip inventors with no last name"""
    data = {
        "patents": [
            {
                "patent_id": "99999999",
                "patent_title": "Test patent",
                "patent_date": "2024-01-01",
                "patent_abstract": "",
                "inventors": [
                    {
                        "inventor_name_first": "Ghost",
                        "inventor_name_last": "",
                    },
                    {
                        "inventor_name_first": "Valid",
                        "inventor_name_last": "Person",
                    },
                ],
                "assignees": [],
            },
        ],
        "total_hits": 1,
    }
    finding = adapter._parse_patents(data)
    persons = [
        e for e in finding.entities
        if e.entity_type == EntityType.PERSON
    ]
    assert len(persons) == 1
    assert persons[0].label == "Valid Person"


def test_parse_missing_assignee_fields(adapter):
    """should skip assignees with no organization name and no individual name"""
    data = {
        "patents": [
            {
                "patent_id": "99999999",
                "patent_title": "Test patent",
                "patent_date": "2024-01-01",
                "patent_abstract": "",
                "inventors": [],
                "assignees": [
                    {"assignee_organization": ""},
                    {"assignee_organization": "RealCo"},
                ],
            },
        ],
        "total_hits": 1,
    }
    finding = adapter._parse_patents(data)
    orgs = [
        e for e in finding.entities
        if e.entity_type == EntityType.ORGANIZATION
    ]
    assert len(orgs) == 1
    assert orgs[0].label == "RealCo"


def test_parse_notes_include_count(adapter):
    """should include patent count and total in notes"""
    finding = adapter._parse_patents(SAMPLE_RESPONSE)
    assert "2 patents" in finding.notes
    assert "total: 2" in finding.notes
