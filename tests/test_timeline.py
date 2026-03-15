"""Tests for timeline reconstruction — date parsing, event extraction, rendering."""

from datetime import UTC, datetime

from osint_agent.models import Entity, EntityType, Relationship, RelationType, Source
from osint_agent.timeline import (
    DatePrecision,
    TimelineGenerator,
    _format_timestamp,
    extract_events,
    parse_temporal_value,
)

# ------------------------------------------------------------------
# Date parsing — basic formats
# ------------------------------------------------------------------


def test_parse_iso_datetime():
    """should parse ISO datetime to second precision"""
    result = parse_temporal_value("2023-06-15T12:00:00Z")
    assert result is not None
    assert result[0] == datetime(2023, 6, 15, 12, 0, 0, tzinfo=UTC)
    assert result[1] == DatePrecision.SECOND


def test_parse_iso_datetime_with_offset():
    """should handle timezone offset format"""
    result = parse_temporal_value("2023-06-15T12:00:00+00:00")
    assert result is not None
    assert result[0] == datetime(2023, 6, 15, 12, 0, 0, tzinfo=UTC)
    assert result[1] == DatePrecision.SECOND


def test_parse_date():
    """should parse YYYY-MM-DD to day precision"""
    result = parse_temporal_value("2023-06-15")
    assert result is not None
    assert result[0] == datetime(2023, 6, 15, tzinfo=UTC)
    assert result[1] == DatePrecision.DAY


def test_parse_year_month():
    """should parse YYYY-MM to month precision"""
    result = parse_temporal_value("2023-06")
    assert result is not None
    assert result[0] == datetime(2023, 6, 1, tzinfo=UTC)
    assert result[1] == DatePrecision.MONTH


def test_parse_year():
    """should parse YYYY to year precision"""
    result = parse_temporal_value("2023")
    assert result is not None
    assert result[0] == datetime(2023, 1, 1, tzinfo=UTC)
    assert result[1] == DatePrecision.YEAR


def test_parse_unix_timestamp_int():
    """should parse integer Unix timestamp to second precision"""
    # 2023-06-15 16:00:00 UTC
    result = parse_temporal_value(1686844800)
    assert result is not None
    assert result[0] == datetime(2023, 6, 15, 16, 0, 0, tzinfo=UTC)
    assert result[1] == DatePrecision.SECOND


def test_parse_unix_timestamp_float():
    """should parse float Unix timestamp to subsecond precision"""
    result = parse_temporal_value(1686844800.12345)
    assert result is not None
    assert result[1] == DatePrecision.SUBSECOND


def test_parse_garbage_returns_none():
    """should return None for unparseable values"""
    assert parse_temporal_value("not a date") is None
    assert parse_temporal_value("TBD") is None
    assert parse_temporal_value("") is None
    assert parse_temporal_value(None) is None


# ------------------------------------------------------------------
# Date parsing — sub-day precision
# ------------------------------------------------------------------


def test_parse_iso_with_fractional_seconds():
    """should detect subsecond precision from fractional seconds"""
    result = parse_temporal_value("1986-04-26T01:23:40.12345Z")
    assert result is not None
    ts, prec = result
    assert prec == DatePrecision.SUBSECOND
    assert ts.year == 1986
    assert ts.hour == 1
    assert ts.minute == 23
    assert ts.second == 40
    assert ts.microsecond > 0


def test_parse_iso_without_fractional_gives_second():
    """should give SECOND precision for ISO datetime without fractional part"""
    result = parse_temporal_value("1986-04-26T01:23:40Z")
    assert result is not None
    assert result[1] == DatePrecision.SECOND
    assert result[0].second == 40


def test_parse_preserves_time_component():
    """should preserve hours, minutes, seconds from ISO datetime"""
    result = parse_temporal_value("2023-08-15T14:30:45Z")
    assert result is not None
    ts = result[0]
    assert ts.hour == 14
    assert ts.minute == 30
    assert ts.second == 45


