"""Tests for the IP WHOIS adapter — IP address to ASN/organization lookup."""

import sys
import pytest
from unittest.mock import MagicMock, patch

from osint_agent.tools.ip_whois import (
    IpWhoisAdapter,
    _extract_address,
    _extract_phone,
    _extract_email,
)
from osint_agent.models import EntityType, RelationType


@pytest.fixture
def adapter():
    return IpWhoisAdapter()


@pytest.fixture
def mock_rdap_result():
    """Canned IPWhois.lookup_rdap() output."""
    return {
        "asn": "15169",
        "asn_description": "GOOGLE",
        "asn_country_code": "US",
        "asn_cidr": "8.8.8.0/24",
        "network": {
            "name": "LVLT-GOGL-8-8-8",
            "start_address": "8.8.8.0",
            "end_address": "8.8.8.255",
        },
        "objects": {
            "GOGL": {
                "contact": {
                    "name": "Google LLC",
                    "kind": "org",
                    "address": [
                        {"value": "1600 Amphitheatre Parkway\nMountain View\nCA 94043"},
                    ],
                    "phone": [
                        {"value": "+1-650-253-0000"},
                    ],
                    "email": [
                        {"value": "network-abuse@google.com"},
                    ],
                },
                "roles": ["registrant"],
            },
            "ZG39-ARIN": {
                "contact": {
                    "name": "Google Net Eng",
                    "kind": "group",
                    "address": [],
                    "phone": [],
                    "email": [
                        {"value": "arin-contact@google.com"},
                    ],
                },
                "roles": ["administrative", "technical"],
            },
        },
    }


@pytest.fixture
def mock_rdap_minimal():
    """RDAP result with no objects (bare minimum)."""
    return {
        "asn": "13335",
        "asn_description": "CLOUDFLARENET",
        "asn_country_code": "US",
        "asn_cidr": "1.1.1.0/24",
        "network": {
            "name": "APNIC-LABS",
            "start_address": "1.1.1.0",
            "end_address": "1.1.1.255",
        },
        "objects": {},
    }


# ------------------------------------------------------------------
# Availability
# ------------------------------------------------------------------

def test_is_available_when_ipwhois_importable():
    with patch.dict(sys.modules, {"ipwhois": MagicMock()}):
        adapter = IpWhoisAdapter()
        assert adapter.is_available() is True


def test_is_available_false_when_ipwhois_missing():
    with patch.dict(sys.modules, {"ipwhois": None}):
        adapter = IpWhoisAdapter()
        with patch("builtins.__import__", side_effect=ImportError):
            assert adapter.is_available() is False


def test_adapter_name(adapter):
    assert adapter.name == "ip_whois"


# ------------------------------------------------------------------
# Helper functions
# ------------------------------------------------------------------

def test_extract_address_from_contact():
    contact = {
        "address": [
            {"value": "123 Main St\nSuite 400\nNew York"},
        ],
    }
    assert _extract_address(contact) == "123 Main St, Suite 400, New York"


def test_extract_address_empty_list():
    assert _extract_address({"address": []}) is None


def test_extract_address_none():
    assert _extract_address({"address": None}) is None


def test_extract_address_missing_key():
    assert _extract_address({}) is None


def test_extract_address_skips_empty_values():
    contact = {
        "address": [
            {"value": None},
            {"value": "456 Oak Ave"},
        ],
    }
    assert _extract_address(contact) == "456 Oak Ave"


def test_extract_phone_from_contact():
    contact = {"phone": [{"value": "+1-555-123-4567"}]}
    assert _extract_phone(contact) == "+1-555-123-4567"


def test_extract_phone_empty():
    assert _extract_phone({"phone": []}) is None


def test_extract_phone_none():
    assert _extract_phone({"phone": None}) is None


def test_extract_phone_missing_key():
    assert _extract_phone({}) is None


def test_extract_email_from_contact():
    contact = {"email": [{"value": "abuse@example.com"}]}
    assert _extract_email(contact) == "abuse@example.com"


def test_extract_email_empty():
    assert _extract_email({"email": []}) is None


def test_extract_email_none():
    assert _extract_email({"email": None}) is None


def test_extract_email_missing_key():
    assert _extract_email({}) is None


