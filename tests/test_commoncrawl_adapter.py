"""Tests for the Common Crawl adapter's parsing and query logic."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from osint_agent.models import EntityType
from osint_agent.tools.commoncrawl import CommonCrawlAdapter, _format_timestamp


@pytest.fixture
def adapter():
    return CommonCrawlAdapter()


# ------------------------------------------------------------------
# Timestamp formatting
# ------------------------------------------------------------------

def test_format_timestamp_standard():
    """should format 14-digit timestamp to ISO-like string"""
    assert _format_timestamp("20231215143022") == "2023-12-15T14:30:22"


def test_format_timestamp_short():
    """should return short strings unchanged"""
    assert _format_timestamp("2023") == "2023"


def test_format_timestamp_empty():
    """should return empty string for empty input"""
    assert _format_timestamp("") == ""


def test_format_timestamp_none():
    """should return None for None input"""
    assert _format_timestamp(None) is None


# ------------------------------------------------------------------
# Adapter basics
# ------------------------------------------------------------------

def test_adapter_name(adapter):
    """should expose 'commoncrawl' as its registry name"""
    assert adapter.name == "commoncrawl"


def test_is_available(adapter):
    """should always be available (no external deps)"""
    assert adapter.is_available() is True


# ------------------------------------------------------------------
# Domain wildcard expansion
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bare_domain_gets_wildcard(adapter):
    """should expand bare domain to wildcard pattern"""
    collinfo = [{"id": "CC-MAIN-2024-10", "cdx-api": "https://index.commoncrawl.org/CC-MAIN-2024-10-index"}]
    ndjson = json.dumps({
        "url": "https://example.com/page",
        "timestamp": "20240101120000",
        "status": "200",
        "mime": "text/html",
        "digest": "abc123",
        "length": "5000",
    })

    mock_collinfo_resp = MagicMock()
    mock_collinfo_resp.status_code = 200
    mock_collinfo_resp.raise_for_status = MagicMock()
    mock_collinfo_resp.json = MagicMock(return_value=collinfo)

    mock_search_resp = MagicMock()
    mock_search_resp.status_code = 200
    mock_search_resp.raise_for_status = MagicMock()
    mock_search_resp.text = ndjson

    async def mock_get(url, **kwargs):
        if "collinfo" in url:
            return mock_collinfo_resp
        return mock_search_resp

    with patch("osint_agent.tools.commoncrawl.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.get = mock_get
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_client

        finding = await adapter.run(query="example.com")

    assert len(finding.entities) == 1
    assert finding.entities[0].entity_type == EntityType.DOCUMENT


@pytest.mark.asyncio
async def test_full_url_not_expanded(adapter):
    """should not add wildcard to full URLs"""
    collinfo = [{"id": "CC-MAIN-2024-10", "cdx-api": "https://index.commoncrawl.org/CC-MAIN-2024-10-index"}]

    mock_collinfo_resp = MagicMock()
    mock_collinfo_resp.status_code = 200
    mock_collinfo_resp.raise_for_status = MagicMock()
    mock_collinfo_resp.json = MagicMock(return_value=collinfo)

    mock_search_resp = MagicMock()
    mock_search_resp.status_code = 200
    mock_search_resp.raise_for_status = MagicMock()
    mock_search_resp.text = ""

    captured_params = {}

    async def mock_get(url, **kwargs):
        if "collinfo" in url:
            return mock_collinfo_resp
        captured_params.update(kwargs.get("params", {}))
        return mock_search_resp

    with patch("osint_agent.tools.commoncrawl.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.get = mock_get
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_client

        await adapter.run(query="https://example.com/specific")

    assert captured_params["url"] == "https://example.com/specific"


# ------------------------------------------------------------------
# Response parsing
# ------------------------------------------------------------------

SAMPLE_NDJSON = "\n".join([
    json.dumps({
        "url": "https://example.com/page1",
        "timestamp": "20240101120000",
        "status": "200",
        "mime": "text/html",
        "digest": "abc123",
        "length": "5000",
    }),
    json.dumps({
        "url": "https://example.com/page2",
        "timestamp": "20240215080000",
        "status": "200",
        "mime": "text/html",
        "digest": "def456",
        "length": "3000",
    }),
])


@pytest.mark.asyncio
async def test_parse_ndjson_creates_entities(adapter):
    """should create one DOCUMENT entity per unique URL"""
    collinfo = [{"id": "CC-MAIN-2024-10", "cdx-api": "https://index.commoncrawl.org/CC-MAIN-2024-10-index"}]

    mock_collinfo_resp = MagicMock()
    mock_collinfo_resp.status_code = 200
    mock_collinfo_resp.raise_for_status = MagicMock()
    mock_collinfo_resp.json = MagicMock(return_value=collinfo)

    mock_search_resp = MagicMock()
    mock_search_resp.status_code = 200
    mock_search_resp.raise_for_status = MagicMock()
    mock_search_resp.text = SAMPLE_NDJSON

    async def mock_get(url, **kwargs):
        if "collinfo" in url:
            return mock_collinfo_resp
        return mock_search_resp

    with patch("osint_agent.tools.commoncrawl.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.get = mock_get
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_client

        finding = await adapter.run(query="example.com")

    docs = [e for e in finding.entities if e.entity_type == EntityType.DOCUMENT]
    assert len(docs) == 2
    urls = {d.properties["url"] for d in docs}
    assert "https://example.com/page1" in urls
    assert "https://example.com/page2" in urls


@pytest.mark.asyncio
async def test_document_properties(adapter):
    """should populate timestamp, status, mime, digest fields"""
    collinfo = [{"id": "CC-MAIN-2024-10", "cdx-api": "https://index.commoncrawl.org/CC-MAIN-2024-10-index"}]

    mock_collinfo_resp = MagicMock()
    mock_collinfo_resp.status_code = 200
    mock_collinfo_resp.raise_for_status = MagicMock()
    mock_collinfo_resp.json = MagicMock(return_value=collinfo)

    mock_search_resp = MagicMock()
    mock_search_resp.status_code = 200
    mock_search_resp.raise_for_status = MagicMock()
    mock_search_resp.text = SAMPLE_NDJSON

    async def mock_get(url, **kwargs):
        if "collinfo" in url:
            return mock_collinfo_resp
        return mock_search_resp

    with patch("osint_agent.tools.commoncrawl.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.get = mock_get
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_client

        finding = await adapter.run(query="example.com")

    doc = finding.entities[0]
    assert doc.properties["timestamp"] == "2024-01-01T12:00:00"
    assert doc.properties["status_code"] == "200"
    assert doc.properties["mime_type"] == "text/html"
    assert doc.properties["source_system"] == "commoncrawl"


@pytest.mark.asyncio
async def test_deduplicates_urls(adapter):
    """should deduplicate records with the same URL"""
    collinfo = [{"id": "CC-MAIN-2024-10", "cdx-api": "https://index.commoncrawl.org/CC-MAIN-2024-10-index"}]
    # Two records with the same URL but different timestamps
    ndjson = "\n".join([
        json.dumps({
            "url": "https://example.com/same",
            "timestamp": "20240101120000",
            "status": "200",
            "mime": "text/html",
            "digest": "abc",
            "length": "100",
        }),
        json.dumps({
            "url": "https://example.com/same",
            "timestamp": "20240215080000",
            "status": "200",
            "mime": "text/html",
            "digest": "def",
            "length": "200",
        }),
    ])

    mock_collinfo_resp = MagicMock()
    mock_collinfo_resp.status_code = 200
    mock_collinfo_resp.raise_for_status = MagicMock()
    mock_collinfo_resp.json = MagicMock(return_value=collinfo)

    mock_search_resp = MagicMock()
    mock_search_resp.status_code = 200
    mock_search_resp.raise_for_status = MagicMock()
    mock_search_resp.text = ndjson

    async def mock_get(url, **kwargs):
        if "collinfo" in url:
            return mock_collinfo_resp
        return mock_search_resp

    with patch("osint_agent.tools.commoncrawl.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.get = mock_get
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_client

        finding = await adapter.run(query="example.com")

    assert len(finding.entities) == 1


# ------------------------------------------------------------------
# Error handling
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handles_403_gracefully(adapter):
    """should return empty Finding with note on 403"""
    collinfo = [{"id": "CC-MAIN-2024-10", "cdx-api": "https://index.commoncrawl.org/CC-MAIN-2024-10-index"}]

    mock_collinfo_resp = MagicMock()
    mock_collinfo_resp.status_code = 200
    mock_collinfo_resp.raise_for_status = MagicMock()
    mock_collinfo_resp.json = MagicMock(return_value=collinfo)

    mock_search_resp = MagicMock()
    mock_search_resp.status_code = 403

    async def mock_get(url, **kwargs):
        if "collinfo" in url:
            return mock_collinfo_resp
        return mock_search_resp

    with patch("osint_agent.tools.commoncrawl.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.get = mock_get
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_client

        finding = await adapter.run(query="example.com")

    assert len(finding.entities) == 0
    assert "403" in finding.notes


@pytest.mark.asyncio
async def test_handles_no_index(adapter):
    """should return empty Finding when index list is empty"""
    mock_collinfo_resp = MagicMock()
    mock_collinfo_resp.status_code = 200
    mock_collinfo_resp.raise_for_status = MagicMock()
    mock_collinfo_resp.json = MagicMock(return_value=[])

    with patch("osint_agent.tools.commoncrawl.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_collinfo_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_client

        finding = await adapter.run(query="example.com")

    assert len(finding.entities) == 0
    assert "could not determine" in finding.notes


@pytest.mark.asyncio
async def test_notes_contain_record_count(adapter):
    """should include record count and unique URL count in notes"""
    collinfo = [{"id": "CC-MAIN-2024-10", "cdx-api": "https://index.commoncrawl.org/CC-MAIN-2024-10-index"}]

    mock_collinfo_resp = MagicMock()
    mock_collinfo_resp.status_code = 200
    mock_collinfo_resp.raise_for_status = MagicMock()
    mock_collinfo_resp.json = MagicMock(return_value=collinfo)

    mock_search_resp = MagicMock()
    mock_search_resp.status_code = 200
    mock_search_resp.raise_for_status = MagicMock()
    mock_search_resp.text = SAMPLE_NDJSON

    async def mock_get(url, **kwargs):
        if "collinfo" in url:
            return mock_collinfo_resp
        return mock_search_resp

    with patch("osint_agent.tools.commoncrawl.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.get = mock_get
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_client

        finding = await adapter.run(query="example.com")

    assert "2 records found" in finding.notes
    assert "2 unique URLs" in finding.notes


# ------------------------------------------------------------------
# Registry integration
# ------------------------------------------------------------------

def test_registry_includes_commoncrawl():
    """should be registered in the tool registry"""
    from osint_agent.tools.registry import ToolRegistry
    registry = ToolRegistry()
    assert registry.get("commoncrawl") is not None


def test_input_routing_includes_commoncrawl():
    """should be routed for url and domain input types"""
    from osint_agent.tools.registry import INPUT_ROUTING
    assert "commoncrawl" in INPUT_ROUTING["url"]
    assert "commoncrawl" in INPUT_ROUTING["domain"]
