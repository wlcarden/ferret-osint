"""Tests for WHOIS adapter's parsing logic and privacy detection."""

import sys
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from osint_agent.models import EntityType, RelationType
from osint_agent.tools.whois_lookup import (
    WhoisAdapter,
    _is_privacy_redacted,
    _normalize_date,
)


def _mock_whois_entry(**overrides):
    """Create a mock WhoisEntry with sensible defaults."""
    defaults = {
        "registrar": "Example Registrar Inc.",
        "creation_date": datetime(2020, 1, 15, 12, 0, 0),
        "expiration_date": datetime(2025, 1, 15, 12, 0, 0),
        "updated_date": datetime(2024, 6, 1, 8, 30, 0),
        "name_servers": ["NS1.EXAMPLE.COM", "NS2.EXAMPLE.COM"],
        "registrant_name": "Jane Doe",
        "registrant_org": "Doe Industries LLC",
        "registrant_country": "US",
        "registrant_state": "CA",
        "registrant_city": "San Francisco",
        "emails": ["admin@example.com", "tech@example.com"],
        "dnssec": "unsigned",
        "status": [
            "clientTransferProhibited https://icann.org/epp#clientTransferProhibited",
        ],
        "org": None,
        "name": None,
    }
    defaults.update(overrides)
    entry = MagicMock()
    for attr, value in defaults.items():
        setattr(entry, attr, value)
    return entry


# --- Privacy detection ---


def test_is_privacy_redacted_true_for_privacy_services():
    assert _is_privacy_redacted("REDACTED FOR PRIVACY") is True
    assert _is_privacy_redacted("WhoisGuard Protected") is True
    assert _is_privacy_redacted("Domains By Proxy, LLC") is True
    assert _is_privacy_redacted("Contact Privacy Inc.") is True
    assert _is_privacy_redacted("Data Protected") is True
    assert _is_privacy_redacted("Identity Protect Limited") is True
    assert _is_privacy_redacted("WITHHELD FOR PRIVACY") is True
    assert _is_privacy_redacted("NOT DISCLOSED") is True


def test_is_privacy_redacted_false_for_real_names():
    assert _is_privacy_redacted("Acme Corporation") is False
    assert _is_privacy_redacted("Jane Doe") is False
    assert _is_privacy_redacted("Google LLC") is False


def test_is_privacy_redacted_true_for_empty_or_none():
    assert _is_privacy_redacted("") is True
    assert _is_privacy_redacted(None) is True


# --- Date normalization ---


def test_normalize_date_single_datetime():
    dt = datetime(2023, 5, 20, 14, 30, 0)
    result = _normalize_date(dt)
    assert result == "2023-05-20T14:30:00"


def test_normalize_date_list_of_datetimes():
    dates = [datetime(2020, 1, 1), datetime(2021, 1, 1)]
    result = _normalize_date(dates)
    assert result == "2020-01-01T00:00:00"


def test_normalize_date_none():
    assert _normalize_date(None) is None


def test_normalize_date_empty_list():
    assert _normalize_date([]) is None


def test_normalize_date_string_fallback():
    assert _normalize_date("2023-01-01") == "2023-01-01"


# --- Parsing: Domain entity ---


def test_parse_creates_domain_entity():
    adapter = WhoisAdapter()
    finding = adapter._parse_results("example.com", _mock_whois_entry())
    domains = [e for e in finding.entities if e.id == "domain:example.com"]
    assert len(domains) == 1
    assert domains[0].entity_type == EntityType.DOMAIN
    assert domains[0].label == "example.com"


def test_parse_domain_properties():
    adapter = WhoisAdapter()
    finding = adapter._parse_results("example.com", _mock_whois_entry())
    domain = [e for e in finding.entities if e.id == "domain:example.com"][0]
    props = domain.properties
    assert props["registrar"] == "Example Registrar Inc."
    assert props["creation_date"] == "2020-01-15T12:00:00"
    assert props["expiration_date"] == "2025-01-15T12:00:00"
    assert props["name_servers"] == ["ns1.example.com", "ns2.example.com"]
    assert props["registrant_country"] == "US"
    assert props["dnssec"] == "unsigned"