def test_extract_email_skips_empty_values():
    contact = {
        "email": [
            {"value": None},
            {"value": "real@example.com"},
        ],
    }
    assert _extract_email(contact) == "real@example.com"


# ------------------------------------------------------------------
# Happy path: full RDAP result
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_creates_ip_entity(mock_rdap_result):
    adapter = IpWhoisAdapter()
    mock_ipwhois_mod = MagicMock()
    mock_ipwhois_instance = MagicMock()
    mock_ipwhois_instance.lookup_rdap.return_value = mock_rdap_result
    mock_ipwhois_mod.IPWhois.return_value = mock_ipwhois_instance
    mock_ipwhois_mod.exceptions = MagicMock()

    with patch.dict(sys.modules, {
        "ipwhois": mock_ipwhois_mod,
        "ipwhois.exceptions": mock_ipwhois_mod.exceptions,
    }):
        finding = await adapter.run(ip="8.8.8.8")

    ip_ent = next(e for e in finding.entities if e.id == "domain:ip:8.8.8.8")
    assert ip_ent.entity_type == EntityType.DOMAIN
    assert ip_ent.label == "8.8.8.8"


@pytest.mark.asyncio
async def test_run_ip_entity_properties(mock_rdap_result):
    adapter = IpWhoisAdapter()
    mock_ipwhois_mod = MagicMock()
    mock_ipwhois_instance = MagicMock()
    mock_ipwhois_instance.lookup_rdap.return_value = mock_rdap_result
    mock_ipwhois_mod.IPWhois.return_value = mock_ipwhois_instance
    mock_ipwhois_mod.exceptions = MagicMock()

    with patch.dict(sys.modules, {
        "ipwhois": mock_ipwhois_mod,
        "ipwhois.exceptions": mock_ipwhois_mod.exceptions,
    }):
        finding = await adapter.run(ip="8.8.8.8")

    ip_ent = next(e for e in finding.entities if e.id == "domain:ip:8.8.8.8")
    assert ip_ent.properties["ip_address"] == "8.8.8.8"
    assert ip_ent.properties["asn"] == "15169"
    assert ip_ent.properties["asn_description"] == "GOOGLE"
    assert ip_ent.properties["asn_country"] == "US"
    assert ip_ent.properties["network_cidr"] == "8.8.8.0/24"
    assert ip_ent.properties["network_name"] == "LVLT-GOGL-8-8-8"
    assert "8.8.8.0" in ip_ent.properties["network_range"]
    assert "8.8.8.255" in ip_ent.properties["network_range"]


@pytest.mark.asyncio
async def test_run_creates_organization_entities(mock_rdap_result):
    adapter = IpWhoisAdapter()
    mock_ipwhois_mod = MagicMock()
    mock_ipwhois_instance = MagicMock()
    mock_ipwhois_instance.lookup_rdap.return_value = mock_rdap_result
    mock_ipwhois_mod.IPWhois.return_value = mock_ipwhois_instance
    mock_ipwhois_mod.exceptions = MagicMock()

    with patch.dict(sys.modules, {
        "ipwhois": mock_ipwhois_mod,
        "ipwhois.exceptions": mock_ipwhois_mod.exceptions,
    }):
        finding = await adapter.run(ip="8.8.8.8")

    orgs = [e for e in finding.entities if e.entity_type == EntityType.ORGANIZATION]
    assert len(orgs) == 2

    gogl = next(e for e in orgs if e.id == "organization:gogl")
    assert gogl.label == "Google LLC"
    assert gogl.properties["handle"] == "GOGL"
    assert gogl.properties["kind"] == "org"
    assert "1600 Amphitheatre" in gogl.properties["address"]
    assert gogl.properties["phone"] == "+1-650-253-0000"
    assert gogl.properties["email"] == "network-abuse@google.com"
    assert "registrant" in gogl.properties["role"]

    zg39 = next(e for e in orgs if e.id == "organization:zg39-arin")
    assert zg39.label == "Google Net Eng"
    assert zg39.properties["kind"] == "group"
    assert zg39.properties["email"] == "arin-contact@google.com"
    assert "administrative" in zg39.properties["role"]


