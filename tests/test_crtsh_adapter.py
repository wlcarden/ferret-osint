"""Tests for the crt.sh adapter — Certificate Transparency subdomain discovery."""

import sys
import pytest
from unittest.mock import MagicMock, patch

from osint_agent.tools.crtsh import CrtshAdapter
from osint_agent.models import EntityType, RelationType


@pytest.fixture
def adapter():
    return CrtshAdapter()


@pytest.fixture
def mock_crtsh_results():
    """Canned pycrtsh search() output with varied certificate entries."""
    return [
        {
            "name": "www.example.com",
            "issuer": "Let's Encrypt Authority X3",
            "not_before": "2024-01-01",
            "not_after": "2024-04-01",
        },
        {
            "name": "*.example.com",
            "issuer": "Let's Encrypt Authority X3",
            "not_before": "2024-02-01",
            "not_after": "2024-05-01",
        },
        {
            "name": "mail.example.com",
            "issuer": "DigiCert SHA2",
            "not_before": "2023-06-15",
            "not_after": "2024-06-15",
        },
        {
            "name": "api.example.com",
            "issuer": "Sectigo RSA",
            "not_before": None,
            "not_after": None,
        },
    ]


# ------------------------------------------------------------------
# Availability
# ------------------------------------------------------------------

def test_is_available_when_pycrtsh_importable():
    with patch.dict(sys.modules, {"pycrtsh": MagicMock()}):
        adapter = CrtshAdapter()
        assert adapter.is_available() is True


def test_is_available_false_when_pycrtsh_missing():
    with patch.dict(sys.modules, {"pycrtsh": None}):
        adapter = CrtshAdapter()
        with patch("builtins.__import__", side_effect=ImportError):
            assert adapter.is_available() is False


def test_adapter_name(adapter):
    assert adapter.name == "crtsh"


# ------------------------------------------------------------------
# Happy path: subdomains discovered
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_creates_base_domain_entity(mock_crtsh_results):
    adapter = CrtshAdapter()
    mock_pycrtsh = MagicMock()
    mock_crtsh_instance = MagicMock()
    mock_crtsh_instance.search.return_value = mock_crtsh_results
    mock_pycrtsh.Crtsh.return_value = mock_crtsh_instance

    with patch.dict(sys.modules, {"pycrtsh": mock_pycrtsh}):
        finding = await adapter.run(domain="example.com")

    base = [e for e in finding.entities if e.id == "domain:example.com"]
    assert len(base) == 1
    assert base[0].entity_type == EntityType.DOMAIN
    assert base[0].label == "example.com"


@pytest.mark.asyncio
async def test_run_creates_subdomain_entities(mock_crtsh_results):
    adapter = CrtshAdapter()
    mock_pycrtsh = MagicMock()
    mock_crtsh_instance = MagicMock()
    mock_crtsh_instance.search.return_value = mock_crtsh_results
    mock_pycrtsh.Crtsh.return_value = mock_crtsh_instance

    with patch.dict(sys.modules, {"pycrtsh": mock_pycrtsh}):
        finding = await adapter.run(domain="example.com")

    entity_ids = {e.id for e in finding.entities}
    assert "domain:www.example.com" in entity_ids
    assert "domain:mail.example.com" in entity_ids
    assert "domain:api.example.com" in entity_ids


@pytest.mark.asyncio
async def test_run_strips_wildcard_prefix(mock_crtsh_results):
    """Wildcard entry '*.example.com' should become 'example.com', which
    is already the base domain, so it should be deduplicated."""
    adapter = CrtshAdapter()
    mock_pycrtsh = MagicMock()
    mock_crtsh_instance = MagicMock()
    mock_crtsh_instance.search.return_value = mock_crtsh_results
    mock_pycrtsh.Crtsh.return_value = mock_crtsh_instance

    with patch.dict(sys.modules, {"pycrtsh": mock_pycrtsh}):
        finding = await adapter.run(domain="example.com")

    # Base domain appears exactly once despite wildcard entry
    base_entities = [e for e in finding.entities if e.id == "domain:example.com"]
    assert len(base_entities) == 1


