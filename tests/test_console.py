"""Tests for Rich CLI output helpers.

The module docstring for osint_agent.console promises that all Rich output
goes to stderr so that structured outputs (markdown reports, HTML graphs)
written to stdout remain pipeable. Every test here asserts that contract in
addition to the specific content it's checking.
"""

from osint_agent import console
from osint_agent.models import Entity, EntityType, RelationType, Relationship, Source


def _assert_stderr_only(capsys):
    """Assert output landed on stderr and stdout stayed empty."""
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err != ""
    return captured.err


def _entity(
    id_: str,
    entity_type: EntityType,
    label: str,
    properties: dict | None = None,
    sources: list[Source] | None = None,
) -> Entity:
    return Entity(
        id=id_,
        entity_type=entity_type,
        label=label,
        properties=properties or {},
        sources=sources if sources is not None else [Source(tool="test")],
    )


# ------------------------------------------------------------------
# heading
# ------------------------------------------------------------------


def test_heading_level_1_panel(capsys):
    console.heading("Phase 1")
    err = _assert_stderr_only(capsys)
    assert "Phase 1" in err


def test_heading_level_2_rule(capsys):
    console.heading("Sub-section", level=2)
    err = _assert_stderr_only(capsys)
    assert "Sub-section" in err


# ------------------------------------------------------------------
# status / success / warning / error
# ------------------------------------------------------------------


def test_status_without_tool(capsys):
    console.status("Running")
    err = _assert_stderr_only(capsys)
    assert "Running" in err


def test_status_with_tool(capsys):
    console.status("Running", tool="maigret")
    err = _assert_stderr_only(capsys)
    assert "Running" in err
    assert "maigret" in err


def test_success(capsys):
    console.success("Done")
    err = _assert_stderr_only(capsys)
    assert "Done" in err


def test_warning(capsys):
    console.warning("Something odd")
    err = _assert_stderr_only(capsys)
    assert "WARNING" in err
    assert "Something odd" in err


def test_error_without_suggestion(capsys):
    console.error("Failed")
    err = _assert_stderr_only(capsys)
    assert "ERROR" in err
    assert "Failed" in err


def test_error_with_suggestion(capsys):
    console.error("Failed", suggestion="Check your API key")
    err = _assert_stderr_only(capsys)
    assert "ERROR" in err
    assert "Failed" in err
    assert "Check your API key" in err


# ------------------------------------------------------------------
# tool_error
# ------------------------------------------------------------------


class _FakeToolError:
    def __init__(self, category, tool, message, suggestion=""):
        self.category = category
        self.tool = tool
        self.message = message
        self.suggestion = suggestion


def test_tool_error_enum_category(capsys):
    from osint_agent.models import ErrorCategory

    err_obj = _FakeToolError(ErrorCategory.RATE_LIMIT, "shodan", "429 Too Many Requests")
    console.tool_error(err_obj)
    err = _assert_stderr_only(capsys)
    assert "shodan" in err
    assert "429 Too Many Requests" in err
    assert "rate_limit" in err


def test_tool_error_string_category(capsys):
    err_obj = _FakeToolError("network", "crtsh", "connection refused")
    console.tool_error(err_obj)
    err = _assert_stderr_only(capsys)
    assert "crtsh" in err
    assert "connection refused" in err
    assert "network" in err


def test_tool_error_with_suggestion(capsys):
    err_obj = _FakeToolError("auth", "edgar", "invalid key", suggestion="Check EDGAR_API_KEY")
    console.tool_error(err_obj)
    err = _assert_stderr_only(capsys)
    assert "Check EDGAR_API_KEY" in err


# ------------------------------------------------------------------
# cache_hit / normalized
# ------------------------------------------------------------------


def test_cache_hit(capsys):
    console.cache_hit("holehe")
    err = _assert_stderr_only(capsys)
    assert "CACHE HIT" in err
    assert "holehe" in err


def test_normalized(capsys):
    console.normalized("Foo@Example.com", "foo@example.com")
    err = _assert_stderr_only(capsys)
    assert "Foo@Example.com" in err
    assert "foo@example.com" in err


