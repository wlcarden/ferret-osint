"""Chernobyl disaster timeline — stress test for sub-second precision and EVENT entities.

Reconstructs the April 26, 1986 reactor explosion sequence using EVENT, PERSON,
LOCATION, and ORGANIZATION entities with temporal precision ranging from YEAR
down to SUBSECOND. Validates that the timeline correctly orders, formats, and
renders events that occurred seconds apart.
"""

from osint_agent.models import Entity, EntityType, Relationship, RelationType, Source
from osint_agent.timeline import (
    DatePrecision,
    TimelineGenerator,
    extract_events,
)

# ------------------------------------------------------------------
# Test data: Chernobyl entities
# ------------------------------------------------------------------

_SRC = Source(tool="manual")


def _ev(eid, label, event_time, event_time_label=None, **extra):
    """Create an EVENT entity with temporal properties."""
    props = {"event_time": event_time}
    if event_time_label:
        props["event_time_label"] = event_time_label
    props.update(extra)
    return Entity(
        id=f"event:chernobyl:{eid}",
        entity_type=EntityType.EVENT,
        label=label,
        properties=props,
        sources=[_SRC],
    )


def _person(pid, name):
    return Entity(
        id=f"person:chernobyl:{pid}",
        entity_type=EntityType.PERSON,
        label=name,
        properties={},
        sources=[_SRC],
    )


def _loc(lid, name):
    return Entity(
        id=f"location:chernobyl:{lid}",
        entity_type=EntityType.LOCATION,
        label=name,
        properties={},
        sources=[_SRC],
    )


def _org(oid, name, **props):
    return Entity(
        id=f"organization:chernobyl:{oid}",
        entity_type=EntityType.ORGANIZATION,
        label=name,
        properties=props,
        sources=[_SRC],
    )


# Key actors
DYATLOV = _person("dyatlov", "Anatoly Dyatlov")
AKIMOV = _person("akimov", "Alexander Akimov")
TOPTUNOV = _person("toptunov", "Leonid Toptunov")
BRUKHANOV = _person("brukhanov", "Viktor Brukhanov")
LEGASOV = _person("legasov", "Valery Legasov")

# Locations
REACTOR_4 = _loc("reactor4", "Reactor No. 4")
PRIPYAT = _loc("pripyat", "Pripyat, Ukrainian SSR")
CONTROL_ROOM = _loc("control_room", "Unit 4 Control Room")

# Organizations
MINISTRY = _org(
    "ministry", "USSR Ministry of Energy",
    registration_date="1946",
)
CHERNOBYL_NPP = _org(
    "chernobyl_npp", "Chernobyl Nuclear Power Plant",
    registration_date="1977",
)

# Events — ordered by time, mixing precision levels.
# The test power reduction started ~24h before the explosion.
EVENTS = [
    _ev(
        "power_reduction", "Test power reduction begins",
        "1986-04-25T01:06:00Z",
        event_time_label="Power reduction from 3200 MW to 1600 MW initiated",
    ),
    _ev(
        "power_delay", "Kiev grid controller delays test",
        "1986-04-25T14:00:00Z",
        event_time_label="Grid demand requires continued operation until evening",
    ),
    _ev(
        "resumed_reduction", "Power reduction resumed",
        "1986-04-25T23:10:00Z",
        event_time_label="Reduction to 720 MW for safety test",
    ),
    _ev(
        "power_drop", "Unexpected power drop",
        "1986-04-26T00:28:00Z",
        event_time_label="Power plummets to 30 MW due to xenon poisoning",
    ),
    _ev(
        "partial_recovery", "Partial power recovery",
        "1986-04-26T01:00:00Z",
        event_time_label="Power raised to ~200 MW by withdrawing control rods",
    ),
    _ev(
        "test_start", "Safety test begins",
        "1986-04-26T01:23:04Z",
        event_time_label="Turbine generator test initiated",
    ),
    _ev(
        "az5_pressed", "AZ-5 emergency shutdown pressed",
        "1986-04-26T01:23:40Z",
        event_time_label="AZ-5 button pressed — SCRAM initiated",
    ),
    _ev(
        "power_spike", "Catastrophic power spike",
        "1986-04-26T01:23:43.7Z",
        event_time_label="Power surges to 30,000 MW — 10x design maximum",
    ),
    _ev(
        "steam_explosion", "Steam explosion",
        "1986-04-26T01:23:44.3Z",
        event_time_label="First steam explosion destroys reactor core",
    ),
    _ev(
        "second_explosion", "Second explosion",
        "1986-04-26T01:23:47.1Z",
        event_time_label="Second explosion — hydrogen or nuclear — ejects core material",
    ),
    _ev(
        "fire_response", "Fire brigade arrives",
        "1986-04-26T01:28:00Z",
        event_time_label="Pripyat fire brigade dispatched to reactor building",
    ),
    _ev(
        "evacuation_order", "Pripyat evacuation ordered",
        "1986-04-27T14:00:00Z",
        event_time_label="36-hour delayed evacuation of 49,000 residents",
    ),
    # Coarser precision events
    _ev(
        "liquidators", "Liquidator cleanup begins",
        "1986-05",
        event_time_label="600,000+ workers deployed for decontamination",
    ),
    _ev(
        "sarcophagus_complete", "Sarcophagus completed",
        "1986-11",
        event_time_label="Object Shelter concrete containment finished",
    ),
    _ev(
        "chernobyl_forum", "Chernobyl Forum report",
        "2005",
        event_time_label="UN/IAEA comprehensive health and environmental assessment",
    ),
    _ev(
        "nsf_complete", "New Safe Confinement completed",
        "2016-11-29",
        event_time_label="New arch structure slid over reactor 4",
    ),
]