# ------------------------------------------------------------------
# Relationships
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_creates_owns_relationships(mock_rdap_result):
    adapter = IpWhoisAdapter()
    mock_ipwhois_mod = MagicMock()
    mock_ipwhois_instance = MagicMock()
    mock_ipwhois_instance.lookup_rdap.return_value = mock_rdap_result
    mock_ipwhois_mod.IPWhois.return_value = mock_ipwhois_instance
    mock_ipwhois_mod.exceptions = MagicMock()

    with patch.dict(sys.modules, {
        "ipwhois": mock_ipwhois_mod,
        "ipwhois.exceptions": mock_ipwhois_mod.exceptions,
    }):
        finding = await adapter.run(ip="8.8.8.8")

    owns = [r for r in finding.relationships if r.relation_type == RelationType.OWNS]
    assert len(owns) == 2
    assert all(r.target_id == "domain:ip:8.8.8.8" for r in owns)

    source_ids = {r.source_id for r in owns}
    assert "organization:gogl" in source_ids
    assert "organization:zg39-arin" in source_ids


@pytest.mark.asyncio
async def test_run_relationship_roles_populated(mock_rdap_result):
    adapter = IpWhoisAdapter()
    mock_ipwhois_mod = MagicMock()
    mock_ipwhois_instance = MagicMock()
    mock_ipwhois_instance.lookup_rdap.return_value = mock_rdap_result
    mock_ipwhois_mod.IPWhois.return_value = mock_ipwhois_instance
    mock_ipwhois_mod.exceptions = MagicMock()

    with patch.dict(sys.modules, {
        "ipwhois": mock_ipwhois_mod,
        "ipwhois.exceptions": mock_ipwhois_mod.exceptions,
    }):
        finding = await adapter.run(ip="8.8.8.8")

    gogl_rel = next(
        r for r in finding.relationships
        if r.source_id == "organization:gogl"
    )
    assert gogl_rel.properties["roles"] == ["registrant"]


# ------------------------------------------------------------------
# Minimal result (no objects)
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_minimal_result_no_orgs(mock_rdap_minimal):
    adapter = IpWhoisAdapter()
    mock_ipwhois_mod = MagicMock()
    mock_ipwhois_instance = MagicMock()
    mock_ipwhois_instance.lookup_rdap.return_value = mock_rdap_minimal
    mock_ipwhois_mod.IPWhois.return_value = mock_ipwhois_instance
    mock_ipwhois_mod.exceptions = MagicMock()

    with patch.dict(sys.modules, {
        "ipwhois": mock_ipwhois_mod,
        "ipwhois.exceptions": mock_ipwhois_mod.exceptions,
    }):
        finding = await adapter.run(ip="1.1.1.1")

    assert len(finding.entities) == 1  # IP entity only
    assert finding.entities[0].id == "domain:ip:1.1.1.1"
    assert len(finding.relationships) == 0


# ------------------------------------------------------------------
# Objects with no contact name (should be skipped)
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_skips_objects_without_contact_name():
    result = {
        "asn": "12345",
        "asn_description": "TEST-ASN",
        "asn_country_code": "US",
        "asn_cidr": "10.0.0.0/8",
        "network": {"name": "TEST-NET", "start_address": "10.0.0.0", "end_address": "10.255.255.255"},
        "objects": {
            "EMPTY-HANDLE": {
                "contact": {
                    "name": None,
                    "kind": "org",
                    "address": [],
                    "phone": [],
                    "email": [],
                },
                "roles": ["registrant"],
            },
            "NO-CONTACT": {
                "contact": {},
                "roles": ["abuse"],
            },
        },
    }
    adapter = IpWhoisAdapter()
    mock_ipwhois_mod = MagicMock()
    mock_ipwhois_instance = MagicMock()
    mock_ipwhois_instance.lookup_rdap.return_value = result
    mock_ipwhois_mod.IPWhois.return_value = mock_ipwhois_instance
    mock_ipwhois_mod.exceptions = MagicMock()

    with patch.dict(sys.modules, {
        "ipwhois": mock_ipwhois_mod,
        "ipwhois.exceptions": mock_ipwhois_mod.exceptions,
    }):
        finding = await adapter.run(ip="10.0.0.1")

    orgs = [e for e in finding.entities if e.entity_type == EntityType.ORGANIZATION]
    assert len(orgs) == 0
    assert len(finding.relationships) == 0