# --- Parsing: Organization entity ---


def test_parse_creates_org_entity():
    adapter = WhoisAdapter()
    finding = adapter._parse_results("example.com", _mock_whois_entry())
    orgs = [e for e in finding.entities if e.entity_type == EntityType.ORGANIZATION]
    assert len(orgs) == 1
    assert orgs[0].label == "Doe Industries LLC"
    assert orgs[0].id == "org:whois:doe_industries_llc"


def test_parse_org_owns_domain_relationship():
    adapter = WhoisAdapter()
    finding = adapter._parse_results("example.com", _mock_whois_entry())
    owns = [
        r for r in finding.relationships
        if r.relation_type == RelationType.OWNS
        and r.target_id == "domain:example.com"
        and r.source_id.startswith("org:")
    ]
    assert len(owns) == 1


def test_parse_skips_org_when_privacy_redacted():
    adapter = WhoisAdapter()
    entry = _mock_whois_entry(registrant_org="REDACTED FOR PRIVACY")
    finding = adapter._parse_results("example.com", entry)
    orgs = [e for e in finding.entities if e.entity_type == EntityType.ORGANIZATION]
    assert len(orgs) == 0


def test_parse_skips_org_when_none():
    adapter = WhoisAdapter()
    entry = _mock_whois_entry(registrant_org=None)
    finding = adapter._parse_results("example.com", entry)
    orgs = [e for e in finding.entities if e.entity_type == EntityType.ORGANIZATION]
    assert len(orgs) == 0


# --- Parsing: Person entity ---


def test_parse_creates_person_entity():
    adapter = WhoisAdapter()
    finding = adapter._parse_results("example.com", _mock_whois_entry())
    persons = [e for e in finding.entities if e.entity_type == EntityType.PERSON]
    assert len(persons) == 1
    assert persons[0].label == "Jane Doe"
    assert persons[0].id == "person:whois:jane_doe"


def test_parse_person_owns_domain_relationship():
    adapter = WhoisAdapter()
    finding = adapter._parse_results("example.com", _mock_whois_entry())
    owns = [
        r for r in finding.relationships
        if r.relation_type == RelationType.OWNS
        and r.source_id.startswith("person:")
    ]
    assert len(owns) == 1
    assert owns[0].target_id == "domain:example.com"


def test_parse_skips_person_when_privacy_redacted():
    adapter = WhoisAdapter()
    entry = _mock_whois_entry(registrant_name="Contact Privacy Inc.")
    finding = adapter._parse_results("example.com", entry)
    persons = [e for e in finding.entities if e.entity_type == EntityType.PERSON]
    assert len(persons) == 0


# --- Parsing: Email entities ---


def test_parse_creates_email_entities():
    adapter = WhoisAdapter()
    finding = adapter._parse_results("example.com", _mock_whois_entry())
    emails = [e for e in finding.entities if e.entity_type == EntityType.EMAIL]
    assert len(emails) == 2
    labels = {e.label for e in emails}
    assert labels == {"admin@example.com", "tech@example.com"}


def test_parse_email_has_email_relationships():
    adapter = WhoisAdapter()
    finding = adapter._parse_results("example.com", _mock_whois_entry())
    has_email = [
        r for r in finding.relationships
        if r.relation_type == RelationType.HAS_EMAIL
    ]
    assert len(has_email) == 2
    assert all(r.source_id == "domain:example.com" for r in has_email)


def test_parse_email_as_string():
    """WHOIS emails field can be a single string instead of a list."""
    adapter = WhoisAdapter()
    entry = _mock_whois_entry(emails="solo@example.com")
    finding = adapter._parse_results("example.com", entry)
    emails = [e for e in finding.entities if e.entity_type == EntityType.EMAIL]
    assert len(emails) == 1
    assert emails[0].label == "solo@example.com"


def test_parse_no_emails():
    adapter = WhoisAdapter()
    entry = _mock_whois_entry(emails=None)
    finding = adapter._parse_results("example.com", entry)
    emails = [e for e in finding.entities if e.entity_type == EntityType.EMAIL]
    assert len(emails) == 0


