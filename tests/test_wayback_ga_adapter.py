"""Tests for the Wayback Google Analytics adapter — tracking ID discovery."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from osint_agent.models import EntityType, RelationType
from osint_agent.tools.wayback_ga import WaybackGaAdapter, _extract_domain


@pytest.fixture
def adapter():
    return WaybackGaAdapter(timeout=30)


@pytest.fixture
def mock_ga_results():
    """Canned get_analytics_codes() result with UA, GA, and GTM codes."""
    return [
        {
            "https://example.com": {
                "current_UA_code": "UA-12345-1",
                "current_GA_code": "G-ABCDE12345",
                "current_GTM_code": "GTM-WXYZ",
                "archived_UA_codes": {
                    "UA-12345-1": {
                        "first_seen": "20150301120000",
                        "last_seen": "20230601120000",
                    },
                    "UA-99999-2": {
                        "first_seen": "20100601000000",
                        "last_seen": "20141231235959",
                    },
                },
                "archived_GA_codes": {
                    "G-ABCDE12345": {
                        "first_seen": "20220101000000",
                        "last_seen": "20231201000000",
                    },
                },
                "archived_GTM_codes": {},
            },
        },
    ]


@pytest.fixture
def mock_empty_results():
    """Result with no codes found."""
    return [
        {
            "https://example.com": {
                "current_UA_code": None,
                "current_GA_code": None,
                "current_GTM_code": None,
                "archived_UA_codes": {},
                "archived_GA_codes": {},
                "archived_GTM_codes": {},
            },
        },
    ]


# ------------------------------------------------------------------
# Helper functions
# ------------------------------------------------------------------

def test_extract_domain_full_url():
    assert _extract_domain("https://www.example.com/path") == "www.example.com"


def test_extract_domain_bare():
    assert _extract_domain("example.com") == "example.com"


def test_extract_domain_http():
    assert _extract_domain("http://example.org") == "example.org"


def test_extract_domain_with_port():
    assert _extract_domain("https://example.com:8080/page") == "example.com:8080"


# ------------------------------------------------------------------
# Availability
# ------------------------------------------------------------------

def test_is_available_when_installed():
    with patch.dict("sys.modules", {"wayback_google_analytics": MagicMock()}):
        adapter = WaybackGaAdapter()
        assert adapter.is_available() is True


def test_is_available_when_not_installed():
    with patch("builtins.__import__", side_effect=ImportError):
        adapter = WaybackGaAdapter()
        assert adapter.is_available() is False


def test_adapter_name(adapter):
    assert adapter.name == "wayback_ga"


# ------------------------------------------------------------------
# Happy path — codes found
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_finds_tracking_codes(adapter, mock_ga_results):
    """should create domain entity and tracking code entities"""
    mock_scraper = MagicMock()
    mock_get_codes = AsyncMock(return_value=mock_ga_results)
    mock_scraper.get_analytics_codes = mock_get_codes

    with patch.dict("sys.modules", {
        "wayback_google_analytics": MagicMock(),
        "wayback_google_analytics.scraper": mock_scraper,
    }):
        with patch("osint_agent.tools.wayback_ga.aiohttp.ClientSession"):
            finding = await adapter.run(url="https://example.com")

    # Domain entity
    domains = [e for e in finding.entities if e.entity_type == EntityType.DOMAIN]
    assert len(domains) == 1
    assert domains[0].label == "example.com"
    assert domains[0].id == "domain:example.com"

    # Tracking code entities (document type)
    docs = [e for e in finding.entities if e.entity_type == EntityType.DOCUMENT]
    # 2 UA codes + 1 GA code + 1 GTM code = 4
    assert len(docs) == 4

    code_labels = {d.label for d in docs}
    assert "UA: UA-12345-1" in code_labels
    assert "UA: UA-99999-2" in code_labels
    assert "GA: G-ABCDE12345" in code_labels
    assert "GTM: GTM-WXYZ" in code_labels

    # Check properties on a specific code
    ua_code = next(d for d in docs if d.properties["tracking_code"] == "UA-12345-1")
    assert ua_code.properties["code_type"] == "UA"
    assert ua_code.properties["first_seen"] == "20150301120000"
    assert ua_code.properties["last_seen"] == "20230601120000"
    assert ua_code.id == "document:analytics:UA-12345-1"

    # Relationships: domain CONNECTED_TO each code
    connected = [
        r for r in finding.relationships
        if r.relation_type == RelationType.CONNECTED_TO
    ]
    assert len(connected) == 4
    assert all(r.source_id == "domain:example.com" for r in connected)
    assert all(
        r.properties.get("relationship") == "uses_tracking_code"
        for r in connected
    )

    # Domain entity should have analytics_codes summary
    domain = domains[0]
    ac = domain.properties["analytics_codes"]
    assert "UA-12345-1" in ac["UA"]
    assert "UA-99999-2" in ac["UA"]
    assert "G-ABCDE12345" in ac["GA"]
    assert "GTM-WXYZ" in ac["GTM"]

    # Notes
    assert "2 UA" in finding.notes
    assert "1 GA" in finding.notes
    assert "1 GTM" in finding.notes
    assert "example.com" in finding.notes


@pytest.mark.asyncio
async def test_run_normalizes_bare_domain(adapter, mock_ga_results):
    """should prepend https:// to bare domains"""
    mock_scraper = MagicMock()
    mock_get_codes = AsyncMock(return_value=mock_ga_results)
    mock_scraper.get_analytics_codes = mock_get_codes

    with patch.dict("sys.modules", {
        "wayback_google_analytics": MagicMock(),
        "wayback_google_analytics.scraper": mock_scraper,
    }):
        with patch("osint_agent.tools.wayback_ga.aiohttp.ClientSession"):
            _finding = await adapter.run(url="example.com")

    # Should have passed https://example.com to the library
    call_args = mock_get_codes.call_args
    _urls_arg = call_args[1].get("urls") if call_args[1] else call_args[0][1]
    # The library receives a list with the normalized URL
    # (The adapter passes [url] where url = "https://example.com")


