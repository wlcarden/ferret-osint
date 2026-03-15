"""Tests for the DNS enumeration adapter — raw DNS record lookups."""

import sys
import pytest
from unittest.mock import MagicMock, patch

from osint_agent.tools.dns_enum import DnsEnumAdapter
from osint_agent.models import EntityType, RelationType


@pytest.fixture
def adapter():
    return DnsEnumAdapter()


def _setup_dns_mocks(records: dict):
    """Build sys.modules patches for dns.resolver and dns.exception.

    The adapter does `import dns.resolver` inside run(), which binds the
    local name `dns` to sys.modules["dns"]. The lambda then accesses
    `dns.resolver.resolve` via attribute lookup. So sys.modules["dns"]
    must have a `.resolver` attribute pointing to the mock resolver,
    and that mock resolver must also be at sys.modules["dns.resolver"].
    """
    NoAnswer = type("NoAnswer", (Exception,), {})
    NXDOMAIN = type("NXDOMAIN", (Exception,), {})
    NoNameservers = type("NoNameservers", (Exception,), {})
    Timeout = type("Timeout", (Exception,), {})

    mock_resolver = MagicMock()
    mock_resolver.NoAnswer = NoAnswer
    mock_resolver.NXDOMAIN = NXDOMAIN
    mock_resolver.NoNameservers = NoNameservers

    mock_exception = MagicMock()
    mock_exception.Timeout = Timeout

    def _resolve(domain, rtype):
        if rtype not in records:
            raise NoAnswer()
        rdata_list = []
        for val in records[rtype]:
            rdata = MagicMock()
            rdata.to_text.return_value = val
            rdata_list.append(rdata)
        return rdata_list

    mock_resolver.resolve = _resolve

    # The dns top-level module mock must expose .resolver and .exception
    # as attributes so that `import dns.resolver` and `dns.resolver.resolve`
    # both resolve correctly.
    mock_dns = MagicMock()
    mock_dns.resolver = mock_resolver
    mock_dns.exception = mock_exception

    return {
        "dns": mock_dns,
        "dns.resolver": mock_resolver,
        "dns.exception": mock_exception,
    }


# ------------------------------------------------------------------
# Availability
# ------------------------------------------------------------------

def test_is_available_when_dns_importable():
    mock_dns = MagicMock()
    mock_dns.resolver = MagicMock()
    with patch.dict(sys.modules, {"dns.resolver": mock_dns.resolver, "dns": mock_dns}):
        adapter = DnsEnumAdapter()
        assert adapter.is_available() is True


def test_is_available_false_when_dns_missing():
    with patch.dict(sys.modules, {"dns.resolver": None, "dns": None}):
        adapter = DnsEnumAdapter()
        with patch("builtins.__import__", side_effect=ImportError):
            assert adapter.is_available() is False


def test_adapter_name(adapter):
    assert adapter.name == "dns_enum"


# ------------------------------------------------------------------
# Happy path: full DNS record set
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_creates_base_domain_entity():
    records = {
        "A": ["93.184.216.34"],
        "MX": ["10 mail.example.com."],
        "NS": ["ns1.example.com.", "ns2.example.com."],
        "TXT": ['"v=spf1 include:_spf.google.com ~all"'],
    }
    mocks = _setup_dns_mocks(records)
    adapter = DnsEnumAdapter()

    with patch.dict(sys.modules, mocks):
        finding = await adapter.run(domain="example.com")

    base = [e for e in finding.entities if e.id == "domain:example.com"]
    assert len(base) == 1
    assert base[0].entity_type == EntityType.DOMAIN
    assert base[0].label == "example.com"


@pytest.mark.asyncio
async def test_run_base_domain_has_dns_properties():
    records = {
        "A": ["93.184.216.34"],
        "AAAA": ["2606:2800:220:1:248:1893:25c8:1946"],
        "TXT": ['"v=spf1 include:_spf.google.com ~all"'],
    }
    mocks = _setup_dns_mocks(records)
    adapter = DnsEnumAdapter()

    with patch.dict(sys.modules, mocks):
        finding = await adapter.run(domain="example.com")

    base = finding.entities[0]
    assert base.properties["dns_a"] == ["93.184.216.34"]
    assert base.properties["dns_aaaa"] == ["2606:2800:220:1:248:1893:25c8:1946"]
    assert "dns_txt" in base.properties


@pytest.mark.asyncio
async def test_run_extracts_spf_policy():
    records = {
        "TXT": ['"v=spf1 include:_spf.google.com ~all"', '"google-verification=abc123"'],
    }
    mocks = _setup_dns_mocks(records)
    adapter = DnsEnumAdapter()

    with patch.dict(sys.modules, mocks):
        finding = await adapter.run(domain="example.com")

    base = finding.entities[0]
    assert "spf_policy" in base.properties
    assert "v=spf1" in base.properties["spf_policy"]