# --- Parsing: Edge cases ---


def test_parse_minimal_whois_data():
    """Domain with all fields None (maximum privacy)."""
    adapter = WhoisAdapter()
    entry = _mock_whois_entry(
        registrar=None,
        creation_date=None,
        expiration_date=None,
        updated_date=None,
        name_servers=None,
        registrant_name=None,
        registrant_org=None,
        registrant_country=None,
        registrant_state=None,
        registrant_city=None,
        emails=None,
        dnssec=None,
        status=None,
    )
    finding = adapter._parse_results("private.com", entry)
    assert len(finding.entities) == 1  # Just the domain
    assert finding.entities[0].id == "domain:private.com"
    assert len(finding.relationships) == 0


def test_parse_date_as_list():
    """WHOIS can return a list of dates for creation_date."""
    adapter = WhoisAdapter()
    entry = _mock_whois_entry(
        creation_date=[datetime(2019, 3, 1), datetime(2019, 3, 2)],
    )
    finding = adapter._parse_results("example.com", entry)
    domain = [e for e in finding.entities if e.id == "domain:example.com"][0]
    assert domain.properties["creation_date"] == "2019-03-01T00:00:00"


def test_parse_notes_summary():
    adapter = WhoisAdapter()
    finding = adapter._parse_results("example.com", _mock_whois_entry())
    assert "WHOIS for 'example.com'" in finding.notes
    assert "2 emails" in finding.notes
    assert "2 nameservers" in finding.notes
    assert "Example Registrar Inc." in finding.notes


# --- Fallback to .org / .name when registrant_ fields are None ---


def test_parse_falls_back_to_org_field():
    """When registrant_org is None, adapter should check .org attribute."""
    adapter = WhoisAdapter()
    entry = _mock_whois_entry(registrant_org=None, org="Fallback Corp")
    finding = adapter._parse_results("example.com", entry)
    orgs = [e for e in finding.entities if e.entity_type == EntityType.ORGANIZATION]
    assert len(orgs) == 1
    assert orgs[0].label == "Fallback Corp"


def test_parse_falls_back_to_name_field():
    """When registrant_name is None, adapter should check .name attribute."""
    adapter = WhoisAdapter()
    entry = _mock_whois_entry(registrant_name=None, name="John Smith")
    finding = adapter._parse_results("example.com", entry)
    persons = [e for e in finding.entities if e.entity_type == EntityType.PERSON]
    assert len(persons) == 1
    assert persons[0].label == "John Smith"


# --- is_available ---


def test_is_available_true_when_whois_importable():
    with patch.dict("sys.modules", {"whois": MagicMock()}):
        adapter = WhoisAdapter()
        assert adapter.is_available() is True


def test_is_available_false_when_whois_not_importable():
    with patch.dict("sys.modules", {"whois": None}):
        adapter = WhoisAdapter()
        # Forcing import failure
        with patch("builtins.__import__", side_effect=ImportError):
            assert adapter.is_available() is False


# --- Full async run with mocked whois.whois ---


@pytest.mark.asyncio
async def test_run_returns_finding():
    adapter = WhoisAdapter()
    mock_entry = _mock_whois_entry()
    mock_whois_mod = MagicMock()
    mock_whois_mod.whois.return_value = mock_entry
    with patch.dict(sys.modules, {"whois": mock_whois_mod}):
        finding = await adapter.run(domain="example.com")
    assert len(finding.entities) >= 1
    domain_entities = [
        e for e in finding.entities if e.id == "domain:example.com"
    ]
    assert len(domain_entities) == 1


@pytest.mark.asyncio
async def test_run_handles_exception():
    adapter = WhoisAdapter()
    mock_whois_mod = MagicMock()
    mock_whois_mod.whois.side_effect = Exception("Connection refused")
    with patch.dict(sys.modules, {"whois": mock_whois_mod}):
        finding = await adapter.run(domain="fail.com")
    assert "failed" in finding.notes.lower()
    assert "fail.com" in finding.notes
    assert len(finding.entities) == 0
