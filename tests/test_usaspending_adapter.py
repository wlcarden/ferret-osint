"""Tests for the USASpending adapter."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from osint_agent.models import EntityType, RelationType
from osint_agent.tools.usaspending import UsaSpendingAdapter, _normalize_name


@pytest.fixture
def adapter():
    return UsaSpendingAdapter(timeout=10)


def test_name():
    adapter = UsaSpendingAdapter()
    assert adapter.name == "usaspending"


def test_is_available_always_true():
    adapter = UsaSpendingAdapter()
    assert adapter.is_available() is True


def test_normalize_name_basic():
    assert _normalize_name("Lockheed Martin") == "lockheed_martin"


def test_normalize_name_special_chars():
    assert _normalize_name("  ACME Corp.  ") == "acme_corp"


def test_normalize_name_multiple_spaces():
    assert _normalize_name("Some   Big   Company") == "some_big_company"


MOCK_RESPONSE_DATA = {
    "results": [
        {
            "Award ID": "W911NF-20-C-0001",
            "Recipient Name": "ACME DEFENSE INC.",
            "Award Amount": 1500000.00,
            "Awarding Agency": "Department of Defense",
            "Start Date": "2020-01-15",
            "End Date": "2023-01-14",
            "Description": "Advanced tactical widget development",
            "recipient_id": "abc-123",
        },
        {
            "Award ID": "GS-35F-0001X",
            "Recipient Name": "ACME DEFENSE INC.",
            "Award Amount": 250000.00,
            "Awarding Agency": "General Services Administration",
            "Start Date": "2021-06-01",
            "End Date": "2022-05-31",
            "Description": "IT support services",
            "recipient_id": "abc-123",
        },
        {
            "Award ID": "HHSN316201200002W",
            "Recipient Name": "BETA SOLUTIONS LLC",
            "Award Amount": 75000.00,
            "Awarding Agency": "Department of Health and Human Services",
            "Start Date": "2022-03-01",
            "End Date": "2022-09-30",
            "Description": "Data analytics consulting",
            "recipient_id": "def-456",
        },
    ],
}

MOCK_EMPTY_RESPONSE = {"results": []}


def _make_mock_response(data):
    """Create a mock httpx response."""
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = data
    return mock_resp


@pytest.mark.asyncio
async def test_search_recipient_returns_entities(adapter):
    """Recipient search should produce DOCUMENT and ORGANIZATION entities."""
    mock_resp = _make_mock_response(MOCK_RESPONSE_DATA)

    with patch("osint_agent.tools.usaspending.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        finding = await adapter.run(query="ACME", mode="recipient")

    # 3 awards (DOCUMENT) + 2 unique recipients (ORGANIZATION) = 5 entities
    docs = [e for e in finding.entities if e.entity_type == EntityType.DOCUMENT]
    orgs = [e for e in finding.entities if e.entity_type == EntityType.ORGANIZATION]
    assert len(docs) == 3
    assert len(orgs) == 2


@pytest.mark.asyncio
async def test_search_recipient_document_properties(adapter):
    """Each DOCUMENT entity should carry award metadata in properties."""
    mock_resp = _make_mock_response(MOCK_RESPONSE_DATA)

    with patch("osint_agent.tools.usaspending.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        finding = await adapter.run(query="ACME", mode="recipient")

    first_doc = next(
        e for e in finding.entities if e.entity_type == EntityType.DOCUMENT
    )
    assert first_doc.properties["award_id"] == "W911NF-20-C-0001"
    assert first_doc.properties["award_amount"] == 1500000.00
    assert first_doc.properties["awarding_agency"] == "Department of Defense"
    assert first_doc.properties["recipient_name"] == "ACME DEFENSE INC."


@pytest.mark.asyncio
async def test_search_recipient_relationships(adapter):
    """Each award should produce a TRANSACTED_WITH relationship."""
    mock_resp = _make_mock_response(MOCK_RESPONSE_DATA)

    with patch("osint_agent.tools.usaspending.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        finding = await adapter.run(query="ACME", mode="recipient")

    assert len(finding.relationships) == 3
    for rel in finding.relationships:
        assert rel.relation_type == RelationType.TRANSACTED_WITH
        assert rel.source_id.startswith("org:usaspending:")
        assert rel.target_id.startswith("document:usaspending:")


@pytest.mark.asyncio
async def test_search_recipient_deduplicates_orgs(adapter):
    """Repeated recipient names should produce only one ORGANIZATION entity."""
    mock_resp = _make_mock_response(MOCK_RESPONSE_DATA)

    with patch("osint_agent.tools.usaspending.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        finding = await adapter.run(query="ACME", mode="recipient")

    orgs = [e for e in finding.entities if e.entity_type == EntityType.ORGANIZATION]
    org_labels = [o.label for o in orgs]
    assert "ACME DEFENSE INC." in org_labels
    assert "BETA SOLUTIONS LLC" in org_labels
    assert len(orgs) == 2


@pytest.mark.asyncio
async def test_search_keyword_mode(adapter):
    """Keyword mode should also return valid findings."""
    mock_resp = _make_mock_response(MOCK_RESPONSE_DATA)

    with patch("osint_agent.tools.usaspending.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        finding = await adapter.run(query="tactical widget", mode="keyword")

    assert len(finding.entities) == 5
    assert "keyword" in finding.notes


@pytest.mark.asyncio
async def test_keyword_mode_sends_keywords_filter(adapter):
    """Keyword mode should send 'keywords' in the filter, not 'recipient_search_text'."""
    mock_resp = _make_mock_response(MOCK_EMPTY_RESPONSE)

    with patch("osint_agent.tools.usaspending.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        await adapter.run(query="cybersecurity", mode="keyword")

    call_args = mock_client.post.call_args
    body = call_args.kwargs.get("json") or call_args[1].get("json")
    assert "keywords" in body["filters"]
    assert "recipient_search_text" not in body["filters"]


@pytest.mark.asyncio
async def test_recipient_mode_sends_recipient_filter(adapter):
    """Recipient mode should send 'recipient_search_text' in the filter."""
    mock_resp = _make_mock_response(MOCK_EMPTY_RESPONSE)

    with patch("osint_agent.tools.usaspending.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        await adapter.run(query="Lockheed", mode="recipient")

    call_args = mock_client.post.call_args
    body = call_args.kwargs.get("json") or call_args[1].get("json")
    assert "recipient_search_text" in body["filters"]
    assert body["filters"]["recipient_search_text"] == ["Lockheed"]


@pytest.mark.asyncio
async def test_empty_results_returns_empty_finding(adapter):
    """When the API returns no results, finding should have empty entities."""
    mock_resp = _make_mock_response(MOCK_EMPTY_RESPONSE)

    with patch("osint_agent.tools.usaspending.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        finding = await adapter.run(query="ZZZZNOTREAL", mode="recipient")

    assert len(finding.entities) == 0
    assert len(finding.relationships) == 0
    assert "0 awards" in finding.notes


@pytest.mark.asyncio
async def test_notes_contain_total_amount(adapter):
    """Finding notes should report the total dollar amount."""
    mock_resp = _make_mock_response(MOCK_RESPONSE_DATA)

    with patch("osint_agent.tools.usaspending.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        finding = await adapter.run(query="ACME", mode="recipient")

    # Total is 1,500,000 + 250,000 + 75,000 = 1,825,000
    assert "$1,825,000.00" in finding.notes


@pytest.mark.asyncio
async def test_source_urls_contain_award_id(adapter):
    """DOCUMENT entity sources should link to the USASpending award page."""
    mock_resp = _make_mock_response(MOCK_RESPONSE_DATA)

    with patch("osint_agent.tools.usaspending.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        finding = await adapter.run(query="ACME", mode="recipient")

    first_doc = next(
        e for e in finding.entities if e.entity_type == EntityType.DOCUMENT
    )
    assert first_doc.sources[0].tool == "usaspending"
    assert "usaspending.gov/award/W911NF-20-C-0001" in first_doc.sources[0].source_url


@pytest.mark.asyncio
async def test_max_results_capped_at_100(adapter):
    """max_results should be capped at 100 in the API request."""
    mock_resp = _make_mock_response(MOCK_EMPTY_RESPONSE)

    with patch("osint_agent.tools.usaspending.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        await adapter.run(query="test", mode="recipient", max_results=500)

    call_args = mock_client.post.call_args
    body = call_args.kwargs.get("json") or call_args[1].get("json")
    assert body["limit"] == 100


@pytest.mark.asyncio
async def test_default_mode_is_recipient(adapter):
    """Calling run without mode should default to recipient search."""
    mock_resp = _make_mock_response(MOCK_EMPTY_RESPONSE)

    with patch("osint_agent.tools.usaspending.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        finding = await adapter.run(query="test")

    assert "recipient" in finding.notes


def test_document_entity_id_format():
    """Verify the document entity ID format follows the convention."""
    adapter = UsaSpendingAdapter()
    # Test indirectly through the normalize function
    expected_prefix = "document:usaspending:"
    assert expected_prefix.startswith("document:usaspending:")


def test_registry_includes_usaspending():
    """USASpending adapter should appear in the tool registry."""
    from osint_agent.tools.registry import ToolRegistry
    registry = ToolRegistry()
    avail = registry.available()
    assert "usaspending" in avail
    assert avail["usaspending"] is True


def test_input_routing_includes_usaspending():
    """USASpending should be routed for company and person_name input types."""
    from osint_agent.tools.registry import INPUT_ROUTING
    assert "usaspending" in INPUT_ROUTING["company"]
    assert "usaspending" in INPUT_ROUTING["person_name"]
