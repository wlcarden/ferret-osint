"""Tests for the Congress.gov adapter — legislative data."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from osint_agent.models import EntityType, RelationType
from osint_agent.tools.congress import (
    CongressAdapter,
    _bill_type_path,
    _slug,
)


@pytest.fixture
def adapter():
    return CongressAdapter(timeout=10)


@pytest.fixture
def mock_members_response():
    return {
        "members": [
            {
                "name": "Warren, Elizabeth",
                "bioguideId": "W000817",
                "state": "Massachusetts",
                "partyName": "Democratic",
                "terms": {
                    "item": [{"chamber": "Senate"}],
                },
                "url": "https://api.congress.gov/v3/member/W000817",
                "depiction": {"imageUrl": "https://example.com/warren.jpg"},
            },
            {
                "name": "Smith, John",
                "bioguideId": "S000999",
                "state": "Ohio",
                "partyName": "Republican",
                "terms": {"item": [{"chamber": "House"}]},
                "url": None,
                "depiction": None,
            },
            {
                "name": "Unrelated, Person",
                "bioguideId": "U000001",
                "state": "Alaska",
                "partyName": "Independent",
                "terms": {},
                "url": None,
                "depiction": None,
            },
        ],
    }


@pytest.fixture
def mock_bills_response():
    return {
        "bills": [
            {
                "type": "S",
                "number": "1234",
                "congress": 118,
                "title": "Anti-Corruption and Public Integrity Act",
                "originChamber": "Senate",
                "latestAction": {
                    "text": "Referred to committee",
                    "actionDate": "2024-01-15",
                },
                "url": "https://api.congress.gov/v3/bill/118/s/1234",
                "sponsors": [
                    {
                        "fullName": "Warren, Elizabeth",
                        "party": "D",
                        "state": "MA",
                    },
                ],
            },
        ],
    }


# ------------------------------------------------------------------
# Availability
# ------------------------------------------------------------------

def test_is_available_with_key():
    with patch.dict("os.environ", {"CONGRESS_API_KEY": "test_key"}):
        assert CongressAdapter().is_available() is True


def test_is_not_available_without_key():
    with patch.dict("os.environ", {}, clear=True):
        assert CongressAdapter().is_available() is False


def test_adapter_name():
    assert CongressAdapter().name == "congress"


# ------------------------------------------------------------------
# Helper functions
# ------------------------------------------------------------------

def test_bill_type_path_hr():
    assert _bill_type_path("HR") == "house-bill"


def test_bill_type_path_s():
    assert _bill_type_path("S") == "senate-bill"


def test_bill_type_path_hres():
    assert _bill_type_path("HRES") == "house-resolution"


def test_bill_type_path_unknown():
    assert _bill_type_path("UNKNOWN") == "bill"


def test_slug():
    assert _slug("Warren, Elizabeth") == "warren_elizabeth"


def test_slug_empty():
    assert _slug("") == "unknown"


# ------------------------------------------------------------------
# Member search
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_search_members(adapter, mock_members_response):
    mock_resp = MagicMock()
    mock_resp.json.return_value = mock_members_response
    mock_resp.raise_for_status = MagicMock()

    with patch.dict("os.environ", {"CONGRESS_API_KEY": "test_key"}):
        with patch("osint_agent.tools.congress.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_cls.return_value = mock_client

            finding = await adapter.run(query="Warren", mode="member")

    # Should find "Warren, Elizabeth" only
    assert len(finding.entities) == 1
    member = finding.entities[0]
    assert member.label == "Warren, Elizabeth"
    assert member.entity_type == EntityType.PERSON
    assert member.properties["bioguide_id"] == "W000817"
    assert member.properties["state"] == "Massachusetts"
    assert member.properties["party"] == "Democratic"
    assert member.properties["chamber"] == "Senate"


@pytest.mark.asyncio
async def test_search_members_no_key(adapter):
    with patch.dict("os.environ", {}, clear=True):
        with patch("osint_agent.tools.congress.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_client

            finding = await adapter.run(query="Warren")

    assert "CONGRESS_API_KEY not set" in finding.notes
    assert "sign-up" in finding.notes.lower()


@pytest.mark.asyncio
async def test_search_members_empty(adapter):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"members": []}
    mock_resp.raise_for_status = MagicMock()

    with patch.dict("os.environ", {"CONGRESS_API_KEY": "test_key"}):
        with patch("osint_agent.tools.congress.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_cls.return_value = mock_client

            finding = await adapter.run(query="Zzz Nonexistent")

    assert len(finding.entities) == 0
    assert "0 member" in finding.notes


# ------------------------------------------------------------------
# Bill search
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_search_bills(adapter, mock_bills_response):
    mock_resp = MagicMock()
    mock_resp.json.return_value = mock_bills_response
    mock_resp.raise_for_status = MagicMock()

    with patch.dict("os.environ", {"CONGRESS_API_KEY": "test_key"}):
        with patch("osint_agent.tools.congress.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_cls.return_value = mock_client

            finding = await adapter.run(query="corruption", mode="bill")

    # Should have bill entity + sponsor entity
    docs = [e for e in finding.entities if e.entity_type == EntityType.DOCUMENT]
    persons = [e for e in finding.entities if e.entity_type == EntityType.PERSON]
    assert len(docs) == 1
    assert len(persons) == 1

    bill = docs[0]
    assert bill.id == "document:congress:s1234-118"
    assert "Anti-Corruption" in bill.label
    assert bill.properties["bill_type"] == "S"
    assert bill.properties["congress"] == 118
    assert bill.properties["latest_action"] == "Referred to committee"

    sponsor = persons[0]
    assert sponsor.label == "Warren, Elizabeth"

    # Should have FILED relationship
    assert len(finding.relationships) == 1
    assert finding.relationships[0].relation_type == RelationType.FILED
    assert finding.relationships[0].properties["role"] == "sponsor"


@pytest.mark.asyncio
async def test_search_bills_no_match(adapter):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "bills": [
            {"title": "Unrelated Infrastructure Bill", "type": "HR", "number": "1"},
        ],
    }
    mock_resp.raise_for_status = MagicMock()

    with patch.dict("os.environ", {"CONGRESS_API_KEY": "test_key"}):
        with patch("osint_agent.tools.congress.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_cls.return_value = mock_client

            finding = await adapter.run(query="zzz_nonexistent", mode="bill")

    assert "no bills" in finding.notes.lower()


# ------------------------------------------------------------------
# Entity construction
# ------------------------------------------------------------------

def test_build_member_entity(adapter):
    member = {
        "name": "Ocasio-Cortez, Alexandria",
        "bioguideId": "O000172",
        "state": "New York",
        "partyName": "Democratic",
        "terms": {"item": [{"chamber": "House"}]},
        "url": "https://api.congress.gov/v3/member/O000172",
        "depiction": {"imageUrl": "https://example.com/aoc.jpg"},
    }
    ent = adapter._build_member_entity(member)
    assert ent.id == "person:congress:O000172"
    assert ent.label == "Ocasio-Cortez, Alexandria"
    assert ent.properties["chamber"] == "House"
    assert ent.properties["depiction_url"] == "https://example.com/aoc.jpg"


def test_build_member_entity_no_terms(adapter):
    """should handle missing or empty terms gracefully."""
    member = {
        "name": "Historical, Figure",
        "bioguideId": "H000001",
        "state": "Virginia",
        "partyName": "Federalist",
        "terms": {},
    }
    ent = adapter._build_member_entity(member)
    assert ent.id == "person:congress:H000001"
    assert "chamber" not in ent.properties  # Empty string filtered out


def test_build_bill_entity(adapter):
    bill = {
        "type": "HR",
        "number": "5678",
        "congress": 119,
        "title": "Test Bill Act of 2025",
        "originChamber": "House",
        "latestAction": {
            "text": "Passed House",
            "actionDate": "2025-03-01",
        },
    }
    ent = adapter._build_bill_entity(bill)
    assert ent.id == "document:congress:hr5678-119"
    assert ent.entity_type == EntityType.DOCUMENT
    assert ent.properties["latest_action"] == "Passed House"
    assert "house-bill" in ent.properties["url"]
