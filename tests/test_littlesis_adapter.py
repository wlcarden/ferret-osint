"""Tests for the LittleSis adapter — power network mapping."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from osint_agent.tools.littlesis import LittleSisAdapter, _REL_CATEGORIES, _TYPE_MAP
from osint_agent.models import EntityType, RelationType


@pytest.fixture
def adapter():
    return LittleSisAdapter(timeout=10)


@pytest.fixture
def mock_search_response():
    """Canned entity search response."""
    return {
        "data": [
            {
                "id": "12345",
                "attributes": {
                    "name": "Koch Industries",
                    "primary_ext": "Org",
                    "blurb": "Multinational conglomerate",
                    "website": "https://kochind.com",
                    "types": ["Business", "PrivateCompany"],
                    "aliases": ["Koch", "Koch Industries Inc"],
                },
            },
        ],
    }


@pytest.fixture
def mock_relationships_response():
    return {
        "data": [
            {
                "attributes": {
                    "category_id": 1,  # position
                    "entity1_id": "12345",
                    "entity2_id": "67890",
                    "description1": "CEO",
                    "description2": "",
                    "amount": None,
                    "start_date": "2000-01-01",
                    "end_date": None,
                },
            },
            {
                "attributes": {
                    "category_id": 5,  # donation
                    "entity1_id": "12345",
                    "entity2_id": "11111",
                    "description1": "",
                    "description2": "",
                    "amount": 50000,
                    "start_date": "2023-01-01",
                    "end_date": "2023-12-31",
                },
            },
        ],
    }


# ------------------------------------------------------------------
# Availability and metadata
# ------------------------------------------------------------------

def test_is_available(adapter):
    assert adapter.is_available() is True


def test_adapter_name(adapter):
    assert adapter.name == "littlesis"


# ------------------------------------------------------------------
# Type and relationship mapping
# ------------------------------------------------------------------

def test_type_map_person():
    assert _TYPE_MAP["Person"] == EntityType.PERSON


def test_type_map_org():
    assert _TYPE_MAP["Org"] == EntityType.ORGANIZATION


def test_rel_categories_position():
    label, rel_type = _REL_CATEGORIES[1]
    assert label == "position"
    assert rel_type == RelationType.WORKS_AT


def test_rel_categories_donation():
    label, rel_type = _REL_CATEGORIES[5]
    assert label == "donation"
    assert rel_type == RelationType.DONATED_TO


def test_rel_categories_ownership():
    label, rel_type = _REL_CATEGORIES[10]
    assert label == "ownership"
    assert rel_type == RelationType.OWNS


# ------------------------------------------------------------------
# Entity construction
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_builds_entities(adapter, mock_search_response, mock_relationships_response):
    search_resp = MagicMock()
    search_resp.status_code = 200
    search_resp.json.return_value = mock_search_response
    search_resp.raise_for_status = MagicMock()

    rel_resp = MagicMock()
    rel_resp.status_code = 200
    rel_resp.json.return_value = mock_relationships_response
    rel_resp.raise_for_status = MagicMock()

    with patch("osint_agent.tools.littlesis.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        # First call = search, second call = relationships
        mock_client.get = AsyncMock(side_effect=[search_resp, rel_resp])
        mock_cls.return_value = mock_client

        finding = await adapter.run(name="Koch Industries")

    # Should have the main entity + related entities
    assert len(finding.entities) >= 1
    main = finding.entities[0]
    assert main.label == "Koch Industries"
    assert main.entity_type == EntityType.ORGANIZATION
    assert main.properties["littlesis_id"] == "12345"
    assert main.properties["blurb"] == "Multinational conglomerate"
    assert "Koch Industries Inc" in main.properties["aliases"]

    # Should have relationships
    assert len(finding.relationships) == 2


@pytest.mark.asyncio
async def test_run_no_results(adapter):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"data": []}
    mock_resp.raise_for_status = MagicMock()

    with patch("osint_agent.tools.littlesis.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_cls.return_value = mock_client

        finding = await adapter.run(name="Nonexistent Corp")

    assert len(finding.entities) == 0
    assert "no results" in finding.notes.lower()


# ------------------------------------------------------------------
# Error handling
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_503_rate_limit(adapter):
    """should return graceful message on 503."""
    import httpx

    mock_resp = MagicMock()
    mock_resp.status_code = 503
    mock_resp.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError(
            "503", request=MagicMock(), response=mock_resp,
        ),
    )

    with patch("osint_agent.tools.littlesis.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_cls.return_value = mock_client

        finding = await adapter.run(name="Test")

    assert "rate limit" in finding.notes.lower()


@pytest.mark.asyncio
async def test_run_relationship_fetch_fails_gracefully(
    adapter, mock_search_response,
):
    """should still return entities when relationship fetch errors."""
    search_resp = MagicMock()
    search_resp.status_code = 200
    search_resp.json.return_value = mock_search_response
    search_resp.raise_for_status = MagicMock()

    import httpx
    error_resp = MagicMock()
    error_resp.status_code = 500
    error_resp.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError(
            "500", request=MagicMock(), response=error_resp,
        ),
    )

    with patch("osint_agent.tools.littlesis.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(side_effect=[search_resp, error_resp])
        mock_cls.return_value = mock_client

        finding = await adapter.run(name="Koch Industries")

    # Should still have the main entity even though relationships failed
    assert len(finding.entities) >= 1
    assert finding.entities[0].label == "Koch Industries"
    assert len(finding.relationships) == 0


# ------------------------------------------------------------------
# Entity ID format
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_entity_id_format(adapter, mock_search_response, mock_relationships_response):
    search_resp = MagicMock()
    search_resp.json.return_value = mock_search_response
    search_resp.raise_for_status = MagicMock()
    rel_resp = MagicMock()
    rel_resp.json.return_value = mock_relationships_response
    rel_resp.raise_for_status = MagicMock()

    with patch("osint_agent.tools.littlesis.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(side_effect=[search_resp, rel_resp])
        mock_cls.return_value = mock_client

        finding = await adapter.run(name="Koch Industries")

    assert finding.entities[0].id == "organization:littlesis:12345"