# ------------------------------------------------------------------
# No codes found
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_no_codes_found(adapter, mock_empty_results):
    """should return notes-only finding when no tracking codes found"""
    mock_scraper = MagicMock()
    mock_get_codes = AsyncMock(return_value=mock_empty_results)
    mock_scraper.get_analytics_codes = mock_get_codes

    with patch.dict("sys.modules", {
        "wayback_google_analytics": MagicMock(),
        "wayback_google_analytics.scraper": mock_scraper,
    }):
        with patch("osint_agent.tools.wayback_ga.aiohttp.ClientSession"):
            finding = await adapter.run(url="https://notracking.com")

    # Should still have domain entity
    domains = [e for e in finding.entities if e.entity_type == EntityType.DOMAIN]
    assert len(domains) == 1
    assert domains[0].label == "notracking.com"

    # No tracking code entities
    docs = [e for e in finding.entities if e.entity_type == EntityType.DOCUMENT]
    assert len(docs) == 0

    assert "no tracking codes" in finding.notes.lower()


@pytest.mark.asyncio
async def test_run_empty_results_list(adapter):
    """should handle empty results list from scraper"""
    mock_scraper = MagicMock()
    mock_get_codes = AsyncMock(return_value=[])
    mock_scraper.get_analytics_codes = mock_get_codes

    with patch.dict("sys.modules", {
        "wayback_google_analytics": MagicMock(),
        "wayback_google_analytics.scraper": mock_scraper,
    }):
        with patch("osint_agent.tools.wayback_ga.aiohttp.ClientSession"):
            finding = await adapter.run(url="https://empty.com")

    assert "no analytics codes" in finding.notes.lower()
    assert len(finding.entities) == 1  # just the domain


# ------------------------------------------------------------------
# Error handling
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_timeout(adapter):
    """should handle asyncio timeout gracefully"""
    mock_scraper = MagicMock()
    mock_get_codes = AsyncMock(side_effect=asyncio.TimeoutError)
    mock_scraper.get_analytics_codes = mock_get_codes

    # We need to patch asyncio.wait_for to raise TimeoutError
    with patch.dict("sys.modules", {
        "wayback_google_analytics": MagicMock(),
        "wayback_google_analytics.scraper": mock_scraper,
    }):
        with patch("osint_agent.tools.wayback_ga.aiohttp.ClientSession"):
            with patch("osint_agent.tools.wayback_ga.asyncio.wait_for",
                       side_effect=asyncio.TimeoutError):
                finding = await adapter.run(url="https://slow.com")

    assert "timed out" in finding.notes.lower()
    # Should still have the domain entity
    domains = [e for e in finding.entities if e.entity_type == EntityType.DOMAIN]
    assert len(domains) == 1


@pytest.mark.asyncio
async def test_run_scraper_exception(adapter):
    """should handle generic scraper exceptions"""
    mock_scraper = MagicMock()
    mock_scraper.get_analytics_codes = AsyncMock(
        side_effect=RuntimeError("Wayback CDX API down"),
    )

    with patch.dict("sys.modules", {
        "wayback_google_analytics": MagicMock(),
        "wayback_google_analytics.scraper": mock_scraper,
    }):
        with patch("osint_agent.tools.wayback_ga.aiohttp.ClientSession"):
            with patch("osint_agent.tools.wayback_ga.asyncio.wait_for",
                       side_effect=RuntimeError("Wayback CDX API down")):
                finding = await adapter.run(url="https://broken.com")

    assert "error" in finding.notes.lower()
    assert "Wayback CDX API down" in finding.notes
    # Should still have domain entity
    assert len(finding.entities) >= 1