@pytest.mark.asyncio
async def test_run_subdomain_properties(mock_crtsh_results):
    adapter = CrtshAdapter()
    mock_pycrtsh = MagicMock()
    mock_crtsh_instance = MagicMock()
    mock_crtsh_instance.search.return_value = mock_crtsh_results
    mock_pycrtsh.Crtsh.return_value = mock_crtsh_instance

    with patch.dict(sys.modules, {"pycrtsh": mock_pycrtsh}):
        finding = await adapter.run(domain="example.com")

    www = next(e for e in finding.entities if e.id == "domain:www.example.com")
    assert www.properties["issuer"] == "Let's Encrypt Authority X3"
    assert www.properties["not_before"] == "2024-01-01"
    assert www.properties["not_after"] == "2024-04-01"


@pytest.mark.asyncio
async def test_run_filters_none_properties(mock_crtsh_results):
    """Properties with None values should be excluded from entity."""
    adapter = CrtshAdapter()
    mock_pycrtsh = MagicMock()
    mock_crtsh_instance = MagicMock()
    mock_crtsh_instance.search.return_value = mock_crtsh_results
    mock_pycrtsh.Crtsh.return_value = mock_crtsh_instance

    with patch.dict(sys.modules, {"pycrtsh": mock_pycrtsh}):
        finding = await adapter.run(domain="example.com")

    api = next(e for e in finding.entities if e.id == "domain:api.example.com")
    assert "not_before" not in api.properties
    assert "not_after" not in api.properties
    assert api.properties["issuer"] == "Sectigo RSA"


# ------------------------------------------------------------------
# Relationships
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_creates_connected_to_relationships(mock_crtsh_results):
    adapter = CrtshAdapter()
    mock_pycrtsh = MagicMock()
    mock_crtsh_instance = MagicMock()
    mock_crtsh_instance.search.return_value = mock_crtsh_results
    mock_pycrtsh.Crtsh.return_value = mock_crtsh_instance

    with patch.dict(sys.modules, {"pycrtsh": mock_pycrtsh}):
        finding = await adapter.run(domain="example.com")

    rels = finding.relationships
    # 3 unique subdomains (www, mail, api) — wildcard dedups to base
    assert len(rels) == 3
    assert all(r.relation_type == RelationType.CONNECTED_TO for r in rels)
    assert all(r.source_id == "domain:example.com" for r in rels)
    assert all(r.properties["via"] == "certificate_transparency" for r in rels)


@pytest.mark.asyncio
async def test_run_relationship_targets_match_entities(mock_crtsh_results):
    adapter = CrtshAdapter()
    mock_pycrtsh = MagicMock()
    mock_crtsh_instance = MagicMock()
    mock_crtsh_instance.search.return_value = mock_crtsh_results
    mock_pycrtsh.Crtsh.return_value = mock_crtsh_instance

    with patch.dict(sys.modules, {"pycrtsh": mock_pycrtsh}):
        finding = await adapter.run(domain="example.com")

    rel_targets = {r.target_id for r in finding.relationships}
    entity_ids = {e.id for e in finding.entities}
    assert rel_targets.issubset(entity_ids)


# ------------------------------------------------------------------
# Notes
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_notes_contain_subdomain_count(mock_crtsh_results):
    adapter = CrtshAdapter()
    mock_pycrtsh = MagicMock()
    mock_crtsh_instance = MagicMock()
    mock_crtsh_instance.search.return_value = mock_crtsh_results
    mock_pycrtsh.Crtsh.return_value = mock_crtsh_instance

    with patch.dict(sys.modules, {"pycrtsh": mock_pycrtsh}):
        finding = await adapter.run(domain="example.com")

    # 3 unique subdomains: www, mail, api
    assert "3 subdomains" in finding.notes
    assert "example.com" in finding.notes


# ------------------------------------------------------------------
# Sources
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_source_is_crtsh(mock_crtsh_results):
    adapter = CrtshAdapter()
    mock_pycrtsh = MagicMock()
    mock_crtsh_instance = MagicMock()
    mock_crtsh_instance.search.return_value = mock_crtsh_results
    mock_pycrtsh.Crtsh.return_value = mock_crtsh_instance

    with patch.dict(sys.modules, {"pycrtsh": mock_pycrtsh}):
        finding = await adapter.run(domain="example.com")

    for entity in finding.entities:
        assert entity.sources[0].tool == "crtsh"
    for rel in finding.relationships:
        assert rel.sources[0].tool == "crtsh"