# ------------------------------------------------------------------
# Null network and objects
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_handles_none_network_and_objects():
    result = {
        "asn": "99999",
        "asn_description": "UNKNOWN",
        "asn_country_code": "",
        "asn_cidr": None,
        "network": None,
        "objects": None,
    }
    adapter = IpWhoisAdapter()
    mock_ipwhois_mod = MagicMock()
    mock_ipwhois_instance = MagicMock()
    mock_ipwhois_instance.lookup_rdap.return_value = result
    mock_ipwhois_mod.IPWhois.return_value = mock_ipwhois_instance
    mock_ipwhois_mod.exceptions = MagicMock()

    with patch.dict(sys.modules, {
        "ipwhois": mock_ipwhois_mod,
        "ipwhois.exceptions": mock_ipwhois_mod.exceptions,
    }):
        finding = await adapter.run(ip="192.0.2.1")

    assert len(finding.entities) == 1
    ip_ent = finding.entities[0]
    assert ip_ent.properties["asn"] == "99999"
    # network_name should be excluded (None network)
    assert "network_name" not in ip_ent.properties


# ------------------------------------------------------------------
# Notes
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_notes_contain_asn_info(mock_rdap_result):
    adapter = IpWhoisAdapter()
    mock_ipwhois_mod = MagicMock()
    mock_ipwhois_instance = MagicMock()
    mock_ipwhois_instance.lookup_rdap.return_value = mock_rdap_result
    mock_ipwhois_mod.IPWhois.return_value = mock_ipwhois_instance
    mock_ipwhois_mod.exceptions = MagicMock()

    with patch.dict(sys.modules, {
        "ipwhois": mock_ipwhois_mod,
        "ipwhois.exceptions": mock_ipwhois_mod.exceptions,
    }):
        finding = await adapter.run(ip="8.8.8.8")

    assert "IP 8.8.8.8" in finding.notes
    assert "GOOGLE" in finding.notes
    assert "AS15169" in finding.notes


@pytest.mark.asyncio
async def test_run_notes_without_asn_description():
    result = {
        "asn": "12345",
        "asn_description": "",
        "asn_country_code": "US",
        "asn_cidr": "10.0.0.0/8",
        "network": {"name": "TEST", "start_address": "10.0.0.0", "end_address": "10.255.255.255"},
        "objects": {},
    }
    adapter = IpWhoisAdapter()
    mock_ipwhois_mod = MagicMock()
    mock_ipwhois_instance = MagicMock()
    mock_ipwhois_instance.lookup_rdap.return_value = result
    mock_ipwhois_mod.IPWhois.return_value = mock_ipwhois_instance
    mock_ipwhois_mod.exceptions = MagicMock()

    with patch.dict(sys.modules, {
        "ipwhois": mock_ipwhois_mod,
        "ipwhois.exceptions": mock_ipwhois_mod.exceptions,
    }):
        finding = await adapter.run(ip="10.0.0.1")

    assert "IP 10.0.0.1" in finding.notes
    assert "AS12345" in finding.notes
    # Should not have empty " | " prefix for empty description
    assert " |  |" not in finding.notes


# ------------------------------------------------------------------
# Sources
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_source_is_ip_whois(mock_rdap_result):
    adapter = IpWhoisAdapter()
    mock_ipwhois_mod = MagicMock()
    mock_ipwhois_instance = MagicMock()
    mock_ipwhois_instance.lookup_rdap.return_value = mock_rdap_result
    mock_ipwhois_mod.IPWhois.return_value = mock_ipwhois_instance
    mock_ipwhois_mod.exceptions = MagicMock()

    with patch.dict(sys.modules, {
        "ipwhois": mock_ipwhois_mod,
        "ipwhois.exceptions": mock_ipwhois_mod.exceptions,
    }):
        finding = await adapter.run(ip="8.8.8.8")

    for entity in finding.entities:
        assert entity.sources[0].tool == "ip_whois"
    for rel in finding.relationships:
        assert rel.sources[0].tool == "ip_whois"