# ------------------------------------------------------------------
# Timestamp formatting
# ------------------------------------------------------------------


def test_format_year():
    ts = datetime(2023, 1, 1, tzinfo=UTC)
    assert _format_timestamp(ts, DatePrecision.YEAR) == "2023"


def test_format_month():
    ts = datetime(2023, 6, 1, tzinfo=UTC)
    assert _format_timestamp(ts, DatePrecision.MONTH) == "2023-06"


def test_format_day():
    ts = datetime(2023, 6, 15, tzinfo=UTC)
    assert _format_timestamp(ts, DatePrecision.DAY) == "2023-06-15"


def test_format_second():
    ts = datetime(1986, 4, 26, 1, 23, 40, tzinfo=UTC)
    assert _format_timestamp(ts, DatePrecision.SECOND) == "1986-04-26 01:23:40"


def test_format_subsecond():
    """should format subsecond with up to 5 decimal places, trailing zeros stripped"""
    ts = datetime(1986, 4, 26, 1, 23, 40, 123450, tzinfo=UTC)
    result = _format_timestamp(ts, DatePrecision.SUBSECOND)
    assert result.startswith("1986-04-26 01:23:40.")
    # 123450 microseconds = .12345 seconds (trailing zero stripped from 6th digit)
    assert result == "1986-04-26 01:23:40.12345"


def test_format_subsecond_strips_trailing_zeros():
    """should strip trailing zeros from fractional seconds"""
    ts = datetime(1986, 4, 26, 1, 23, 40, 500000, tzinfo=UTC)
    result = _format_timestamp(ts, DatePrecision.SUBSECOND)
    assert result == "1986-04-26 01:23:40.5"


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
    assert events[0].timestamp == datetime(2023, 3, 15, tzinfo=UTC)
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
    assert events[0].timestamp == datetime(2022, 11, 1, tzinfo=UTC)
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


def test_companion_label_override():
    """should use {key}_label property when present"""
    entity = _make_entity(
        "event:chernobyl:explosion", EntityType.EVENT, "Reactor 4",
        tool="manual", event_time="1986-04-26T01:23:44Z",
        event_time_label="Steam explosion",
    )
    events = extract_events([entity], [])
    assert len(events) == 1
    assert events[0].event_description == "Steam explosion"


def test_generic_temporal_keys():
    """should recognize generic temporal keys like event_time, occurred_at"""
    entity = _make_entity(
        "event:test:e1", EntityType.EVENT, "Test Event",
        tool="manual", event_time="2023-01-15T08:00:00Z",
    )
    events = extract_events([entity], [])
    assert len(events) == 1
    assert events[0].event_description == "Event"
    assert events[0].precision == DatePrecision.SECOND


def test_extract_subsecond_events_ordered():
    """should preserve sub-second ordering for close-together events"""
    e1 = _make_entity(
        "event:test:a", EntityType.EVENT, "First",
        tool="manual", event_time="1986-04-26T01:23:40.00000Z",
    )
    e2 = _make_entity(
        "event:test:b", EntityType.EVENT, "Second",
        tool="manual", event_time="1986-04-26T01:23:44.00000Z",
    )
    events = extract_events([e1, e2], [])
    assert len(events) == 2
    sorted_events = sorted(events, key=lambda e: e.timestamp)
    assert sorted_events[0].entity_label == "First"
    assert sorted_events[1].entity_label == "Second"


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


def test_markdown_subsecond_display():
    """should show full timestamp in markdown for sub-second events"""
    entity = _make_entity(
        "event:test:x", EntityType.EVENT, "Explosion",
        tool="manual", event_time="1986-04-26T01:23:40.12345Z",
    )
    gen = TimelineGenerator()
    md = gen.generate_from_data([entity], [], fmt="markdown")
    assert "01:23:40.12345" in md


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
