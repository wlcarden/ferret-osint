"""Tests for the ProPublica Nonprofit Explorer adapter."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from osint_agent.models import EntityType, RelationType
from osint_agent.tools.propublica_nonprofit import (
    ProPublicaNonprofitAdapter,
    _ntee_category,
)


@pytest.fixture
def adapter():
    return ProPublicaNonprofitAdapter(timeout=10)


@pytest.fixture
def mock_search_response():
    """Canned search.json response with two organizations."""
    return {
        "organizations": [
            {
                "ein": 123456789,
                "name": "NATIONAL RIFLE ASSOCIATION",
                "city": "FAIRFAX",
                "state": "VA",
                "ntee_code": "R20",
                "subseccd": 4,
                "classification_codes": "1000",
                "ruling_date": "194012",
                "tax_period": 202212,
                "income_amount": 350000000,
                "revenue_amt": 310000000,
                "asset_amount": 250000000,
            },
            {
                "ein": 987654321,
                "name": "NRA FOUNDATION",
                "city": "FAIRFAX",
                "state": "VA",
                "ntee_code": "B30",
                "subseccd": 3,
                "classification_codes": "1000",
                "ruling_date": "199006",
                "tax_period": 202212,
                "income_amount": 50000000,
                "revenue_amt": 45000000,
                "asset_amount": 60000000,
            },
        ],
    }


@pytest.fixture
def mock_detail_response():
    """Canned organizations/{ein}.json response."""
    return {
        "organization": {
            "ein": 123456789,
            "name": "NATIONAL RIFLE ASSOCIATION",
            "city": "FAIRFAX",
            "state": "VA",
            "ntee_code": "R20",
        },
        "filings_with_data": [
            {
                "tax_prd_yr": 2022,
                "totrevenue": 310000000,
                "totfuncexpns": 305000000,
                "totassetsend": 250000000,
                "totliabend": 200000000,
                "pdf_url": "https://projects.propublica.org/nonprofits/download-filing?path=12-2022_123456789_990.pdf",
            },
            {
                "tax_prd_yr": 2021,
                "totrevenue": 290000000,
                "totfuncexpns": 280000000,
                "totassetsend": 240000000,
                "totliabend": 190000000,
                "pdf_url": "https://projects.propublica.org/nonprofits/download-filing?path=12-2021_123456789_990.pdf",
            },
        ],
        "filings_without_data": [],
    }


@pytest.fixture
def mock_empty_search():
    return {"organizations": []}


def _make_mock_client(responses):
    """Build a mock httpx.AsyncClient that returns responses in order."""
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    mock_resps = []
    for data in responses:
        resp = MagicMock()
        resp.json.return_value = data
        resp.raise_for_status = MagicMock()
        mock_resps.append(resp)

    mock_client.get = AsyncMock(side_effect=mock_resps)
    return mock_client


# ------------------------------------------------------------------
# Availability and metadata
# ------------------------------------------------------------------

def test_is_available(adapter):
    assert adapter.is_available() is True


def test_adapter_name(adapter):
    assert adapter.name == "propublica_nonprofit"


# ------------------------------------------------------------------
# NTEE category helper
# ------------------------------------------------------------------

def test_ntee_category_arts():
    assert _ntee_category("A20") == "Arts, Culture & Humanities"


def test_ntee_category_education():
    assert _ntee_category("B30") == "Education"


def test_ntee_category_civil_rights():
    assert _ntee_category("R20") == "Civil Rights, Social Action & Advocacy"


def test_ntee_category_unknown_code():
    assert _ntee_category("Z99") == "Unknown"


def test_ntee_category_empty():
    assert _ntee_category("") == "Unknown"


# ------------------------------------------------------------------
# Name search — happy path
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_search_name_finds_orgs(
    adapter, mock_search_response, mock_detail_response,
):
    """should return organization entities and fetch details for top result"""
    mock_client = _make_mock_client([
        mock_search_response,   # search.json
        mock_detail_response,   # organizations/{ein}.json for top result
    ])

    with patch("osint_agent.tools.propublica_nonprofit.httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = mock_client
        finding = await adapter.run(name="NRA")

    # Should have at least two org entities from search
    orgs = [e for e in finding.entities if e.entity_type == EntityType.ORGANIZATION]
    assert len(orgs) >= 2
    labels = {o.label for o in orgs}
    assert "NATIONAL RIFLE ASSOCIATION" in labels
    assert "NRA FOUNDATION" in labels

    # Top org should have financial data from detail fetch
    top_org = next(o for o in orgs if o.label == "NATIONAL RIFLE ASSOCIATION")
    assert top_org.properties["ein"] == 123456789
    assert top_org.properties["city"] == "FAIRFAX"
    assert top_org.properties["state"] == "VA"
    assert top_org.properties["ntee_code"] == "R20"
    assert top_org.properties["ntee_description"] == "Civil Rights, Social Action & Advocacy"

    # Financial data from filings should be applied to top org
    assert top_org.properties["total_revenue"] == 310000000
    assert top_org.properties["total_expenses"] == 305000000
    assert top_org.properties["total_assets"] == 250000000
    assert top_org.properties["total_liabilities"] == 200000000
    assert top_org.properties["tax_period"] == 2022

    # Filing URLs should be present
    assert "filing_urls" in top_org.properties
    assert len(top_org.properties["filing_urls"]) == 2

    # Notes should mention result count and top result
    assert "2 result(s)" in finding.notes
    assert "NATIONAL RIFLE ASSOCIATION" in finding.notes
    assert "123456789" in finding.notes

    # Organization URL should be set
    assert top_org.properties["url"] == (
        "https://projects.propublica.org/nonprofits/organizations/123456789"
    )
    assert top_org.properties["organization_type"] == "nonprofit"


@pytest.mark.asyncio
async def test_search_name_no_results(adapter, mock_empty_search):
    """should return notes-only finding when no nonprofits match"""
    mock_client = _make_mock_client([mock_empty_search])

    with patch("osint_agent.tools.propublica_nonprofit.httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = mock_client
        finding = await adapter.run(name="ZZZZ Nonexistent Org")

    assert len(finding.entities) == 0
    assert "no nonprofits found" in finding.notes.lower()
    assert "ZZZZ Nonexistent Org" in finding.notes


@pytest.mark.asyncio
async def test_search_name_http_error(adapter):
    """should return error finding on HTTP failure"""
    import httpx

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(
        side_effect=httpx.HTTPError("Connection refused"),
    )

    with patch("osint_agent.tools.propublica_nonprofit.httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = mock_client
        finding = await adapter.run(name="Test Org")

    assert "error" in finding.notes.lower()
    assert len(finding.entities) == 0


# ------------------------------------------------------------------
# EIN lookup
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_lookup_ein_success(adapter, mock_detail_response):
    """should look up a specific nonprofit by EIN"""
    mock_client = _make_mock_client([mock_detail_response])

    with patch("osint_agent.tools.propublica_nonprofit.httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = mock_client
        finding = await adapter.run(name="", ein="12-3456789")

    # Should strip the hyphen and call with clean EIN
    call_url = mock_client.get.call_args[0][0]
    assert "123456789" in call_url
    assert "-" not in call_url.split("/")[-1].replace(".json", "")

    orgs = [e for e in finding.entities if e.entity_type == EntityType.ORGANIZATION]
    assert len(orgs) == 1
    assert orgs[0].label == "NATIONAL RIFLE ASSOCIATION"
    assert "2 filing(s)" in finding.notes


@pytest.mark.asyncio
async def test_lookup_ein_not_found(adapter):
    """should handle 404 for unknown EIN"""
    import httpx

    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = 404

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(
        side_effect=httpx.HTTPStatusError(
            "Not Found",
            request=MagicMock(),
            response=mock_resp,
        ),
    )

    with patch("osint_agent.tools.propublica_nonprofit.httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = mock_client
        finding = await adapter.run(name="", ein="000000000")

    assert "no nonprofit with EIN" in finding.notes
    assert len(finding.entities) == 0


@pytest.mark.asyncio
async def test_lookup_ein_server_error(adapter):
    """should handle non-404 HTTP errors"""
    import httpx

    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = 500

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(
        side_effect=httpx.HTTPStatusError(
            "Internal Server Error",
            request=MagicMock(),
            response=mock_resp,
        ),
    )

    with patch("osint_agent.tools.propublica_nonprofit.httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = mock_client
        finding = await adapter.run(name="", ein="123456789")

    assert "500" in finding.notes
    assert len(finding.entities) == 0


@pytest.mark.asyncio
async def test_lookup_ein_no_filings(adapter):
    """should handle organization with no filings"""
    detail = {
        "organization": {
            "ein": 111111111,
            "name": "TINY NONPROFIT",
            "city": "NOWHERE",
            "state": "KS",
        },
        "filings_with_data": [],
        "filings_without_data": [],
    }
    mock_client = _make_mock_client([detail])

    with patch("osint_agent.tools.propublica_nonprofit.httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = mock_client
        finding = await adapter.run(name="", ein="111111111")

    orgs = [e for e in finding.entities if e.entity_type == EntityType.ORGANIZATION]
    assert len(orgs) == 1
    assert orgs[0].label == "TINY NONPROFIT"
    # Should not have financial fields since there are no filings
    assert "total_revenue" not in orgs[0].properties
    assert "0 filing(s)" in finding.notes


# ------------------------------------------------------------------
# Entity construction
# ------------------------------------------------------------------

def test_build_org_entity_full_data(adapter):
    org_data = {
        "ein": 123456789,
        "name": "TEST FOUNDATION",
        "city": "NEW YORK",
        "state": "NY",
        "ntee_code": "T20",
        "subseccd": 3,
        "classification_codes": "1000",
        "ruling_date": "199501",
        "tax_period": 202312,
        "income_amount": 5000000,
        "revenue_amt": 4800000,
        "asset_amount": 10000000,
    }
    ent = adapter._build_org_entity(org_data)

    assert ent.entity_type == EntityType.ORGANIZATION
    assert ent.label == "TEST FOUNDATION"
    assert ent.id == "organization:nonprofit:123456789"
    assert ent.properties["ein"] == 123456789
    assert ent.properties["city"] == "NEW YORK"
    assert ent.properties["state"] == "NY"
    assert ent.properties["ntee_code"] == "T20"
    assert ent.properties["ntee_description"] == "Philanthropy & Voluntarism"
    assert ent.properties["income_amount"] == 5000000
    assert ent.properties["revenue_amount"] == 4800000
    assert ent.properties["organization_type"] == "nonprofit"
    assert "123456789" in ent.properties["url"]


def test_build_org_entity_minimal(adapter):
    """should handle org data with only name (no EIN, no NTEE)"""
    org_data = {"name": "MYSTERY ORG"}
    ent = adapter._build_org_entity(org_data)

    assert ent.label == "MYSTERY ORG"
    assert ent.id == "organization:nonprofit:mystery_org"
    assert ent.properties["organization_type"] == "nonprofit"
    # No EIN in properties
    assert "ein" not in ent.properties


def test_build_org_entity_empty_string_fields(adapter):
    """should not include empty string values in properties"""
    org_data = {
        "ein": 123,
        "name": "TEST",
        "city": "",
        "state": "",
        "ntee_code": "",
    }
    ent = adapter._build_org_entity(org_data)
    assert "city" not in ent.properties
    assert "state" not in ent.properties
    assert "ntee_code" not in ent.properties


# ------------------------------------------------------------------
# Filing processing
# ------------------------------------------------------------------

def test_process_filings_applies_financial_data(adapter):
    """should apply latest filing financials to org entity properties"""
    from osint_agent.models import Entity, EntityType, Source

    org = Entity(
        id="organization:nonprofit:111",
        entity_type=EntityType.ORGANIZATION,
        label="TEST ORG",
        properties={"organization_type": "nonprofit"},
        sources=[Source(tool="propublica_nonprofit")],
    )
    filings = [
        {
            "tax_prd_yr": 2023,
            "totrevenue": 1000000,
            "totfuncexpns": 900000,
            "totassetsend": 500000,
            "totliabend": 200000,
            "pdf_url": "https://example.com/2023.pdf",
        },
        {
            "tax_prd_yr": 2022,
            "totrevenue": 800000,
            "totfuncexpns": 750000,
            "totassetsend": 400000,
            "totliabend": 150000,
            "pdf_url": "https://example.com/2022.pdf",
        },
    ]

    ents, rels = adapter._process_filings(org, filings)

    # Financial data from latest filing applied to org
    assert org.properties["tax_period"] == 2023
    assert org.properties["total_revenue"] == 1000000
    assert org.properties["total_expenses"] == 900000
    assert org.properties["total_assets"] == 500000
    assert org.properties["total_liabilities"] == 200000

    # PDF URLs collected
    assert len(org.properties["filing_urls"]) == 2


def test_process_filings_empty(adapter):
    """should return empty lists when no filings"""
    from osint_agent.models import Entity, EntityType, Source

    org = Entity(
        id="organization:nonprofit:111",
        entity_type=EntityType.ORGANIZATION,
        label="TEST ORG",
        properties={},
        sources=[Source(tool="propublica_nonprofit")],
    )
    ents, rels = adapter._process_filings(org, [])
    assert ents == []
    assert rels == []


# ------------------------------------------------------------------
# Detail fetch failure
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_search_detail_fetch_fails_gracefully(
    adapter, mock_search_response,
):
    """should still return search results when detail fetch fails"""
    import httpx

    search_resp = MagicMock()
    search_resp.json.return_value = mock_search_response
    search_resp.raise_for_status = MagicMock()

    detail_resp = MagicMock()
    detail_resp.raise_for_status.side_effect = httpx.HTTPError("timeout")

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(side_effect=[search_resp, detail_resp])

    with patch("osint_agent.tools.propublica_nonprofit.httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = mock_client
        finding = await adapter.run(name="NRA")

    # Should still have org entities from search, no crash
    orgs = [e for e in finding.entities if e.entity_type == EntityType.ORGANIZATION]
    assert len(orgs) >= 2


# ------------------------------------------------------------------
# Registry
# ------------------------------------------------------------------

def test_registered_in_registry():
    from osint_agent.tools.registry import ToolRegistry, INPUT_ROUTING

    assert "propublica_nonprofit" in INPUT_ROUTING["company"]
    registry = ToolRegistry()
    adapter = registry.get("propublica_nonprofit")
    assert adapter is not None
