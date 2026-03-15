"""Tests for the SBIR.gov adapter."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from osint_agent.models import EntityType, RelationType
from osint_agent.tools.sbir import (
    SbirAdapter,
    _clean_tracking_number,
    _normalize_name,
    _parse_amount,
)


@pytest.fixture
def adapter():
    return SbirAdapter(timeout=10)


def test_name():
    adapter = SbirAdapter()
    assert adapter.name == "sbir"


def test_is_available_always_true():
    adapter = SbirAdapter()
    assert adapter.is_available() is True


# --- helper function tests ---

def test_normalize_name_basic():
    assert _normalize_name("Lockheed Martin") == "lockheed_martin"


def test_normalize_name_special_chars():
    assert _normalize_name("  ACME Corp.  ") == "acme_corp"


def test_normalize_name_multiple_spaces():
    assert _normalize_name("Some   Big   Company") == "some_big_company"


def test_parse_amount_standard():
    assert _parse_amount("$69,731.00") == 69731.00


def test_parse_amount_no_cents():
    assert _parse_amount("$100,000") == 100000.0


def test_parse_amount_empty():
    assert _parse_amount("") == 0.0


def test_parse_amount_zero():
    assert _parse_amount("$0") == 0.0


def test_parse_amount_no_dollar_sign():
    assert _parse_amount("1234.56") == 1234.56


def test_parse_amount_garbage():
    assert _parse_amount("N/A") == 0.0


def test_clean_tracking_number_standard():
    assert _clean_tracking_number("N08-092") == "N08-092"


def test_clean_tracking_number_spaces():
    assert _clean_tracking_number("  N08-092  ") == "N08-092"


def test_clean_tracking_number_empty():
    assert _clean_tracking_number("") == "unknown"


# --- mock API response data ---

MOCK_RESPONSE_DATA = [
    {
        "Agency": "DOD",
        "Branch": "Navy",
        "Program": "SBIR",
        "Phase": "1",
        "Agency Tracking Number": "N08-092",
        "Contract": "N68335-08-C-0295",
        "Proposal Title": "Low-Overhead SCA Core Framework",
        "Company": "Objective Interface Systems, Inc.",
        "Address": "220 Spring Street, Suite 530",
        "City": "Herndon",
        "State": "VA",
        "Zip": "20170",
        "Amount": "$69,731.00",
        "Award Year": "2008",
        "Award Start Date": "",
        "Award End Date": "",
        "PI": "R William Beckwith",
        "PI Phone": "(703) 295-6500",
        "PI Title": "President",
        "RI": "",
        "Abstract": "A short abstract about the project.",
        "DUNS": "098009040",
        "HUBZone Owned": "N",
        "Socially and Economically Disadvantaged": "N",
        "Woman Owned": "N",
    },
    {
        "Agency": "DOD",
        "Branch": "Army",
        "Program": "STTR",
        "Phase": "2",
        "Agency Tracking Number": "A09-001",
        "Contract": "W911NF-09-C-0001",
        "Proposal Title": "Advanced Signal Processing",
        "Company": "Objective Interface Systems, Inc.",
        "Address": "220 Spring Street, Suite 530",
        "City": "Herndon",
        "State": "VA",
        "Zip": "20170",
        "Amount": "$750,000.00",
        "Award Year": "2009",
        "Award Start Date": "",
        "Award End Date": "",
        "PI": "Jane Smith",
        "PI Phone": "(703) 555-1234",
        "PI Title": "CTO",
        "RI": "",
        "Abstract": "Another abstract for testing.",
        "DUNS": "098009040",
        "HUBZone Owned": "N",
        "Socially and Economically Disadvantaged": "N",
        "Woman Owned": "N",
    },
    {
        "Agency": "NASA",
        "Branch": "",
        "Program": "SBIR",
        "Phase": "1",
        "Agency Tracking Number": "NNX10-005",
        "Contract": "NNX10AB05C",
        "Proposal Title": "Lightweight Composite Structures",
        "Company": "SpaceTech LLC",
        "Address": "100 Rocket Way",
        "City": "Houston",
        "State": "TX",
        "Zip": "77001",
        "Amount": "$100,000.00",
        "Award Year": "2010",
        "Award Start Date": "",
        "Award End Date": "",
        "PI": "R William Beckwith",
        "PI Phone": "(703) 295-6500",
        "PI Title": "Consultant",
        "RI": "",
        "Abstract": "Testing abstract for NASA award.",
        "DUNS": "111222333",
        "HUBZone Owned": "Y",
        "Socially and Economically Disadvantaged": "N",
        "Woman Owned": "Y",
    },
]

MOCK_EMPTY_RESPONSE = []


def _make_mock_response(data):
    """Create a mock httpx response."""
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = data
    return mock_resp


# --- entity extraction tests ---

@pytest.mark.asyncio
async def test_search_firm_returns_entities(adapter):
    """Firm search should produce DOCUMENT, ORGANIZATION, and PERSON entities."""
    mock_resp = _make_mock_response(MOCK_RESPONSE_DATA)

    with patch("osint_agent.tools.sbir.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        finding = await adapter.run(query="Objective Interface", mode="firm")

    docs = [e for e in finding.entities if e.entity_type == EntityType.DOCUMENT]
    orgs = [e for e in finding.entities if e.entity_type == EntityType.ORGANIZATION]
    persons = [e for e in finding.entities if e.entity_type == EntityType.PERSON]

    # 3 awards, 2 unique companies, 2 unique PIs
    assert len(docs) == 3
    assert len(orgs) == 2
    assert len(persons) == 2


@pytest.mark.asyncio
async def test_document_properties(adapter):
    """Each DOCUMENT entity should carry award metadata in properties."""
    mock_resp = _make_mock_response(MOCK_RESPONSE_DATA)

    with patch("osint_agent.tools.sbir.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        finding = await adapter.run(query="test", mode="firm")

    first_doc = next(
        e for e in finding.entities if e.entity_type == EntityType.DOCUMENT
    )
    assert first_doc.properties["agency"] == "DOD"
    assert first_doc.properties["branch"] == "Navy"
    assert first_doc.properties["program"] == "SBIR"
    assert first_doc.properties["phase"] == "1"
    assert first_doc.properties["contract_number"] == "N68335-08-C-0295"
    assert first_doc.properties["amount"] == 69731.00
    assert first_doc.properties["award_year"] == "2008"
    assert first_doc.properties["pi_name"] == "R William Beckwith"
    assert first_doc.properties["pi_title"] == "President"
    assert first_doc.properties["abstract"] == "A short abstract about the project."


@pytest.mark.asyncio
async def test_organization_properties(adapter):
    """ORGANIZATION entity should carry company details."""
    mock_resp = _make_mock_response(MOCK_RESPONSE_DATA)

    with patch("osint_agent.tools.sbir.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        finding = await adapter.run(query="test", mode="firm")

    orgs = [e for e in finding.entities if e.entity_type == EntityType.ORGANIZATION]
    obj_org = next(o for o in orgs if "Objective" in o.label)
    assert obj_org.properties["city"] == "Herndon"
    assert obj_org.properties["state"] == "VA"
    assert obj_org.properties["duns"] == "098009040"
    assert obj_org.properties["hubzone"] == "N"


@pytest.mark.asyncio
async def test_person_properties(adapter):
    """PERSON entity should carry PI details."""
    mock_resp = _make_mock_response(MOCK_RESPONSE_DATA)

    with patch("osint_agent.tools.sbir.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        finding = await adapter.run(query="test", mode="firm")

    persons = [
        e for e in finding.entities if e.entity_type == EntityType.PERSON
    ]
    beckwith = next(p for p in persons if "Beckwith" in p.label)
    assert beckwith.properties["title"] == "President"
    assert beckwith.properties["phone"] == "(703) 295-6500"
    assert beckwith.id == "person:sbir:r_william_beckwith"


# --- deduplication tests ---

@pytest.mark.asyncio
async def test_deduplicates_organizations(adapter):
    """Repeated company names should produce only one ORGANIZATION entity."""
    mock_resp = _make_mock_response(MOCK_RESPONSE_DATA)

    with patch("osint_agent.tools.sbir.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        finding = await adapter.run(query="test", mode="firm")

    orgs = [e for e in finding.entities if e.entity_type == EntityType.ORGANIZATION]
    org_labels = [o.label for o in orgs]
    assert "Objective Interface Systems, Inc." in org_labels
    assert "SpaceTech LLC" in org_labels
    assert len(orgs) == 2


@pytest.mark.asyncio
async def test_deduplicates_persons(adapter):
    """Repeated PI names should produce only one PERSON entity."""
    mock_resp = _make_mock_response(MOCK_RESPONSE_DATA)

    with patch("osint_agent.tools.sbir.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        finding = await adapter.run(query="test", mode="firm")

    persons = [
        e for e in finding.entities if e.entity_type == EntityType.PERSON
    ]
    person_labels = [p.label for p in persons]
    # R William Beckwith appears in award 1 and 3 but should only have 1 entity
    assert person_labels.count("R William Beckwith") == 1
    assert "Jane Smith" in person_labels
    assert len(persons) == 2


# --- relationship tests ---

@pytest.mark.asyncio
async def test_relationships_org_filed_document(adapter):
    """Each award should produce an ORG -> FILED -> DOCUMENT relationship."""
    mock_resp = _make_mock_response(MOCK_RESPONSE_DATA)

    with patch("osint_agent.tools.sbir.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        finding = await adapter.run(query="test", mode="firm")

    org_rels = [
        r for r in finding.relationships
        if r.source_id.startswith("org:sbir:")
    ]
    assert len(org_rels) == 3
    for rel in org_rels:
        assert rel.relation_type == RelationType.FILED
        assert rel.target_id.startswith("document:sbir:")


@pytest.mark.asyncio
async def test_relationships_person_filed_document(adapter):
    """Each award with a PI should produce PERSON -> FILED -> DOCUMENT."""
    mock_resp = _make_mock_response(MOCK_RESPONSE_DATA)

    with patch("osint_agent.tools.sbir.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        finding = await adapter.run(query="test", mode="firm")

    person_rels = [
        r for r in finding.relationships
        if r.source_id.startswith("person:sbir:")
    ]
    assert len(person_rels) == 3
    for rel in person_rels:
        assert rel.relation_type == RelationType.FILED
        assert rel.target_id.startswith("document:sbir:")
        assert rel.properties.get("role") == "Principal Investigator"


# --- mode routing tests ---

@pytest.mark.asyncio
async def test_firm_mode_sends_firm_param(adapter):
    """Firm mode should send 'firm' query parameter."""
    mock_resp = _make_mock_response(MOCK_EMPTY_RESPONSE)

    with patch("osint_agent.tools.sbir.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        await adapter.run(query="ACME Corp", mode="firm")

    call_args = mock_client.get.call_args
    params = call_args.kwargs.get("params") or call_args[1].get("params")
    assert params["firm"] == "ACME Corp"
    assert "ri" not in params
    assert "keyword" not in params


@pytest.mark.asyncio
async def test_pi_mode_sends_ri_param(adapter):
    """PI mode should send 'ri' query parameter."""
    mock_resp = _make_mock_response(MOCK_EMPTY_RESPONSE)

    with patch("osint_agent.tools.sbir.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        await adapter.run(query="John Doe", mode="pi")

    call_args = mock_client.get.call_args
    params = call_args.kwargs.get("params") or call_args[1].get("params")
    assert params["ri"] == "John Doe"
    assert "firm" not in params
    assert "keyword" not in params


@pytest.mark.asyncio
async def test_keyword_mode_falls_back_to_firm(adapter):
    """Keyword mode should fall back to firm param (API has no keyword param)."""
    mock_resp = _make_mock_response(MOCK_EMPTY_RESPONSE)

    with patch("osint_agent.tools.sbir.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        await adapter.run(query="cybersecurity", mode="keyword")

    call_args = mock_client.get.call_args
    params = call_args.kwargs.get("params") or call_args[1].get("params")
    assert params["firm"] == "cybersecurity"
    assert "ri" not in params


@pytest.mark.asyncio
async def test_default_mode_is_firm(adapter):
    """Calling run without mode should default to firm search."""
    mock_resp = _make_mock_response(MOCK_EMPTY_RESPONSE)

    with patch("osint_agent.tools.sbir.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        finding = await adapter.run(query="test")

    assert "firm" in finding.notes
    call_args = mock_client.get.call_args
    params = call_args.kwargs.get("params") or call_args[1].get("params")
    assert "firm" in params


# --- empty results tests ---

@pytest.mark.asyncio
async def test_empty_results_returns_empty_finding(adapter):
    """When the API returns no results, finding should have empty entities."""
    mock_resp = _make_mock_response(MOCK_EMPTY_RESPONSE)

    with patch("osint_agent.tools.sbir.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        finding = await adapter.run(query="ZZZZNOTREAL", mode="firm")

    assert len(finding.entities) == 0
    assert len(finding.relationships) == 0
    assert "0 awards" in finding.notes


# --- notes tests ---

@pytest.mark.asyncio
async def test_notes_contain_total_amount(adapter):
    """Finding notes should report the total dollar amount."""
    mock_resp = _make_mock_response(MOCK_RESPONSE_DATA)

    with patch("osint_agent.tools.sbir.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        finding = await adapter.run(query="test", mode="firm")

    # Total is 69,731 + 750,000 + 100,000 = 919,731
    assert "$919,731.00" in finding.notes


@pytest.mark.asyncio
async def test_notes_contain_award_count(adapter):
    """Finding notes should report the number of awards."""
    mock_resp = _make_mock_response(MOCK_RESPONSE_DATA)

    with patch("osint_agent.tools.sbir.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        finding = await adapter.run(query="test", mode="firm")

    assert "3 awards" in finding.notes


# --- max_results cap test ---

@pytest.mark.asyncio
async def test_max_results_capped_at_500(adapter):
    """max_results should be capped at MAX_API_ROWS (500)."""
    mock_resp = _make_mock_response(MOCK_EMPTY_RESPONSE)

    with patch("osint_agent.tools.sbir.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        await adapter.run(query="test", mode="firm", max_results=5000)

    call_args = mock_client.get.call_args
    params = call_args.kwargs.get("params") or call_args[1].get("params")
    assert params["rows"] == 500


# --- source tests ---

@pytest.mark.asyncio
async def test_source_urls_point_to_sbir(adapter):
    """Entity sources should link to sbir.gov."""
    mock_resp = _make_mock_response(MOCK_RESPONSE_DATA)

    with patch("osint_agent.tools.sbir.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        finding = await adapter.run(query="test", mode="firm")

    first_doc = next(
        e for e in finding.entities if e.entity_type == EntityType.DOCUMENT
    )
    assert first_doc.sources[0].tool == "sbir"
    assert "sbir.gov" in first_doc.sources[0].source_url


# --- entity ID format tests ---

@pytest.mark.asyncio
async def test_document_entity_id_format(adapter):
    """Document entity IDs should follow 'document:sbir:<tracking>'."""
    mock_resp = _make_mock_response(MOCK_RESPONSE_DATA)

    with patch("osint_agent.tools.sbir.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        finding = await adapter.run(query="test", mode="firm")

    first_doc = next(
        e for e in finding.entities if e.entity_type == EntityType.DOCUMENT
    )
    assert first_doc.id == "document:sbir:N08-092"


# --- abstract truncation test ---

@pytest.mark.asyncio
async def test_abstract_truncated_to_500_chars(adapter):
    """Abstracts longer than 500 chars should be truncated."""
    long_abstract_data = [
        {
            **MOCK_RESPONSE_DATA[0],
            "Abstract": "A" * 1000,
        },
    ]
    mock_resp = _make_mock_response(long_abstract_data)

    with patch("osint_agent.tools.sbir.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        finding = await adapter.run(query="test", mode="firm")

    first_doc = next(
        e for e in finding.entities if e.entity_type == EntityType.DOCUMENT
    )
    assert len(first_doc.properties["abstract"]) == 500


# --- award with missing PI test ---

@pytest.mark.asyncio
async def test_award_without_pi_skips_person_entity(adapter):
    """Award with empty PI field should not create a PERSON entity."""
    no_pi_data = [
        {
            **MOCK_RESPONSE_DATA[0],
            "PI": "",
            "PI Title": "",
            "PI Phone": "",
        },
    ]
    mock_resp = _make_mock_response(no_pi_data)

    with patch("osint_agent.tools.sbir.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        finding = await adapter.run(query="test", mode="firm")

    persons = [
        e for e in finding.entities if e.entity_type == EntityType.PERSON
    ]
    assert len(persons) == 0
    person_rels = [
        r for r in finding.relationships
        if r.source_id.startswith("person:sbir:")
    ]
    assert len(person_rels) == 0


# --- registry integration tests ---

def test_registry_includes_sbir():
    """SBIR adapter should appear in the tool registry."""
    from osint_agent.tools.registry import ToolRegistry
    registry = ToolRegistry()
    avail = registry.available()
    assert "sbir" in avail
    assert avail["sbir"] is True


def test_input_routing_includes_sbir():
    """SBIR should be routed for company and person_name input types."""
    from osint_agent.tools.registry import INPUT_ROUTING
    assert "sbir" in INPUT_ROUTING["company"]
    assert "sbir" in INPUT_ROUTING["person_name"]
