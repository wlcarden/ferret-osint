"""Tests for the DuckDuckGo search adapter's parsing logic."""

from unittest.mock import MagicMock, patch

import pytest

from osint_agent.models import EntityType
from osint_agent.tools.ddg_search import DdgSearchAdapter, _url_hash


def _sample_text_results():
    """Minimal DuckDuckGo text search results matching real format."""
    return [
        {
            "title": "Example Page One",
            "href": "https://example.com/page1",
            "body": "This is the first result body text.",
        },
        {
            "title": "Example Page Two",
            "href": "https://example.com/page2",
            "body": "Second result with more detail about the query.",
        },
    ]


def _sample_news_results():
    """Minimal DuckDuckGo news search results matching real format."""
    return [
        {
            "title": "Breaking News Story",
            "url": "https://news.example.com/story1",
            "body": "A significant event has occurred.",
            "date": "2025-01-15T10:30:00",
            "source": "Example News",
        },
        {
            "title": "Another News Article",
            "url": "https://news.example.com/story2",
            "body": "Follow-up coverage on the event.",
            "date": "2025-01-16T08:00:00",
            "source": "Other Outlet",
        },
    ]


def test_adapter_name():
    adapter = DdgSearchAdapter()
    assert adapter.name == "ddg_search"


def test_is_available_when_installed():
    adapter = DdgSearchAdapter()
    with patch.dict("sys.modules", {"duckduckgo_search": MagicMock()}):
        assert adapter.is_available() is True


def test_is_available_when_not_installed():
    adapter = DdgSearchAdapter()
    with patch.dict("sys.modules", {"ddgs": None, "duckduckgo_search": None}):
        assert adapter.is_available() is False


def test_parse_text_creates_document_entities():
    adapter = DdgSearchAdapter()
    results = _sample_text_results()
    finding = adapter._parse_text("test query", results)

    assert len(finding.entities) == 2
    for entity in finding.entities:
        assert entity.entity_type == EntityType.DOCUMENT


def test_parse_text_entity_ids_use_url_hash():
    adapter = DdgSearchAdapter()
    results = _sample_text_results()
    finding = adapter._parse_text("test query", results)

    expected_hash = _url_hash("https://example.com/page1")
    assert finding.entities[0].id == f"document:ddg:{expected_hash}"


def test_parse_text_entity_labels():
    adapter = DdgSearchAdapter()
    results = _sample_text_results()
    finding = adapter._parse_text("test query", results)

    assert finding.entities[0].label == "Example Page One"
    assert finding.entities[1].label == "Example Page Two"


def test_parse_text_entity_properties():
    adapter = DdgSearchAdapter()
    results = _sample_text_results()
    finding = adapter._parse_text("test query", results)

    props = finding.entities[0].properties
    assert props["url"] == "https://example.com/page1"
    assert props["snippet"] == "This is the first result body text."
    assert props["search_query"] == "test query"
    assert props["result_type"] == "web"


def test_parse_text_truncates_snippet():
    adapter = DdgSearchAdapter()
    long_body = "x" * 1000
    results = [{"title": "Long", "href": "https://example.com/long", "body": long_body}]
    finding = adapter._parse_text("q", results)

    assert len(finding.entities[0].properties["snippet"]) == 500


def test_parse_text_notes():
    adapter = DdgSearchAdapter()
    results = _sample_text_results()
    finding = adapter._parse_text("test query", results)

    assert finding.notes == "DuckDuckGo text search for 'test query': 2 results"


def test_parse_news_creates_document_entities():
    adapter = DdgSearchAdapter()
    results = _sample_news_results()
    finding = adapter._parse_news("event", results)

    assert len(finding.entities) == 2
    for entity in finding.entities:
        assert entity.entity_type == EntityType.DOCUMENT


def test_parse_news_entity_properties():
    adapter = DdgSearchAdapter()
    results = _sample_news_results()
    finding = adapter._parse_news("event", results)

    props = finding.entities[0].properties
    assert props["url"] == "https://news.example.com/story1"
    assert props["result_type"] == "news"
    assert props["date"] == "2025-01-15T10:30:00"
    assert props["source"] == "Example News"
    assert props["search_query"] == "event"


def test_parse_news_notes():
    adapter = DdgSearchAdapter()
    results = _sample_news_results()
    finding = adapter._parse_news("event", results)

    assert finding.notes == "DuckDuckGo news search for 'event': 2 results"


def test_parse_empty_results():
    adapter = DdgSearchAdapter()
    finding = adapter._parse_text("nothing", [])

    assert len(finding.entities) == 0
    assert finding.notes == "DuckDuckGo text search for 'nothing': 0 results"


def test_all_entities_have_sources():
    adapter = DdgSearchAdapter()
    finding = adapter._parse_text("test", _sample_text_results())

    for entity in finding.entities:
        assert len(entity.sources) >= 1
        assert entity.sources[0].tool == "ddg_search"


def test_all_news_entities_have_source_urls():
    adapter = DdgSearchAdapter()
    finding = adapter._parse_news("test", _sample_news_results())

    for entity in finding.entities:
        assert entity.sources[0].source_url is not None
        assert entity.sources[0].source_url.startswith("https://")


def test_url_hash_deterministic():
    url = "https://example.com/page"
    assert _url_hash(url) == _url_hash(url)
    assert len(_url_hash(url)) == 12


def test_url_hash_different_urls():
    assert _url_hash("https://a.com") != _url_hash("https://b.com")


@pytest.mark.asyncio
async def test_run_text_mode():
    """run() delegates to DDGS().text() in text mode."""
    adapter = DdgSearchAdapter()
    mock_ddgs_instance = MagicMock()
    mock_ddgs_instance.text.return_value = _sample_text_results()

    with patch("ddgs.DDGS", return_value=mock_ddgs_instance):
        finding = await adapter.run(query="test", mode="text", max_results=10)

    mock_ddgs_instance.text.assert_called_once_with("test", max_results=10)
    assert len(finding.entities) == 2
    assert "text search" in finding.notes


@pytest.mark.asyncio
async def test_run_news_mode():
    """run() delegates to DDGS().news() in news mode."""
    adapter = DdgSearchAdapter()
    mock_ddgs_instance = MagicMock()
    mock_ddgs_instance.news.return_value = _sample_news_results()

    with patch("ddgs.DDGS", return_value=mock_ddgs_instance):
        finding = await adapter.run(query="breaking", mode="news", max_results=5)

    mock_ddgs_instance.news.assert_called_once_with("breaking", max_results=5)
    assert len(finding.entities) == 2
    assert "news search" in finding.notes