# ------------------------------------------------------------------
# finding
# ------------------------------------------------------------------


def test_finding_empty_entities_and_relationships(capsys):
    console.finding([], [])
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_finding_notes_only(capsys):
    console.finding([], [], notes="Nothing found here")
    err = _assert_stderr_only(capsys)
    assert "Nothing found here" in err


def test_finding_with_entities_and_relationships(capsys):
    entities = [
        _entity("person:1", EntityType.PERSON, "Jane Doe"),
        _entity("org:1", EntityType.ORGANIZATION, "Acme Corp"),
    ]
    relationships = [
        Relationship(
            source_id="person:1",
            target_id="org:1",
            relation_type=RelationType.WORKS_AT,
        ),
    ]
    console.finding(entities, relationships)
    err = _assert_stderr_only(capsys)
    assert "Summary" in err
    assert "Jane Doe" in err
    assert "Acme Corp" in err
    assert "Relationships" in err
    assert "works_at" in err


def test_finding_multi_source_entities(capsys):
    multi = _entity(
        "person:2",
        EntityType.PERSON,
        "Corroborated Person",
        sources=[Source(tool="maigret"), Source(tool="holehe")],
    )
    console.finding([multi], [])
    err = _assert_stderr_only(capsys)
    assert "MULTI-SOURCE" in err
    assert "Corroborated Person" in err
    assert "maigret, holehe" in err


def test_finding_truncates_multi_source_over_ten(capsys):
    multi_entities = [
        _entity(
            f"person:{i}",
            EntityType.PERSON,
            f"Person {i}",
            sources=[Source(tool="a"), Source(tool="b")],
        )
        for i in range(12)
    ]
    console.finding(multi_entities, [])
    err = _assert_stderr_only(capsys)
    assert "... and 2 more" in err


def test_finding_truncates_type_group_over_fifteen(capsys):
    entities = [
        _entity(f"person:{i}", EntityType.PERSON, f"Person {i}")
        for i in range(17)
    ]
    console.finding(entities, [])
    err = _assert_stderr_only(capsys)
    assert "... and 2 more" in err


def test_finding_entity_detail_url(capsys):
    entity = _entity(
        "domain:1", EntityType.DOMAIN, "example.com",
        properties={"url": "https://example.com"},
    )
    console.finding([entity], [])
    err = _assert_stderr_only(capsys)
    assert "https://example.com" in err


def test_finding_entity_detail_platform(capsys):
    entity = _entity(
        "account:1", EntityType.ACCOUNT, "janedoe",
        properties={"platform": "twitter"},
    )
    console.finding([entity], [])
    err = _assert_stderr_only(capsys)
    assert "(twitter)" in err


def test_finding_entity_detail_party_and_state(capsys):
    entity = _entity(
        "person:3", EntityType.PERSON, "Sen. Smith",
        properties={"party": "D", "state": "CA"},
    )
    console.finding([entity], [])
    err = _assert_stderr_only(capsys)
    assert "(D, CA)" in err


def test_finding_entity_detail_no_matching_property(capsys):
    entity = _entity("person:4", EntityType.PERSON, "No Detail Person")
    console.finding([entity], [])
    err = _assert_stderr_only(capsys)
    assert "No Detail Person" in err


# ------------------------------------------------------------------
# investigation_table
# ------------------------------------------------------------------


def test_investigation_table_empty(capsys):
    console.investigation_table([])
    err = _assert_stderr_only(capsys)
    assert "ID" in err
    assert "Name" in err


def test_investigation_table_with_rows(capsys):
    investigations = [
        {"id": 1, "name": "Op Ferret", "created_at": "2026-01-01T12:00:00Z"},
    ]
    console.investigation_table(investigations)
    err = _assert_stderr_only(capsys)
    assert "Op Ferret" in err
    assert "2026-01-01 12:00:00" in err


# ------------------------------------------------------------------
# entity_resolution_table
# ------------------------------------------------------------------


def test_entity_resolution_table_empty(capsys):
    console.entity_resolution_table([])
    err = _assert_stderr_only(capsys)
    assert "0 cross-source links" in err


