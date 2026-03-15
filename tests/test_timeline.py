"""Tests for timeline reconstruction — date parsing, event extraction, rendering."""

from datetime import date

import pytest

from osint_agent.models import Entity, EntityType, Relationship, RelationType, Source
from osint_agent.timeline import (
    DatePrecision,
    TimelineGenerator,
    extract_activity_events,
    extract_events,
    parse_temporal_value,
)


# ------------------------------------------------------------------
# Date parsing
# ------------------------------------------------------------------


def test_parse_iso_datetime():
    """should parse ISO datetime to day precision"""
    result = parse_temporal_value("2023-06-15T12:00:00Z")
    assert result == (date(2023, 6, 15), DatePrecision.DAY)


def test_parse_iso_datetime_with_offset():
    """should handle timezone offset format"""
    result = parse_temporal_value("2023-06-15T12:00:00+00:00")
    assert result == (date(2023, 6, 15), DatePrecision.DAY)


def test_parse_date():
    """should parse YYYY-MM-DD to day precision"""
    result = parse_temporal_value("2023-06-15")
    assert result == (date(2023, 6, 15), DatePrecision.DAY)


def test_parse_year_month():
    """should parse YYYY-MM to month precision"""
    result = parse_temporal_value("2023-06")
    assert result == (date(2023, 6, 1), DatePrecision.MONTH)


def test_parse_year():
    """should parse YYYY to year precision"""
    result = parse_temporal_value("2023")
    assert result == (date(2023, 1, 1), DatePrecision.YEAR)


def test_parse_unix_timestamp():
    """should parse numeric Unix timestamp to day precision"""
    # 2023-06-15 16:00:00 UTC
    result = parse_temporal_value(1686844800)
    assert result is not None
    assert result[0] == date(2023, 6, 15)
    assert result[1] == DatePrecision.DAY


def test_parse_garbage_returns_none():
    """should return None for unparseable values"""
    assert parse_temporal_value("not a date") is None
    assert parse_temporal_value("TBD") is None
    assert parse_temporal_value("") is None
    assert parse_temporal_value(None) is None


# ------------------------------------------------------------------
# Event extraction
# ------------------------------------------------------------------


def _make_entity(eid, etype, label, tool="test", **props):
    return Entity(
        id=eid,
        entity_type=etype,
        label=label,
        properties=props,
        sources=[Source(tool=tool)],
    )


def test_extract_from_entity_properties():
    """should extract events from known temporal property keys"""
    entity = _make_entity(
        "document:edgar:filing1", EntityType.DOCUMENT, "SEC Filing",
        tool="edgar", filing_date="2023-03-15",
    )
    events = extract_events([entity], [])
    assert len(events) == 1
    assert events[0].date == date(2023, 3, 15)
    assert events[0].entity_label == "SEC Filing"
    assert events[0].event_description == "SEC filing"
    assert events[0].source_tool == "edgar"


def test_extract_from_relationship_properties():
    """should extract events from relationship temporal properties"""
    person = _make_entity(
        "person:fec:john", EntityType.PERSON, "John Doe", tool="openfec",
    )
    rel = Relationship(
        source_id="person:fec:john",
        target_id="fec_committee:fec:pac1",
        relation_type=RelationType.DONATED_TO,
        properties={"most_recent_date": "2022-11-01"},
        sources=[Source(tool="openfec")],
    )
    events = extract_events([person], [rel])
    assert len(events) == 1
    assert events[0].date == date(2022, 11, 1)
    assert events[0].entity_label == "John Doe"
    assert events[0].event_description == "FEC donation"


def test_skips_unknown_keys():
    """should not extract events from unrecognized property keys"""
    entity = _make_entity(
        "person:test:a", EntityType.PERSON, "A",
        random_field="2023-01-01", employer="Acme",
    )
    events = extract_events([entity], [])
    assert len(events) == 0


def test_skips_unparseable_dates():
    """should skip properties with unparseable date values"""
    entity = _make_entity(
        "document:test:x", EntityType.DOCUMENT, "X",
        filing_date="TBD", date_filed="pending",
    )
    events = extract_events([entity], [])
    assert len(events) == 0


# ------------------------------------------------------------------
# Rendering
# ------------------------------------------------------------------


def test_markdown_groups_by_year():
    """should group events by year with descending year headers"""
    entities = [
        _make_entity(
            "person:test:a", EntityType.PERSON, "Alice",
            tool="edgar", filing_date="2023-06-15",
        ),
        _make_entity(
            "person:test:b", EntityType.PERSON, "Bob",
            tool="openfec", most_recent_date="2021-03-01",
        ),
    ]
    gen = TimelineGenerator()
    md = gen.generate_from_data(entities, [], fmt="markdown")

    # 2023 should appear before 2021 (descending)
    pos_2023 = md.index("## 2023")
    pos_2021 = md.index("## 2021")
    assert pos_2023 < pos_2021

    assert "**Alice**" in md
    assert "**Bob**" in md
    assert "*[edgar]*" in md


def test_html_contains_events_data():
    """should embed events data in HTML output"""
    entities = [
        _make_entity(
            "person:test:a", EntityType.PERSON, "Alice",
            tool="edgar", filing_date="2023-06-15",
        ),
    ]
    gen = TimelineGenerator()
    html = gen.generate_from_data(entities, [], fmt="html")

    assert "Alice" in html
    assert "2023-06-15" in html
    assert "<html" in html
    # Self-contained: no external script src
    assert 'src="http' not in html


def test_empty_timeline_message():
    """should show message when no temporal properties exist"""
    entity = _make_entity(
        "person:test:a", EntityType.PERSON, "No Dates",
        employer="Acme",
    )
    gen = TimelineGenerator()
    md = gen.generate_from_data([entity], [], fmt="markdown")
    assert "No timeline events found" in md


def test_activity_excluded_by_default():
    """should not include activity events unless include_activity is set"""
    entities = [
        _make_entity(
            "person:test:a", EntityType.PERSON, "Alice",
            tool="reddit", created="2020-01-15T00:00:00Z",
        ),
    ]
    notes = [
        {"tool": "reddit", "notes": "Reddit analysis ran", "created_at": "2024-01-15T10:00:00Z"},
    ]
    gen = TimelineGenerator()

    # Default: no activity
    md_no = gen.generate_from_data(entities, [], finding_notes=notes, fmt="markdown")
    assert "Investigation Activity" not in md_no

    # With flag: activity appears
    md_yes = gen.generate_from_data(
        entities, [], finding_notes=notes,
        fmt="markdown", include_activity=True,
    )
    assert "Investigation Activity" in md_yes
    assert "reddit tool" in md_yes
