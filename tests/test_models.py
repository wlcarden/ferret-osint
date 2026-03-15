"""Smoke tests for core data models."""

from osint_agent.models import (
    Entity,
    EntityType,
    ErrorCategory,
    Finding,
    Relationship,
    RelationType,
    Source,
    ToolError,
)


def test_entity_creation():
    entity = Entity(
        id="email:test@example.com",
        entity_type=EntityType.EMAIL,
        label="test@example.com",
        sources=[Source(tool="holehe")],
    )
    assert entity.entity_type == EntityType.EMAIL
    assert len(entity.sources) == 1


def test_finding_composition():
    person = Entity(
        id="person:jane-doe",
        entity_type=EntityType.PERSON,
        label="Jane Doe",
    )
    email = Entity(
        id="email:jane@example.com",
        entity_type=EntityType.EMAIL,
        label="jane@example.com",
    )
    rel = Relationship(
        source_id=person.id,
        target_id=email.id,
        relation_type=RelationType.HAS_EMAIL,
    )
    finding = Finding(entities=[person, email], relationships=[rel])
    assert len(finding.entities) == 2
    assert len(finding.relationships) == 1


# --- ToolError ---


def test_tool_error_construction():
    err = ToolError(
        tool="openfec",
        category=ErrorCategory.RATE_LIMIT,
        message="HTTP 429",
        http_status=429,
        retry_after=60.0,
        suggestion="Rate limited — wait 60s before retrying",
    )
    assert err.tool == "openfec"
    assert err.category == ErrorCategory.RATE_LIMIT
    assert err.http_status == 429
    assert err.retry_after == 60.0


def test_tool_error_for_http_status_429_with_retry_after():
    err = ToolError.for_http_status(
        tool="openfec",
        status=429,
        headers={"Retry-After": "120"},
    )
    assert err.category == ErrorCategory.RATE_LIMIT
    assert err.retry_after == 120.0
    assert "120" in err.suggestion


def test_tool_error_for_http_status_401():
    err = ToolError.for_http_status(tool="courtlistener", status=401)
    assert err.category == ErrorCategory.AUTH
    assert err.http_status == 401
    assert "API key" in err.suggestion


def test_tool_error_for_http_status_404():
    err = ToolError.for_http_status(tool="patents", status=404)
    assert err.category == ErrorCategory.NOT_FOUND


def test_tool_error_for_http_status_500():
    err = ToolError.for_http_status(tool="usaspending", status=500)
    assert err.category == ErrorCategory.SERVER


def test_tool_error_for_http_status_unknown():
    err = ToolError.for_http_status(tool="test", status=418)
    assert err.category == ErrorCategory.UNKNOWN
    assert "418" in err.message


def test_tool_error_for_http_status_429_no_headers():
    err = ToolError.for_http_status(tool="test", status=429)
    assert err.category == ErrorCategory.RATE_LIMIT
    assert err.retry_after is None


def test_tool_error_for_http_status_429_invalid_retry_after():
    err = ToolError.for_http_status(
        tool="test",
        status=429,
        headers={"Retry-After": "not-a-number"},
    )
    assert err.category == ErrorCategory.RATE_LIMIT
    assert err.retry_after is None


# --- Finding with error ---


def test_finding_error_defaults_to_none():
    finding = Finding(notes="test")
    assert finding.error is None


def test_finding_with_error():
    err = ToolError(
        tool="test",
        category=ErrorCategory.NETWORK,
        message="connection refused",
    )
    finding = Finding(notes="test: connection refused", error=err)
    assert finding.error is not None
    assert finding.error.category == ErrorCategory.NETWORK


def test_finding_with_error_serializes():
    err = ToolError(
        tool="test",
        category=ErrorCategory.TIMEOUT,
        message="timed out",
        suggestion="Try again",
    )
    finding = Finding(notes="test", error=err)
    data = finding.model_dump()
    assert data["error"]["category"] == "timeout"
    assert data["error"]["suggestion"] == "Try again"

    # Round-trip
    restored = Finding.model_validate(data)
    assert restored.error.category == ErrorCategory.TIMEOUT
    assert restored.error.tool == "test"