# ------------------------------------------------------------------
# Empty / no WHOIS data
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_empty_result():
    adapter = IpWhoisAdapter()
    mock_ipwhois_mod = MagicMock()
    mock_ipwhois_instance = MagicMock()
    mock_ipwhois_instance.lookup_rdap.return_value = {}
    mock_ipwhois_mod.IPWhois.return_value = mock_ipwhois_instance
    mock_ipwhois_mod.exceptions = MagicMock()

    with patch.dict(sys.modules, {
        "ipwhois": mock_ipwhois_mod,
        "ipwhois.exceptions": mock_ipwhois_mod.exceptions,
    }):
        finding = await adapter.run(ip="0.0.0.0")

    assert len(finding.entities) == 0
    assert "no whois data" in finding.notes.lower()


@pytest.mark.asyncio
async def test_run_none_result():
    adapter = IpWhoisAdapter()
    mock_ipwhois_mod = MagicMock()
    mock_ipwhois_instance = MagicMock()
    mock_ipwhois_instance.lookup_rdap.return_value = None
    mock_ipwhois_mod.IPWhois.return_value = mock_ipwhois_instance
    mock_ipwhois_mod.exceptions = MagicMock()

    with patch.dict(sys.modules, {
        "ipwhois": mock_ipwhois_mod,
        "ipwhois.exceptions": mock_ipwhois_mod.exceptions,
    }):
        finding = await adapter.run(ip="0.0.0.0")

    assert len(finding.entities) == 0
    assert "no whois data" in finding.notes.lower()


# ------------------------------------------------------------------
# Error handling
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_handles_ip_defined_error():
    """Private/reserved IPs should return a Finding with notes."""
    adapter = IpWhoisAdapter()

    IPDefinedError = type("IPDefinedError", (Exception,), {})
    ASNRegistryError = type("ASNRegistryError", (Exception,), {})
    WhoisLookupError = type("WhoisLookupError", (Exception,), {})

    mock_ipwhois_mod = MagicMock()
    mock_ipwhois_instance = MagicMock()
    mock_ipwhois_instance.lookup_rdap.side_effect = IPDefinedError(
        "192.168.1.1 is a private IP"
    )
    mock_ipwhois_mod.IPWhois.return_value = mock_ipwhois_instance

    mock_exceptions = MagicMock()
    mock_exceptions.IPDefinedError = IPDefinedError
    mock_exceptions.ASNRegistryError = ASNRegistryError
    mock_exceptions.WhoisLookupError = WhoisLookupError
    mock_ipwhois_mod.exceptions = mock_exceptions

    with patch.dict(sys.modules, {
        "ipwhois": mock_ipwhois_mod,
        "ipwhois.exceptions": mock_exceptions,
    }):
        finding = await adapter.run(ip="192.168.1.1")

    assert len(finding.entities) == 0
    assert "error" in finding.notes.lower()
    assert "192.168.1.1" in finding.notes


@pytest.mark.asyncio
async def test_run_handles_generic_exception():
    adapter = IpWhoisAdapter()

    IPDefinedError = type("IPDefinedError", (Exception,), {})
    ASNRegistryError = type("ASNRegistryError", (Exception,), {})
    WhoisLookupError = type("WhoisLookupError", (Exception,), {})

    mock_ipwhois_mod = MagicMock()
    mock_ipwhois_instance = MagicMock()
    mock_ipwhois_instance.lookup_rdap.side_effect = ConnectionError("Network down")
    mock_ipwhois_mod.IPWhois.return_value = mock_ipwhois_instance

    mock_exceptions = MagicMock()
    mock_exceptions.IPDefinedError = IPDefinedError
    mock_exceptions.ASNRegistryError = ASNRegistryError
    mock_exceptions.WhoisLookupError = WhoisLookupError
    mock_ipwhois_mod.exceptions = mock_exceptions

    with patch.dict(sys.modules, {
        "ipwhois": mock_ipwhois_mod,
        "ipwhois.exceptions": mock_exceptions,
    }):
        finding = await adapter.run(ip="8.8.4.4")

    assert len(finding.entities) == 0
    assert "error" in finding.notes.lower()