# ------------------------------------------------------------------
# Deduplication
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_deduplicates_repeated_subdomains():
    """Same subdomain appearing in multiple certs should produce one entity."""
    adapter = CrtshAdapter()
    results = [
        {"name": "www.example.com", "issuer": "CA1"},
        {"name": "www.example.com", "issuer": "CA2"},
        {"name": "api.example.com", "issuer": "CA1"},
    ]
    mock_pycrtsh = MagicMock()
    mock_crtsh_instance = MagicMock()
    mock_crtsh_instance.search.return_value = results
    mock_pycrtsh.Crtsh.return_value = mock_crtsh_instance

    with patch.dict(sys.modules, {"pycrtsh": mock_pycrtsh}):
        finding = await adapter.run(domain="example.com")

    www_entities = [e for e in finding.entities if e.id == "domain:www.example.com"]
    assert len(www_entities) == 1
    # base + www + api = 3 entities
    assert len(finding.entities) == 3


# ------------------------------------------------------------------
# Empty name entries
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_skips_entries_with_empty_name():
    adapter = CrtshAdapter()
    results = [
        {"name": "", "issuer": "CA1"},
        {"name": "sub.example.com", "issuer": "CA2"},
    ]
    mock_pycrtsh = MagicMock()
    mock_crtsh_instance = MagicMock()
    mock_crtsh_instance.search.return_value = results
    mock_pycrtsh.Crtsh.return_value = mock_crtsh_instance

    with patch.dict(sys.modules, {"pycrtsh": mock_pycrtsh}):
        finding = await adapter.run(domain="example.com")

    # base + sub = 2
    assert len(finding.entities) == 2


@pytest.mark.asyncio
async def test_run_skips_entries_with_missing_name():
    adapter = CrtshAdapter()
    results = [
        {"issuer": "CA1"},  # no "name" key
        {"name": "sub.example.com", "issuer": "CA2"},
    ]
    mock_pycrtsh = MagicMock()
    mock_crtsh_instance = MagicMock()
    mock_crtsh_instance.search.return_value = results
    mock_pycrtsh.Crtsh.return_value = mock_crtsh_instance

    with patch.dict(sys.modules, {"pycrtsh": mock_pycrtsh}):
        finding = await adapter.run(domain="example.com")

    assert len(finding.entities) == 2


# ------------------------------------------------------------------
# Empty / no results
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_empty_results():
    adapter = CrtshAdapter()
    mock_pycrtsh = MagicMock()
    mock_crtsh_instance = MagicMock()
    mock_crtsh_instance.search.return_value = []
    mock_pycrtsh.Crtsh.return_value = mock_crtsh_instance

    with patch.dict(sys.modules, {"pycrtsh": mock_pycrtsh}):
        finding = await adapter.run(domain="nodata.com")

    assert len(finding.entities) == 0
    assert "no certificates" in finding.notes.lower()
    assert "nodata.com" in finding.notes


@pytest.mark.asyncio
async def test_run_none_results():
    adapter = CrtshAdapter()
    mock_pycrtsh = MagicMock()
    mock_crtsh_instance = MagicMock()
    mock_crtsh_instance.search.return_value = None
    mock_pycrtsh.Crtsh.return_value = mock_crtsh_instance

    with patch.dict(sys.modules, {"pycrtsh": mock_pycrtsh}):
        finding = await adapter.run(domain="none.com")

    assert len(finding.entities) == 0
    assert "no certificates" in finding.notes.lower()


# ------------------------------------------------------------------
# Error handling
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_handles_exception():
    adapter = CrtshAdapter()
    mock_pycrtsh = MagicMock()
    mock_crtsh_instance = MagicMock()
    mock_crtsh_instance.search.side_effect = ConnectionError("Timeout")
    mock_pycrtsh.Crtsh.return_value = mock_crtsh_instance

    with patch.dict(sys.modules, {"pycrtsh": mock_pycrtsh}):
        finding = await adapter.run(domain="fail.com")

    assert len(finding.entities) == 0
    assert "error" in finding.notes.lower()
    assert "Timeout" in finding.notes


@pytest.mark.asyncio
async def test_run_handles_runtime_error():
    adapter = CrtshAdapter()
    mock_pycrtsh = MagicMock()
    mock_crtsh_instance = MagicMock()
    mock_crtsh_instance.search.side_effect = RuntimeError("API unavailable")
    mock_pycrtsh.Crtsh.return_value = mock_crtsh_instance

    with patch.dict(sys.modules, {"pycrtsh": mock_pycrtsh}):
        finding = await adapter.run(domain="down.com")

    assert len(finding.entities) == 0
    assert "error" in finding.notes.lower()