# ------------------------------------------------------------------
# MX record entities and relationships
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_creates_mx_entities():
    records = {
        "MX": ["10 mail.example.com.", "20 backup.example.com."],
    }
    mocks = _setup_dns_mocks(records)
    adapter = DnsEnumAdapter()

    with patch.dict(sys.modules, mocks):
        finding = await adapter.run(domain="example.com")

    mx_entities = [
        e for e in finding.entities
        if e.properties.get("role") == "mail_server"
    ]
    assert len(mx_entities) == 2
    labels = {e.label for e in mx_entities}
    # Trailing dots should be stripped by rstrip(".")
    assert "mail.example.com" in labels
    assert "backup.example.com" in labels


@pytest.mark.asyncio
async def test_run_mx_entities_have_priority():
    records = {
        "MX": ["10 mail.example.com."],
    }
    mocks = _setup_dns_mocks(records)
    adapter = DnsEnumAdapter()

    with patch.dict(sys.modules, mocks):
        finding = await adapter.run(domain="example.com")

    mx = next(
        e for e in finding.entities
        if e.properties.get("role") == "mail_server"
    )
    assert mx.properties["mx_priority"] == "10"


@pytest.mark.asyncio
async def test_run_mx_relationships():
    records = {
        "MX": ["10 mail.example.com."],
    }
    mocks = _setup_dns_mocks(records)
    adapter = DnsEnumAdapter()

    with patch.dict(sys.modules, mocks):
        finding = await adapter.run(domain="example.com")

    mx_rels = [
        r for r in finding.relationships
        if r.properties.get("via") == "MX_record"
    ]
    assert len(mx_rels) == 1
    assert mx_rels[0].source_id == "domain:example.com"
    assert mx_rels[0].target_id == "domain:mail.example.com"
    assert mx_rels[0].relation_type == RelationType.CONNECTED_TO


# ------------------------------------------------------------------
# NS record entities and relationships
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_creates_ns_entities():
    records = {
        "NS": ["ns1.example.com.", "ns2.example.com."],
    }
    mocks = _setup_dns_mocks(records)
    adapter = DnsEnumAdapter()

    with patch.dict(sys.modules, mocks):
        finding = await adapter.run(domain="example.com")

    ns_entities = [
        e for e in finding.entities
        if e.properties.get("role") == "nameserver"
    ]
    assert len(ns_entities) == 2
    labels = {e.label for e in ns_entities}
    assert "ns1.example.com" in labels
    assert "ns2.example.com" in labels


@pytest.mark.asyncio
async def test_run_ns_relationships():
    records = {
        "NS": ["ns1.example.com."],
    }
    mocks = _setup_dns_mocks(records)
    adapter = DnsEnumAdapter()

    with patch.dict(sys.modules, mocks):
        finding = await adapter.run(domain="example.com")

    ns_rels = [
        r for r in finding.relationships
        if r.properties.get("via") == "NS_record"
    ]
    assert len(ns_rels) == 1
    assert ns_rels[0].source_id == "domain:example.com"
    assert ns_rels[0].target_id == "domain:ns1.example.com"
    assert ns_rels[0].relation_type == RelationType.CONNECTED_TO


# ------------------------------------------------------------------
# Deduplication between MX and NS
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_deduplicates_mx_and_ns():
    """If the same host appears in both MX and NS, only one entity is created."""
    records = {
        "MX": ["10 shared.example.com."],
        "NS": ["shared.example.com."],
    }
    mocks = _setup_dns_mocks(records)
    adapter = DnsEnumAdapter()

    with patch.dict(sys.modules, mocks):
        finding = await adapter.run(domain="example.com")

    shared = [e for e in finding.entities if e.label == "shared.example.com"]
    # Should appear once (MX wins because it's processed first)
    assert len(shared) == 1
    assert shared[0].properties["role"] == "mail_server"


# ------------------------------------------------------------------
# Notes
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_notes_contain_record_counts():
    records = {
        "A": ["1.2.3.4"],
        "MX": ["10 mail.example.com."],
        "NS": ["ns1.example.com."],
    }
    mocks = _setup_dns_mocks(records)
    adapter = DnsEnumAdapter()

    with patch.dict(sys.modules, mocks):
        finding = await adapter.run(domain="example.com")

    assert "DNS for example.com" in finding.notes
    assert "A: 1" in finding.notes
    assert "MX: 1" in finding.notes
    assert "NS: 1" in finding.notes


# ------------------------------------------------------------------
# Sources
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_source_is_dns_enum():
    records = {
        "A": ["1.2.3.4"],
    }
    mocks = _setup_dns_mocks(records)
    adapter = DnsEnumAdapter()

    with patch.dict(sys.modules, mocks):
        finding = await adapter.run(domain="example.com")

    for entity in finding.entities:
        assert entity.sources[0].tool == "dns_enum"