def test_entity_resolution_table_high_confidence(capsys):
    rel = Relationship(
        source_id="person:a",
        target_id="person:b",
        relation_type=RelationType.ALSO_KNOWN_AS,
        properties={
            "confidence": 0.9,
            "source_label": "Jane Doe",
            "target_label": "J. Doe",
        },
    )
    console.entity_resolution_table([rel])
    err = _assert_stderr_only(capsys)
    assert "Jane Doe" in err
    assert "J. Doe" in err
    assert "HIGH 90%" in err


def test_entity_resolution_table_medium_and_low_confidence(capsys):
    rels = [
        Relationship(
            source_id="person:a",
            target_id="person:b",
            relation_type=RelationType.ALSO_KNOWN_AS,
            properties={"confidence": 0.65},
        ),
        Relationship(
            source_id="person:c",
            target_id="person:d",
            relation_type=RelationType.ALSO_KNOWN_AS,
            properties={"confidence": 0.3},
        ),
    ]
    console.entity_resolution_table(rels)
    err = _assert_stderr_only(capsys)
    assert "MEDIUM 65%" in err
    assert "LOW 30%" in err


def test_entity_resolution_table_falls_back_to_ids(capsys):
    rel = Relationship(
        source_id="person:a",
        target_id="person:b",
        relation_type=RelationType.ALSO_KNOWN_AS,
        properties={"confidence": 0.9},
    )
    console.entity_resolution_table([rel])
    err = _assert_stderr_only(capsys)
    assert "person:a" in err
    assert "person:b" in err


# ------------------------------------------------------------------
# graph_summary
# ------------------------------------------------------------------


def test_graph_summary(capsys):
    console.graph_summary(42, 17)
    err = _assert_stderr_only(capsys)
    assert "42 entities" in err
    assert "17 relationships" in err


def test_graph_summary_zero_counts(capsys):
    console.graph_summary(0, 0)
    err = _assert_stderr_only(capsys)
    assert "0 entities" in err
    assert "0 relationships" in err


# ------------------------------------------------------------------
# key_status / validation_report
# ------------------------------------------------------------------


def test_key_status_ok(capsys):
    console.key_status("Shodan", True, "valid")
    err = _assert_stderr_only(capsys)
    assert "OK" in err
    assert "Shodan" in err
    assert "valid" in err


def test_key_status_fail(capsys):
    console.key_status("Shodan", False, "invalid key")
    err = _assert_stderr_only(capsys)
    assert "FAIL" in err
    assert "Shodan" in err
    assert "invalid key" in err


def test_validation_report_empty(capsys):
    console.validation_report([])
    err = _assert_stderr_only(capsys)
    assert "No API keys configured" in err


def test_validation_report_all_valid(capsys):
    console.validation_report([("Shodan", True, "valid")])
    err = _assert_stderr_only(capsys)
    assert "OK" in err
    assert "Shodan" in err
    assert "WARNING" not in err


def test_validation_report_mixed(capsys):
    results = [
        ("Shodan", True, "valid"),
        ("EDGAR", False, "invalid key"),
    ]
    console.validation_report(results)
    err = _assert_stderr_only(capsys)
    assert "OK" in err and "Shodan" in err
    assert "FAIL" in err and "EDGAR" in err
    assert "1 key(s) failed" in err


# ------------------------------------------------------------------
# phase_heading / step_status
# ------------------------------------------------------------------


def test_phase_heading(capsys):
    console.phase_heading("Reconnaissance")
    err = _assert_stderr_only(capsys)
    assert "Reconnaissance" in err


def test_step_status_run(capsys):
    console.step_status("RUN", "maigret lookup")
    err = _assert_stderr_only(capsys)
    assert "RUN" in err
    assert "maigret lookup" in err


def test_step_status_with_extra(capsys):
    console.step_status("CACHE", "holehe lookup", extra="2.3s")
    err = _assert_stderr_only(capsys)
    assert "CACHE" in err
    assert "holehe lookup" in err
    assert "2.3s" in err


def test_step_status_unknown_action_defaults_style(capsys):
    console.step_status("WEIRD", "something odd")
    err = _assert_stderr_only(capsys)
    assert "WEIRD" in err
    assert "something odd" in err
