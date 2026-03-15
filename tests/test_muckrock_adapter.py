"""Tests for the MuckRock adapter — FOIA request and agency search."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from osint_agent.tools.muckrock import MuckRockAdapter
from osint_agent.models import EntityType


@pytest.fixture
def adapter():
    return MuckRockAdapter(timeout=10)


@pytest.fixture
def mock_foia_page():
    """Canned FOIA request search results (one page)."""
    return {
        "results": [
            {
                "id": 99001,
                "title": "Proud Boys membership records",
                "slug": "proud-boys-membership-records",
                "status": "processed",
                "datetime_submitted": "2024-01-15T12:00:00Z",
                "datetime_done": "2024-03-01T09:00:00Z",
                "tracking_id": "FOI-2024-001",
                "price": "0.00",
                "agency": 42,
                "username": "journalist123",
                "tags": ["extremism", "militia"],
            },
            {
                "id": 99002,
                "title": "Police surveillance equipment purchases",
                "slug": "police-surveillance-equipment",
                "status": "ack",
                "datetime_submitted": "2024-02-20T10:00:00Z",
                "datetime_done": None,
                "tracking_id": "",
                "price": "25.00",
                "agency": 88,
                "username": "foia_hawk",
                "tags": [],
            },
            {
                "id": 99003,
                "title": "City council meeting minutes",
                "slug": "city-council-meeting-minutes",
                "status": "rejected",
                "datetime_submitted": "2024-03-01T08:00:00Z",
                "datetime_done": None,
                "tracking_id": None,
                "price": "0.00",
                "agency": None,
                "username": "citizen42",
                "tags": ["local-government"],
            },
        ],
        "next": None,
    }


@pytest.fixture
def mock_agency_page():
    """Canned agency search results (one page)."""
    return {
        "results": [
            {
                "id": 42,
                "name": "Portland Police Bureau",
                "slug": "portland-police-bureau",
                "status": "approved",
                "appeal_agency": None,
                "requires_proxy": False,
                "average_response_time": 30,
                "fee_rate": 0.1,
                "success_rate": 0.75,
                "jurisdiction": {
                    "name": "Portland, OR",
                    "level": "local",
                },
            },
            {
                "id": 43,
                "name": "Portland Fire & Rescue",
                "slug": "portland-fire-rescue",
                "status": "approved",
                "appeal_agency": 100,
                "requires_proxy": False,
                "average_response_time": 15,
                "fee_rate": None,
                "success_rate": 0.9,
                "jurisdiction": {
                    "name": "Portland, OR",
                    "level": "local",
                },
            },
            {
                "id": 200,
                "name": "FBI",
                "slug": "fbi",
                "status": "approved",
                "appeal_agency": None,
                "requires_proxy": False,
                "average_response_time": 120,
                "fee_rate": 0.25,
                "success_rate": 0.3,
                "jurisdiction": {
                    "name": "United States of America",
                    "level": "federal",
                },
            },
        ],
        "next": None,
    }


def _make_http_mock(json_data):
    """Build a MagicMock that acts like an httpx.Response."""
    resp = MagicMock()
    resp.json.return_value = json_data
    resp.raise_for_status = MagicMock()
    return resp


def _patch_client(responses):
    """Patch httpx.AsyncClient to return canned responses in order."""
    mock_cls = patch("osint_agent.tools.muckrock.httpx.AsyncClient")
    return mock_cls, responses


# ------------------------------------------------------------------
# Availability and metadata
# ------------------------------------------------------------------

def test_is_available(adapter):
    assert adapter.is_available() is True


def test_adapter_name(adapter):
    assert adapter.name == "muckrock"


# ------------------------------------------------------------------
# FOIA search — happy path
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_foia_search_matches_title(adapter, mock_foia_page):
    """should match FOIA requests whose title contains the query (case-insensitive)"""
    resp = _make_http_mock(mock_foia_page)

    with patch("osint_agent.tools.muckrock.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=resp)
        mock_cls.return_value = mock_client

        finding = await adapter.run(query="proud boys", mode="foia")

    # Only "Proud Boys membership records" should match
    assert len(finding.entities) == 1
    ent = finding.entities[0]
    assert ent.entity_type == EntityType.DOCUMENT
    assert ent.id == "document:muckrock:99001"
    assert ent.label == "Proud Boys membership records"

    # Check properties
    assert ent.properties["foia_status"] == "processed"
    assert ent.properties["document_source"] == "muckrock"
    assert ent.properties["requester"] == "journalist123"
    assert ent.properties["agency_id"] == 42
    assert ent.properties["foia_tags"] == ["extremism", "militia"]
    assert ent.properties["tracking_id"] == "FOI-2024-001"
    # price "0.00" should be excluded
    assert "price" not in ent.properties

    # URL
    assert "proud-boys-membership-records-99001" in ent.properties["url"]

    # Source
    assert ent.sources[0].tool == "muckrock"
    assert ent.sources[0].source_url is not None

    assert "1 FOIA" in finding.notes


@pytest.mark.asyncio
async def test_foia_search_matches_slug(adapter, mock_foia_page):
    """should match on slug as well as title"""
    resp = _make_http_mock(mock_foia_page)

    with patch("osint_agent.tools.muckrock.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=resp)
        mock_cls.return_value = mock_client

        # "surveillance" appears in the slug "police-surveillance-equipment"
        finding = await adapter.run(query="surveillance", mode="foia")

    assert len(finding.entities) == 1
    assert finding.entities[0].id == "document:muckrock:99002"
    # Price 25.00 should be included (not "0.00")
    assert finding.entities[0].properties["price"] == "25.00"


@pytest.mark.asyncio
async def test_foia_search_case_insensitive(adapter, mock_foia_page):
    """should match case-insensitively"""
    resp = _make_http_mock(mock_foia_page)

    with patch("osint_agent.tools.muckrock.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=resp)
        mock_cls.return_value = mock_client

        finding = await adapter.run(query="COUNCIL MEETING", mode="foia")

    assert len(finding.entities) == 1
    assert finding.entities[0].id == "document:muckrock:99003"


@pytest.mark.asyncio
async def test_foia_search_no_agency_id(adapter, mock_foia_page):
    """should handle FOIA request with null agency gracefully"""
    resp = _make_http_mock(mock_foia_page)

    with patch("osint_agent.tools.muckrock.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=resp)
        mock_cls.return_value = mock_client

        finding = await adapter.run(query="council meeting", mode="foia")

    ent = finding.entities[0]
    assert "agency_id" not in ent.properties


# ------------------------------------------------------------------
# FOIA search — empty / error
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_foia_search_no_match(adapter):
    """should return notes finding when query matches nothing"""
    resp = _make_http_mock({"results": [], "next": None})

    with patch("osint_agent.tools.muckrock.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=resp)
        mock_cls.return_value = mock_client

        finding = await adapter.run(query="xyznonexistent123", mode="foia")

    assert len(finding.entities) == 0
    assert "no FOIA requests" in finding.notes


@pytest.mark.asyncio
async def test_foia_search_http_error_first_page(adapter):
    """should return error notes when first page request fails"""
    with patch("osint_agent.tools.muckrock.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "503 Service Unavailable",
                request=MagicMock(),
                response=MagicMock(status_code=503),
            ),
        )
        mock_cls.return_value = mock_client

        finding = await adapter.run(query="test", mode="foia")

    assert len(finding.entities) == 0
    assert "error" in finding.notes.lower()


@pytest.mark.asyncio
async def test_foia_search_pagination(adapter):
    """should follow pagination until next is null"""
    page1 = {
        "results": [
            {
                "id": 1,
                "title": "Police records request",
                "slug": "police-records",
                "status": "processed",
                "agency": 10,
                "username": "user1",
                "tags": [],
            },
        ],
        "next": "https://www.muckrock.com/api_v1/foia/?page=2",
    }
    page2 = {
        "results": [
            {
                "id": 2,
                "title": "Police budget data",
                "slug": "police-budget",
                "status": "ack",
                "agency": 10,
                "username": "user2",
                "tags": [],
            },
        ],
        "next": None,
    }
    resp1 = _make_http_mock(page1)
    resp2 = _make_http_mock(page2)

    with patch("osint_agent.tools.muckrock.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(side_effect=[resp1, resp2])
        mock_cls.return_value = mock_client

        finding = await adapter.run(query="police", mode="foia")

    assert len(finding.entities) == 2
    ids = {e.id for e in finding.entities}
    assert "document:muckrock:1" in ids
    assert "document:muckrock:2" in ids


# ------------------------------------------------------------------
# Agency search — happy path
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_agency_search_matches(adapter, mock_agency_page):
    """should find agencies whose name contains the query"""
    resp = _make_http_mock(mock_agency_page)

    with patch("osint_agent.tools.muckrock.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=resp)
        mock_cls.return_value = mock_client

        finding = await adapter.run(query="portland", mode="agency")

    # Should match "Portland Police Bureau" and "Portland Fire & Rescue"
    assert len(finding.entities) == 2
    for ent in finding.entities:
        assert ent.entity_type == EntityType.ORGANIZATION
        assert "portland" in ent.label.lower()

    # Check properties on first entity
    ppb = next(e for e in finding.entities if e.label == "Portland Police Bureau")
    assert ppb.id == "organization:muckrock:42"
    assert ppb.properties["agency_type"] == "government"
    assert ppb.properties["jurisdiction"] == "Portland, OR"
    assert ppb.properties["jurisdiction_level"] == "local"
    assert ppb.properties["average_response_time"] == 30
    assert ppb.properties["success_rate"] == 0.75

    # URL
    assert "portland-police-bureau-42" in ppb.properties["url"]

    # Source
    assert ppb.sources[0].tool == "muckrock"

    assert "2 agency" in finding.notes


@pytest.mark.asyncio
async def test_agency_search_jurisdiction_as_id(adapter):
    """should handle jurisdiction as integer ID instead of dict"""
    page = {
        "results": [
            {
                "id": 500,
                "name": "Test Agency",
                "slug": "test-agency",
                "status": "approved",
                "jurisdiction": 77,
            },
        ],
        "next": None,
    }
    resp = _make_http_mock(page)

    with patch("osint_agent.tools.muckrock.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=resp)
        mock_cls.return_value = mock_client

        finding = await adapter.run(query="test", mode="agency")

    assert len(finding.entities) == 1
    assert finding.entities[0].properties["jurisdiction_id"] == 77


# ------------------------------------------------------------------
# Agency search — empty / error
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_agency_search_no_match(adapter):
    """should return notes finding when no agencies match"""
    resp = _make_http_mock({"results": [], "next": None})

    with patch("osint_agent.tools.muckrock.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=resp)
        mock_cls.return_value = mock_client

        finding = await adapter.run(query="xyznonexistent", mode="agency")

    assert len(finding.entities) == 0
    assert "no agencies" in finding.notes.lower()


@pytest.mark.asyncio
async def test_agency_search_http_error_first_page(adapter):
    """should return error notes when first agency page request fails"""
    with patch("osint_agent.tools.muckrock.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "500 Internal Server Error",
                request=MagicMock(),
                response=MagicMock(status_code=500),
            ),
        )
        mock_cls.return_value = mock_client

        finding = await adapter.run(query="test", mode="agency")

    assert len(finding.entities) == 0
    assert "error" in finding.notes.lower()


# ------------------------------------------------------------------
# Default mode
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_default_mode_is_foia(adapter, mock_foia_page):
    """should default to FOIA search when mode is not specified"""
    resp = _make_http_mock(mock_foia_page)

    with patch("osint_agent.tools.muckrock.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=resp)
        mock_cls.return_value = mock_client

        finding = await adapter.run(query="police")

    # Should have matched via FOIA (title contains "Police")
    docs = [e for e in finding.entities if e.entity_type == EntityType.DOCUMENT]
    assert len(docs) >= 1


# ------------------------------------------------------------------
# Entity construction details
# ------------------------------------------------------------------

def test_build_foia_entity_fallback_label(adapter):
    """should use slug as fallback title and FOIA # as last resort"""
    ent = adapter._build_foia_entity({
        "id": 12345,
        "title": "",
        "slug": "some-request",
        "status": "ack",
    })
    assert ent.label == "some-request"


def test_build_foia_entity_no_title_or_slug(adapter):
    """should use FOIA # as label when both title and slug are empty"""
    ent = adapter._build_foia_entity({
        "id": 12345,
        "title": "",
        "slug": "",
        "status": "ack",
    })
    assert ent.label == "FOIA #12345"


def test_build_agency_entity(adapter):
    """should construct organization entity with correct properties"""
    ent = adapter._build_agency_entity({
        "id": 42,
        "name": "Test Bureau",
        "slug": "test-bureau",
        "status": "approved",
        "average_response_time": 25,
    })
    assert ent.id == "organization:muckrock:42"
    assert ent.entity_type == EntityType.ORGANIZATION
    assert ent.label == "Test Bureau"
    assert ent.properties["agency_type"] == "government"
    assert ent.properties["status"] == "approved"