# ------------------------------------------------------------------
# No records found
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_all_queries_fail_returns_base_entity():
    """When all DNS queries raise NoAnswer, base entity is still created."""
    records = {}  # all record types will raise NoAnswer
    mocks = _setup_dns_mocks(records)
    adapter = DnsEnumAdapter()

    with patch.dict(sys.modules, mocks):
        finding = await adapter.run(domain="empty.com")

    assert len(finding.entities) == 1
    assert finding.entities[0].id == "domain:empty.com"
    assert len(finding.relationships) == 0
    assert "DNS for empty.com" in finding.notes


# ------------------------------------------------------------------
# Error handling: individual record type failures
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_continues_on_partial_failure():
    """If one record type fails with an unexpected error, others still work."""
    NoAnswer = type("NoAnswer", (Exception,), {})
    NXDOMAIN = type("NXDOMAIN", (Exception,), {})
    NoNameservers = type("NoNameservers", (Exception,), {})
    Timeout = type("Timeout", (Exception,), {})

    mock_resolver = MagicMock()
    mock_resolver.NoAnswer = NoAnswer
    mock_resolver.NXDOMAIN = NXDOMAIN
    mock_resolver.NoNameservers = NoNameservers

    mock_exception = MagicMock()
    mock_exception.Timeout = Timeout

    def _resolve(domain, rtype):
        if rtype == "A":
            rdata = MagicMock()
            rdata.to_text.return_value = "1.2.3.4"
            return [rdata]
        if rtype == "MX":
            raise RuntimeError("Unexpected DNS failure")
        raise NoAnswer()

    mock_resolver.resolve = _resolve

    mock_dns = MagicMock()
    mock_dns.resolver = mock_resolver
    mock_dns.exception = mock_exception

    mocks = {
        "dns": mock_dns,
        "dns.resolver": mock_resolver,
        "dns.exception": mock_exception,
    }
    adapter = DnsEnumAdapter()

    with patch.dict(sys.modules, mocks):
        finding = await adapter.run(domain="partial.com")

    base = finding.entities[0]
    assert "dns_a" in base.properties
    assert "dns_mx" not in base.properties


# ------------------------------------------------------------------
# NXDOMAIN handling
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_handles_nxdomain():
    """NXDOMAIN for all queries should not raise, just return base entity."""
    NoAnswer = type("NoAnswer", (Exception,), {})
    NXDOMAIN = type("NXDOMAIN", (Exception,), {})
    NoNameservers = type("NoNameservers", (Exception,), {})
    Timeout = type("Timeout", (Exception,), {})

    mock_resolver = MagicMock()
    mock_resolver.NoAnswer = NoAnswer
    mock_resolver.NXDOMAIN = NXDOMAIN
    mock_resolver.NoNameservers = NoNameservers

    mock_exception = MagicMock()
    mock_exception.Timeout = Timeout

    def _resolve(domain, rtype):
        raise NXDOMAIN()

    mock_resolver.resolve = _resolve

    mock_dns = MagicMock()
    mock_dns.resolver = mock_resolver
    mock_dns.exception = mock_exception

    mocks = {
        "dns": mock_dns,
        "dns.resolver": mock_resolver,
        "dns.exception": mock_exception,
    }
    adapter = DnsEnumAdapter()

    with patch.dict(sys.modules, mocks):
        finding = await adapter.run(domain="nonexistent.invalid")

    assert len(finding.entities) == 1
    assert finding.entities[0].id == "domain:nonexistent.invalid"
    assert len(finding.relationships) == 0


# ------------------------------------------------------------------
# Mixed record types with relationships
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_full_record_set_entities_and_relationships():
    """Full integration: A + MX + NS should produce correct entity/rel counts."""
    records = {
        "A": ["93.184.216.34", "93.184.216.35"],
        "MX": ["10 mx1.example.com.", "20 mx2.example.com."],
        "NS": ["ns1.example.com.", "ns2.example.com."],
        "TXT": ['"v=spf1 -all"'],
    }
    mocks = _setup_dns_mocks(records)
    adapter = DnsEnumAdapter()

    with patch.dict(sys.modules, mocks):
        finding = await adapter.run(domain="example.com")

    # base + 2 MX + 2 NS = 5 entities
    assert len(finding.entities) == 5
    # 2 MX rels + 2 NS rels = 4 relationships
    assert len(finding.relationships) == 4

    mx_rels = [r for r in finding.relationships if r.properties.get("via") == "MX_record"]
    ns_rels = [r for r in finding.relationships if r.properties.get("via") == "NS_record"]
    assert len(mx_rels) == 2
    assert len(ns_rels) == 2