# Relationships
RELATIONSHIPS = [
    Relationship(
        source_id="person:chernobyl:dyatlov",
        target_id="event:chernobyl:test_start",
        relation_type=RelationType.PARTICIPATED_IN,
        properties={},
        sources=[_SRC],
    ),
    Relationship(
        source_id="person:chernobyl:akimov",
        target_id="event:chernobyl:az5_pressed",
        relation_type=RelationType.PARTICIPATED_IN,
        properties={},
        sources=[_SRC],
    ),
    Relationship(
        source_id="event:chernobyl:steam_explosion",
        target_id="location:chernobyl:reactor4",
        relation_type=RelationType.OCCURRED_AT,
        properties={},
        sources=[_SRC],
    ),
    Relationship(
        source_id="organization:chernobyl:chernobyl_npp",
        target_id="location:chernobyl:pripyat",
        relation_type=RelationType.LOCATED_AT,
        properties={},
        sources=[_SRC],
    ),
    Relationship(
        source_id="person:chernobyl:dyatlov",
        target_id="organization:chernobyl:chernobyl_npp",
        relation_type=RelationType.WORKS_AT,
        properties={"start_date": "1973"},
        sources=[_SRC],
    ),
]


# ------------------------------------------------------------------
# Tests
# ------------------------------------------------------------------


class TestChernobylEventExtraction:
    """Verify event extraction from mixed-precision Chernobyl data."""

    def _all_entities(self):
        return (
            [DYATLOV, AKIMOV, TOPTUNOV, BRUKHANOV, LEGASOV]
            + [REACTOR_4, PRIPYAT, CONTROL_ROOM]
            + [MINISTRY, CHERNOBYL_NPP]
            + EVENTS
        )

    def test_extracts_all_event_entities(self):
        """should extract events from all EVENT entities with event_time"""
        events = extract_events(self._all_entities(), [])
        event_labels = {e.entity_label for e in events if e.entity_type == "event"}
        assert "Steam explosion" in event_labels
        assert "AZ-5 emergency shutdown pressed" in event_labels
        assert len(event_labels) == len(EVENTS)

    def test_extracts_org_registration_dates(self):
        """should extract registration_date from organizations"""
        events = extract_events(self._all_entities(), [])
        org_events = [e for e in events if e.entity_type == "organization"]
        assert len(org_events) == 2
        org_labels = {e.entity_label for e in org_events}
        assert "USSR Ministry of Energy" in org_labels

    def test_relationship_temporal_properties(self):
        """should extract start_date from works_at relationship with context"""
        events = extract_events(self._all_entities(), RELATIONSHIPS)
        rel_events = [
            e for e in events
            if e.property_key == "start_date" and e.entity_label == "Anatoly Dyatlov"
        ]
        assert len(rel_events) == 1
        assert rel_events[0].precision == DatePrecision.YEAR
        # Should include relationship context, not just "Started"
        assert "Chernobyl Nuclear Power Plant" in rel_events[0].event_description
        assert "works at" in rel_events[0].event_description

    def test_companion_labels_used(self):
        """should use event_time_label instead of generic 'Event'"""
        events = extract_events(EVENTS, [])
        for ev in events:
            # None of them should have the generic "Event" label
            assert ev.event_description != "Event", (
                f"{ev.entity_label} has generic description"
            )

    def test_contextual_enrichment(self):
        """should enrich event descriptions with PARTICIPATED_IN and OCCURRED_AT context"""
        events = extract_events(self._all_entities(), RELATIONSHIPS)

        steam = next(e for e in events if e.entity_label == "Steam explosion")
        # OCCURRED_AT → Reactor No. 4
        assert "at Reactor No. 4" in steam.event_description

        test_start = next(e for e in events if e.entity_label == "Safety test begins")
        # PARTICIPATED_IN → Dyatlov
        assert "Anatoly Dyatlov" in test_start.event_description

        az5 = next(e for e in events if e.entity_label == "AZ-5 emergency shutdown pressed")
        # PARTICIPATED_IN → Akimov
        assert "Alexander Akimov" in az5.event_description

    def test_total_event_count(self):
        """should extract correct total number of timeline events"""
        events = extract_events(self._all_entities(), RELATIONSHIPS)
        # 16 EVENT entities + 2 org registration_date + 1 rel start_date = 19
        assert len(events) == 19