@pytest.mark.asyncio
async def test_run_handles_asn_registry_error():
    adapter = IpWhoisAdapter()

    IPDefinedError = type("IPDefinedError", (Exception,), {})
    ASNRegistryError = type("ASNRegistryError", (Exception,), {})
    WhoisLookupError = type("WhoisLookupError", (Exception,), {})

    mock_ipwhois_mod = MagicMock()
    mock_ipwhois_instance = MagicMock()
    mock_ipwhois_instance.lookup_rdap.side_effect = ASNRegistryError("ASN not found")
    mock_ipwhois_mod.IPWhois.return_value = mock_ipwhois_instance

    mock_exceptions = MagicMock()
    mock_exceptions.IPDefinedError = IPDefinedError
    mock_exceptions.ASNRegistryError = ASNRegistryError
    mock_exceptions.WhoisLookupError = WhoisLookupError
    mock_ipwhois_mod.exceptions = mock_exceptions

    with patch.dict(sys.modules, {
        "ipwhois": mock_ipwhois_mod,
        "ipwhois.exceptions": mock_exceptions,
    }):
        finding = await adapter.run(ip="203.0.113.1")

    assert len(finding.entities) == 0
    assert "error" in finding.notes.lower()


# ------------------------------------------------------------------
# Falsy property filtering
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_filters_falsy_ip_properties():
    """IP entity properties dict should exclude falsy values."""
    result = {
        "asn": "",
        "asn_description": "",
        "asn_country_code": "",
        "asn_cidr": None,
        "network": {"name": None, "start_address": None, "end_address": None},
        "objects": {},
    }
    adapter = IpWhoisAdapter()
    mock_ipwhois_mod = MagicMock()
    mock_ipwhois_instance = MagicMock()
    mock_ipwhois_instance.lookup_rdap.return_value = result
    mock_ipwhois_mod.IPWhois.return_value = mock_ipwhois_instance
    mock_ipwhois_mod.exceptions = MagicMock()

    with patch.dict(sys.modules, {
        "ipwhois": mock_ipwhois_mod,
        "ipwhois.exceptions": mock_ipwhois_mod.exceptions,
    }):
        finding = await adapter.run(ip="10.0.0.1")

    ip_ent = finding.entities[0]
    # ip_address is "10.0.0.1" which is truthy — should be present
    assert ip_ent.properties["ip_address"] == "10.0.0.1"
    # Empty strings and None values should be filtered out
    assert "asn" not in ip_ent.properties
    assert "asn_description" not in ip_ent.properties
    assert "network_cidr" not in ip_ent.properties
    assert "network_name" not in ip_ent.properties


# ------------------------------------------------------------------
# Organization property filtering
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_org_filters_falsy_properties():
    """Organization entity should exclude empty/None contact fields."""
    result = {
        "asn": "12345",
        "asn_description": "TEST",
        "asn_country_code": "US",
        "asn_cidr": "10.0.0.0/8",
        "network": {"name": "TEST", "start_address": "10.0.0.0", "end_address": "10.255.255.255"},
        "objects": {
            "TEST-HANDLE": {
                "contact": {
                    "name": "Test Org",
                    "kind": None,
                    "address": [],
                    "phone": [],
                    "email": [],
                },
                "roles": [],
            },
        },
    }
    adapter = IpWhoisAdapter()
    mock_ipwhois_mod = MagicMock()
    mock_ipwhois_instance = MagicMock()
    mock_ipwhois_instance.lookup_rdap.return_value = result
    mock_ipwhois_mod.IPWhois.return_value = mock_ipwhois_instance
    mock_ipwhois_mod.exceptions = MagicMock()

    with patch.dict(sys.modules, {
        "ipwhois": mock_ipwhois_mod,
        "ipwhois.exceptions": mock_ipwhois_mod.exceptions,
    }):
        finding = await adapter.run(ip="10.0.0.1")

    org = next(e for e in finding.entities if e.entity_type == EntityType.ORGANIZATION)
    assert org.label == "Test Org"
    assert org.properties["handle"] == "TEST-HANDLE"
    # None/empty fields should be excluded
    assert "kind" not in org.properties
    assert "address" not in org.properties
    assert "phone" not in org.properties
    assert "email" not in org.properties
