"""Tests for the DocumentCloud adapter — FOIA document search."""

import sys
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from osint_agent.tools.documentcloud import DocumentCloudAdapter
from osint_agent.models import EntityType


@pytest.fixture
def adapter():
    return DocumentCloudAdapter()


@pytest.fixture
def mock_api_response():
    return {
        "count": 2,
        "results": [
            {
                "id": "doc-001",
                "title": "FOIA Response: FBI Surveillance Records",
                "source": "FBI",
                "description": "Released under FOIA request",
                "created_at": "2024-01-15T10:00:00Z",
                "page_count": 42,
                "language": "eng",
                "canonical_url": "https://www.documentcloud.org/documents/doc-001",
                "pdf_url": "https://assets.documentcloud.org/doc-001.pdf",
                "organization": {"id": 1, "name": "MuckRock"},
                "user": {"name": "Reporter Name"},
            },
            {
                "id": "doc-002",
                "title": "DHS Memo on Border Policy",
                "source": "DHS",
                "description": None,
                "created_at": "2024-02-01T08:00:00Z",
                "page_count": 5,
                "language": "eng",
                "canonical_url": "https://www.documentcloud.org/documents/doc-002",
                "pdf_url": None,
                "organization": 42,
                "user": None,
            },
        ],
    }


# ------------------------------------------------------------------
# Availability
# ------------------------------------------------------------------

def test_is_available_when_installed():
    mock_dc = MagicMock()
    with patch.dict(sys.modules, {"documentcloud": mock_dc}):
        assert DocumentCloudAdapter().is_available() is True


def test_is_not_available_when_missing():
    with patch.dict(sys.modules, {"documentcloud": None}):
        assert DocumentCloudAdapter().is_available() is False


def test_adapter_name():
    assert DocumentCloudAdapter().name == "documentcloud"


# ------------------------------------------------------------------
# Public API response parsing
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_search_public_api(adapter, mock_api_response):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = mock_api_response
    mock_resp.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_resp)

    # httpx is imported inside _search_public_api from sys.modules
    with patch("httpx.AsyncClient", return_value=mock_client):
        finding = await adapter._search_public_api("surveillance")

    assert len(finding.entities) == 2
    assert all(e.entity_type == EntityType.DOCUMENT for e in finding.entities)

    # First document
    doc1 = finding.entities[0]
    assert doc1.label == "FOIA Response: FBI Surveillance Records"
    assert doc1.id == "document:documentcloud:doc-001"
    assert doc1.properties["source"] == "FBI"
    assert doc1.properties["page_count"] == 42
    assert doc1.properties["organization"] == "MuckRock"
    assert doc1.properties["contributor"] == "Reporter Name"
    assert doc1.properties["url"] == "https://www.documentcloud.org/documents/doc-001"

    # Second document — tests edge cases
    doc2 = finding.entities[1]
    assert doc2.properties["organization"] == "42"  # Non-dict org → str

    assert "2 of 2" in finding.notes


@pytest.mark.asyncio
async def test_search_public_api_empty(adapter):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"count": 0, "results": []}
    mock_resp.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch("httpx.AsyncClient", return_value=mock_client):
        finding = await adapter._search_public_api("nonexistent")

    assert len(finding.entities) == 0
    assert "no documents" in finding.notes.lower()


@pytest.mark.asyncio
async def test_search_public_api_http_error(adapter):
    import httpx

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(side_effect=httpx.ConnectError("timeout"))

    with patch("httpx.AsyncClient", return_value=mock_client):
        finding = await adapter._search_public_api("test")

    assert "error" in finding.notes.lower()


# ------------------------------------------------------------------
# Entity construction from API dict
# ------------------------------------------------------------------

def test_build_doc_entity_from_api_full(adapter):
    doc = {
        "id": "abc-123",
        "title": "Test Document",
        "source": "DOJ",
        "description": "A test doc",
        "created_at": "2024-01-01",
        "page_count": 10,
        "language": "eng",
        "canonical_url": "https://www.documentcloud.org/documents/abc-123",
        "pdf_url": "https://assets.documentcloud.org/abc-123.pdf",
        "organization": {"name": "ProPublica"},
        "user": {"name": "Jane Reporter"},
    }
    ent = adapter._build_doc_entity_from_api(doc)
    assert ent.id == "document:documentcloud:abc-123"
    assert ent.label == "Test Document"
    assert ent.properties["source"] == "DOJ"
    assert ent.properties["organization"] == "ProPublica"
    assert ent.properties["contributor"] == "Jane Reporter"
    assert ent.properties["document_source"] == "documentcloud"


def test_build_doc_entity_from_api_minimal(adapter):
    doc = {"id": "xyz-789"}
    ent = adapter._build_doc_entity_from_api(doc)
    assert ent.id == "document:documentcloud:xyz-789"
    assert ent.label == "Document xyz-789"
    assert ent.properties["document_source"] == "documentcloud"