class TestChernobylPrecisionLevels:
    """Verify correct precision detection across all granularity levels."""

    def test_mixed_precision_detection(self):
        """should detect all five precision levels across the event set"""
        events = extract_events(EVENTS, [])
        precisions = {e.entity_label: e.precision for e in events}

        # YEAR precision
        assert precisions["Chernobyl Forum report"] == DatePrecision.YEAR

        # MONTH precision
        assert precisions["Liquidator cleanup begins"] == DatePrecision.MONTH
        assert precisions["Sarcophagus completed"] == DatePrecision.MONTH

        # DAY precision
        assert precisions["New Safe Confinement completed"] == DatePrecision.DAY

        # SECOND precision (ISO datetime without fractional seconds)
        assert precisions["AZ-5 emergency shutdown pressed"] == DatePrecision.SECOND
        assert precisions["Safety test begins"] == DatePrecision.SECOND

        # SUBSECOND precision (ISO datetime with fractional seconds)
        assert precisions["Catastrophic power spike"] == DatePrecision.SUBSECOND
        assert precisions["Steam explosion"] == DatePrecision.SUBSECOND
        assert precisions["Second explosion"] == DatePrecision.SUBSECOND

    def test_explosion_sequence_ordering(self):
        """should correctly order events 3 seconds apart in the explosion sequence"""
        explosion_events = [
            e for e in EVENTS
            if e.id in {
                "event:chernobyl:az5_pressed",
                "event:chernobyl:power_spike",
                "event:chernobyl:steam_explosion",
                "event:chernobyl:second_explosion",
            }
        ]
        events = extract_events(explosion_events, [])
        sorted_events = sorted(events, key=lambda e: e.timestamp)

        labels = [e.entity_label for e in sorted_events]
        assert labels == [
            "AZ-5 emergency shutdown pressed",
            "Catastrophic power spike",
            "Steam explosion",
            "Second explosion",
        ]

    def test_subsecond_deltas_between_explosions(self):
        """should preserve sub-second differences between explosion events"""
        events = extract_events(EVENTS, [])
        by_label = {e.entity_label: e for e in events}

        az5 = by_label["AZ-5 emergency shutdown pressed"]
        spike = by_label["Catastrophic power spike"]
        steam = by_label["Steam explosion"]
        second = by_label["Second explosion"]

        # AZ-5 01:23:40 → spike 01:23:43.7 = 3.7s
        assert abs((spike.timestamp - az5.timestamp).total_seconds() - 3.7) < 0.01
        # spike 01:23:43.7 → steam 01:23:44.3 = 0.6s
        assert abs((steam.timestamp - spike.timestamp).total_seconds() - 0.6) < 0.01
        # steam 01:23:44.3 → second 01:23:47.1 = 2.8s
        assert abs((second.timestamp - steam.timestamp).total_seconds() - 2.8) < 0.01