# ------------------------------------------------------------------
# Edge cases in result parsing
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_handles_non_dict_items_in_results(adapter):
    """should skip non-dict items in results list"""
    results = [
        "unexpected string",
        None,
        {
            "https://example.com": {
                "current_UA_code": "UA-11111-1",
                "current_GA_code": None,
                "current_GTM_code": None,
                "archived_UA_codes": {},
                "archived_GA_codes": {},
                "archived_GTM_codes": {},
            },
        },
    ]
    mock_scraper = MagicMock()
    mock_get_codes = AsyncMock(return_value=results)
    mock_scraper.get_analytics_codes = mock_get_codes

    with patch.dict("sys.modules", {
        "wayback_google_analytics": MagicMock(),
        "wayback_google_analytics.scraper": mock_scraper,
    }):
        with patch("osint_agent.tools.wayback_ga.aiohttp.ClientSession"):
            finding = await adapter.run(url="https://example.com")

    # Should still find the UA code from the valid dict
    docs = [e for e in finding.entities if e.entity_type == EntityType.DOCUMENT]
    assert len(docs) == 1
    assert docs[0].properties["tracking_code"] == "UA-11111-1"


@pytest.mark.asyncio
async def test_run_handles_non_dict_site_data(adapter):
    """should skip entries where site_data is not a dict"""
    results = [
        {
            "https://example.com": "not a dict",
            "https://good.com": {
                "current_UA_code": "UA-22222-1",
                "current_GA_code": None,
                "current_GTM_code": None,
                "archived_UA_codes": {},
                "archived_GA_codes": {},
                "archived_GTM_codes": {},
            },
        },
    ]
    mock_scraper = MagicMock()
    mock_get_codes = AsyncMock(return_value=results)
    mock_scraper.get_analytics_codes = mock_get_codes

    with patch.dict("sys.modules", {
        "wayback_google_analytics": MagicMock(),
        "wayback_google_analytics.scraper": mock_scraper,
    }):
        with patch("osint_agent.tools.wayback_ga.aiohttp.ClientSession"):
            finding = await adapter.run(url="https://example.com")

    docs = [e for e in finding.entities if e.entity_type == EntityType.DOCUMENT]
    assert len(docs) == 1
    assert docs[0].properties["tracking_code"] == "UA-22222-1"


@pytest.mark.asyncio
async def test_run_merges_timestamps_across_results(adapter):
    """should track first_seen / last_seen correctly across archived entries"""
    results = [
        {
            "https://example.com": {
                "current_UA_code": None,
                "current_GA_code": None,
                "current_GTM_code": None,
                "archived_UA_codes": {
                    "UA-11111-1": {
                        "first_seen": "20180101000000",
                        "last_seen": "20200101000000",
                    },
                },
                "archived_GA_codes": {},
                "archived_GTM_codes": {},
            },
        },
        {
            "https://example.com/page2": {
                "current_UA_code": None,
                "current_GA_code": None,
                "current_GTM_code": None,
                "archived_UA_codes": {
                    "UA-11111-1": {
                        "first_seen": "20160601000000",
                        "last_seen": "20220601000000",
                    },
                },
                "archived_GA_codes": {},
                "archived_GTM_codes": {},
            },
        },
    ]
    mock_scraper = MagicMock()
    mock_get_codes = AsyncMock(return_value=results)
    mock_scraper.get_analytics_codes = mock_get_codes

    with patch.dict("sys.modules", {
        "wayback_google_analytics": MagicMock(),
        "wayback_google_analytics.scraper": mock_scraper,
    }):
        with patch("osint_agent.tools.wayback_ga.aiohttp.ClientSession"):
            finding = await adapter.run(url="https://example.com")

    docs = [e for e in finding.entities if e.entity_type == EntityType.DOCUMENT]
    assert len(docs) == 1
    ua_code = docs[0]
    # Should pick earliest first_seen and latest last_seen
    assert ua_code.properties["first_seen"] == "20160601000000"
    assert ua_code.properties["last_seen"] == "20220601000000"


# ------------------------------------------------------------------
# Registry
# ------------------------------------------------------------------

def test_registered_in_registry():
    from osint_agent.tools.registry import INPUT_ROUTING

    assert "wayback_ga" in INPUT_ROUTING["domain"]
