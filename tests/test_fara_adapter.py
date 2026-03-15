"""Tests for the FARA adapter — Foreign Agents Registration Act."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from osint_agent.tools.fara import FaraAdapter, _extract_rows, _slug
from osint_agent.models import EntityType, RelationType


@pytest.fixture
def adapter():
    return FaraAdapter(timeout=10)


@pytest.fixture
def mock_registrants_active():
    return {
        "REGISTRANTS": {
            "ROW": [
                {
                    "Registration_Number": 6001,
                    "Name": "Mercury Public Affairs",
                    "Registration_Date": "2019-01-15",
                    "City": "Washington",
                    "State": "DC",
                    "Address_1": "1200 K Street NW",
                    "Zip": "20005",
                },
                {
                    "Registration_Number": 6002,
                    "Name": "Other Firm LLC",
                    "Registration_Date": "2020-06-01",
                    "City": "New York",
                    "State": "NY",
                    "Address_1": "123 Broadway",
                    "Zip": "10001",
                },
            ],
        },
    }


@pytest.fixture
def mock_registrants_terminated():
    return {"REGISTRANTS": {"ROW": []}}


@pytest.fixture
def mock_foreign_principals():
    return {
        "FOREIGNPRINCIPALS": {
            "ROW": [
                {
                    "FP_NAME": "Kingdom of Saudi Arabia",
                    "COUNTRY_NAME": "SAUDI ARABIA",
                    "CITY": "Riyadh",
                    "STATE": "",
                    "ADDRESS_1": "Palace",
                    "FP_REG_DATE": "2019-01-15",
                },
            ],
        },
    }


@pytest.fixture
def mock_documents():
    return {
        "REGDOCS": {
            "ROW": [
                {
                    "Document_Type": "Supplemental Statement",
                    "Stamped_Date": "2024-01-10",
                    "Url": "https://efile.fara.gov/docs/6001-Supplemental-Statement-20240110.pdf",
                },
            ],
        },
    }


# ------------------------------------------------------------------
# Availability and metadata
# ------------------------------------------------------------------

def test_is_available(adapter):
    assert adapter.is_available() is True


def test_adapter_name(adapter):
    assert adapter.name == "fara"


# ------------------------------------------------------------------
# Helper functions
# ------------------------------------------------------------------

def test_extract_rows_normal():
    data = {"REGISTRANTS": {"ROW": [{"a": 1}, {"a": 2}]}}
    assert len(_extract_rows(data)) == 2


def test_extract_rows_single():
    """Should wrap single ROW dict in a list."""
    data = {"REGISTRANTS": {"ROW": {"a": 1}}}
    assert _extract_rows(data) == [{"a": 1}]


def test_extract_rows_empty():
    assert _extract_rows({}) == []


def test_slug():
    assert _slug("Mercury Public Affairs") == "mercury_public_affairs"


def test_slug_truncates():
    long_name = "A" * 100
    assert len(_slug(long_name)) <= 50


def test_slug_empty():
    assert _slug("") == "unknown"


# ------------------------------------------------------------------
# Name search
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_search_name_finds_match(
    adapter, mock_registrants_active, mock_registrants_terminated,
    mock_foreign_principals, mock_documents,
):
    active_resp = MagicMock()
    active_resp.json.return_value = mock_registrants_active
    active_resp.raise_for_status = MagicMock()

    terminated_resp = MagicMock()
    terminated_resp.json.return_value = mock_registrants_terminated
    terminated_resp.raise_for_status = MagicMock()

    fp_resp = MagicMock()
    fp_resp.json.return_value = mock_foreign_principals
    fp_resp.raise_for_status = MagicMock()

    doc_resp = MagicMock()
    doc_resp.json.return_value = mock_documents
    doc_resp.raise_for_status = MagicMock()

    with patch("osint_agent.tools.fara.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        # Active registrants → FP active → FP terminated → RegDocs → Terminated registrants
        mock_client.get = AsyncMock(side_effect=[
            active_resp,      # Active registrants
            fp_resp,          # FP Active for 6001
            terminated_resp,  # FP Terminated for 6001
            doc_resp,         # RegDocs for 6001
            terminated_resp,  # Terminated registrants
        ])
        mock_cls.return_value = mock_client

        finding = await adapter.run(name="Mercury")

    # Should find Mercury Public Affairs but not Other Firm
    orgs = [e for e in finding.entities if e.entity_type == EntityType.ORGANIZATION]
    assert any(e.label == "Mercury Public Affairs" for e in orgs)
    assert not any(e.label == "Other Firm LLC" for e in orgs)

    # Should have foreign principal entity
    fp_entities = [e for e in orgs if "foreign_principal" in e.properties.get("entity_type", "")]
    assert len(fp_entities) == 1
    assert fp_entities[0].label == "Kingdom of Saudi Arabia"

    # Should have document entity
    docs = [e for e in finding.entities if e.entity_type == EntityType.DOCUMENT]
    assert len(docs) == 1

    # Should have relationships
    assert len(finding.relationships) >= 2  # AFFILIATED_WITH + FILED


@pytest.mark.asyncio
async def test_search_name_no_match(adapter, mock_registrants_terminated):
    empty_resp = MagicMock()
    empty_resp.json.return_value = mock_registrants_terminated
    empty_resp.raise_for_status = MagicMock()

    with patch("osint_agent.tools.fara.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=empty_resp)
        mock_cls.return_value = mock_client

        finding = await adapter.run(name="Nonexistent Firm")

    assert len(finding.entities) == 0
    assert "no registrants" in finding.notes.lower()


# ------------------------------------------------------------------
# Registration lookup
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_lookup_registration(
    adapter, mock_registrants_active,
    mock_foreign_principals, mock_documents,
):
    active_resp = MagicMock()
    active_resp.json.return_value = mock_registrants_active
    active_resp.raise_for_status = MagicMock()

    fp_resp = MagicMock()
    fp_resp.json.return_value = mock_foreign_principals
    fp_resp.raise_for_status = MagicMock()

    terminated_fp_resp = MagicMock()
    terminated_fp_resp.json.return_value = {"FP": {"ROW": []}}
    terminated_fp_resp.raise_for_status = MagicMock()

    doc_resp = MagicMock()
    doc_resp.json.return_value = mock_documents
    doc_resp.raise_for_status = MagicMock()

    with patch("osint_agent.tools.fara.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(side_effect=[
            active_resp,
            fp_resp,
            terminated_fp_resp,
            doc_resp,
        ])
        mock_cls.return_value = mock_client

        finding = await adapter.run(name="", registration_number=6001)

    assert any(
        e.properties.get("registration_number") == 6001
        for e in finding.entities
    )
    assert "6001" in finding.notes


# ------------------------------------------------------------------
# Registrant entity construction
# ------------------------------------------------------------------

def test_build_registrant_entity(adapter):
    row = {
        "Registration_Number": 6001,
        "Name": "Test Firm",
        "Registration_Date": "2020-01-01",
        "City": "DC",
        "State": "DC",
        "Address_1": "1600 Penn Ave",
        "Zip": "20500",
    }
    ent = adapter._build_registrant_entity(row, "active")
    assert ent.label == "Test Firm"
    assert ent.entity_type == EntityType.ORGANIZATION
    assert ent.properties["registration_number"] == 6001
    assert ent.properties["fara_status"] == "active"
    assert ent.id == "organization:fara:6001"