class TestChernobylMarkdownRender:
    """Verify markdown output structure for the full Chernobyl timeline."""

    def _generate(self):
        all_entities = (
            [DYATLOV, AKIMOV, TOPTUNOV, BRUKHANOV, LEGASOV]
            + [REACTOR_4, PRIPYAT, CONTROL_ROOM]
            + [MINISTRY, CHERNOBYL_NPP]
            + EVENTS
        )
        gen = TimelineGenerator()
        return gen.generate_from_data(
            all_entities, RELATIONSHIPS,
            investigation_name="Chernobyl Disaster",
            fmt="markdown",
        )

    def test_title_and_event_count(self):
        md = self._generate()
        assert "# Timeline: Chernobyl Disaster" in md
        assert "19 events" in md
        # Span header should respect precision — "1946" not "1946-01-01"
        assert "spanning 1946 to 2016-11-29" in md

    def test_year_groups(self):
        md = self._generate()
        assert "## 2016" in md
        assert "## 2005" in md
        assert "## 1986" in md
        # 2016 before 2005 before 1986 (descending)
        assert md.index("## 2016") < md.index("## 2005") < md.index("## 1986")

    def test_subsecond_timestamps_displayed(self):
        """should show time-only in day subgroups, with fractional seconds"""
        md = self._generate()
        # Inside day subgroups, times are shown without date prefix
        assert "**01:23:40**" in md  # AZ-5 press (SECOND, time-only)
        assert "**01:23:04**" in md  # Test start (SECOND, time-only)
        assert "**01:23:43.7**" in md  # Power spike (SUBSECOND, time-only)
        assert "**01:23:44.3**" in md  # Steam explosion (SUBSECOND, time-only)

    def test_day_subgroups_in_april_1986(self):
        """should add day subheadings when sub-day events span multiple days"""
        md = self._generate()
        # April 1986 has events on April 25, 26, 27 with sub-day precision
        assert "#### 1986-04-25" in md
        assert "#### 1986-04-26" in md
        assert "#### 1986-04-27" in md
        # Day headers should be ordered chronologically within the month
        assert md.index("#### 1986-04-25") < md.index("#### 1986-04-26")
        assert md.index("#### 1986-04-26") < md.index("#### 1986-04-27")

    def test_companion_labels_in_output(self):
        """should display custom event descriptions with contextual enrichment"""
        md = self._generate()
        assert "AZ-5 button pressed" in md
        assert "First steam explosion destroys reactor core" in md
        assert "49,000 residents" in md
        # Contextual enrichment from OCCURRED_AT relationship
        assert "at Reactor No. 4" in md

    def test_month_precision_formatted(self):
        md = self._generate()
        assert "**1986-05**" in md  # Liquidator cleanup
        assert "**1986-11**" in md  # Sarcophagus

    def test_year_precision_formatted(self):
        md = self._generate()
        assert "**2005**" in md  # Chernobyl Forum
        # Start date for Dyatlov relationship — enriched with context
        assert "**1973**" in md
        assert "works at" in md
        assert "Chernobyl Nuclear Power Plant" in md


class TestChernobylHtmlRender:
    """Verify HTML output for the full Chernobyl timeline."""

    def _generate(self):
        gen = TimelineGenerator()
        return gen.generate_from_data(
            EVENTS, RELATIONSHIPS,
            investigation_name="Chernobyl Disaster",
            fmt="html",
        )

    def test_html_structure(self):
        html = self._generate()
        assert "<html" in html
        assert "Chernobyl Disaster" in html
        assert 'src="http' not in html  # self-contained

    def test_event_type_in_html(self):
        """should include 'event' entity type in the JSON data"""
        html = self._generate()
        assert '"entity_type":"event"' in html

    def test_all_events_in_html(self):
        """should embed all events in the HTML (16 event entities + 1 relationship start_date)"""
        html = self._generate()
        assert '"event_count"' not in html  # not a JSON key, just the template
        assert "17 events" in html

    def test_subsecond_display_in_html(self):
        """should show both second and subsecond timestamps in date_display"""
        html = self._generate()
        assert "01:23:40" in html       # AZ-5 press (SECOND)
        assert "01:23:43.7" in html     # Power spike (SUBSECOND)
        assert "01:23:44.3" in html     # Steam explosion (SUBSECOND)

    def test_html_day_subgroups(self):
        """should add day-header divs when sub-day events span multiple days"""
        html = self._generate()
        assert "day-header" in html
        assert "1986-04-25" in html
        assert "1986-04-26" in html
