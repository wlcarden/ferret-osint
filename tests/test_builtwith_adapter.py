"""Tests for the BuiltWith adapter — website technology fingerprinting."""

import sys
import pytest
from unittest.mock import MagicMock, patch

from osint_agent.tools.builtwith_adapter import BuiltWithAdapter
from osint_agent.models import EntityType


@pytest.fixture
def adapter():
    return BuiltWithAdapter()


@pytest.fixture
def mock_builtwith_result():
    """Canned builtwith.parse() output with multiple categories."""
    return {
        "web-servers": ["Nginx"],
        "javascript-frameworks": ["jQuery", "React"],
        "analytics": ["Google Analytics", "Hotjar"],
        "cms": ["WordPress"],
        "cdn": ["Cloudflare"],
    }


# ------------------------------------------------------------------
# Availability
# ------------------------------------------------------------------

def test_is_available_when_builtwith_importable():
    with patch.dict(sys.modules, {"builtwith": MagicMock()}):
        adapter = BuiltWithAdapter()
        assert adapter.is_available() is True


def test_is_available_false_when_builtwith_missing():
    with patch.dict(sys.modules, {"builtwith": None}):
        adapter = BuiltWithAdapter()
        with patch("builtins.__import__", side_effect=ImportError):
            assert adapter.is_available() is False


def test_adapter_name(adapter):
    assert adapter.name == "builtwith"


# ------------------------------------------------------------------
# Happy path: domain with detected technologies
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_returns_domain_entity(mock_builtwith_result):
    adapter = BuiltWithAdapter()
    mock_bw = MagicMock()
    mock_bw.parse.return_value = mock_builtwith_result

    with patch.dict(sys.modules, {"builtwith": mock_bw}):
        finding = await adapter.run(domain="example.com")

    domains = [e for e in finding.entities if e.entity_type == EntityType.DOMAIN]
    assert len(domains) == 1
    assert domains[0].id == "domain:example.com"
    assert domains[0].label == "example.com"


@pytest.mark.asyncio
async def test_run_entity_properties_contain_all_categories(mock_builtwith_result):
    adapter = BuiltWithAdapter()
    mock_bw = MagicMock()
    mock_bw.parse.return_value = mock_builtwith_result

    with patch.dict(sys.modules, {"builtwith": mock_bw}):
        finding = await adapter.run(domain="example.com")

    domain = finding.entities[0]
    assert domain.properties["web-servers"] == ["Nginx"]
    assert domain.properties["javascript-frameworks"] == ["jQuery", "React"]
    assert domain.properties["analytics"] == ["Google Analytics", "Hotjar"]
    assert domain.properties["cms"] == ["WordPress"]
    assert domain.properties["cdn"] == ["Cloudflare"]


@pytest.mark.asyncio
async def test_run_notes_contain_tech_count(mock_builtwith_result):
    adapter = BuiltWithAdapter()
    mock_bw = MagicMock()
    mock_bw.parse.return_value = mock_builtwith_result

    with patch.dict(sys.modules, {"builtwith": mock_bw}):
        finding = await adapter.run(domain="example.com")

    # 1 + 2 + 2 + 1 + 1 = 7 technologies across 5 categories
    assert "7 technologies" in finding.notes
    assert "5 categories" in finding.notes
    assert "example.com" in finding.notes


@pytest.mark.asyncio
async def test_run_source_is_builtwith(mock_builtwith_result):
    adapter = BuiltWithAdapter()
    mock_bw = MagicMock()
    mock_bw.parse.return_value = mock_builtwith_result

    with patch.dict(sys.modules, {"builtwith": mock_bw}):
        finding = await adapter.run(domain="example.com")

    assert finding.entities[0].sources[0].tool == "builtwith"


# ------------------------------------------------------------------
# URL normalization
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_prepends_https_for_bare_domain(mock_builtwith_result):
    adapter = BuiltWithAdapter()
    mock_bw = MagicMock()
    mock_bw.parse.return_value = mock_builtwith_result

    with patch.dict(sys.modules, {"builtwith": mock_bw}):
        await adapter.run(domain="example.com")

    mock_bw.parse.assert_called_once_with("https://example.com")


@pytest.mark.asyncio
async def test_run_preserves_url_with_scheme(mock_builtwith_result):
    adapter = BuiltWithAdapter()
    mock_bw = MagicMock()
    mock_bw.parse.return_value = mock_builtwith_result

    with patch.dict(sys.modules, {"builtwith": mock_bw}):
        await adapter.run(domain="http://example.com")

    mock_bw.parse.assert_called_once_with("http://example.com")


@pytest.mark.asyncio
async def test_run_extracts_clean_domain_from_url(mock_builtwith_result):
    """Domain entity ID should strip protocol and path."""
    adapter = BuiltWithAdapter()
    mock_bw = MagicMock()
    mock_bw.parse.return_value = mock_builtwith_result

    with patch.dict(sys.modules, {"builtwith": mock_bw}):
        finding = await adapter.run(domain="https://sub.example.com/page")

    assert finding.entities[0].id == "domain:sub.example.com"
    assert finding.entities[0].label == "sub.example.com"


# ------------------------------------------------------------------
# Empty results
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_empty_techs_returns_notes_only():
    adapter = BuiltWithAdapter()
    mock_bw = MagicMock()
    mock_bw.parse.return_value = {}

    with patch.dict(sys.modules, {"builtwith": mock_bw}):
        finding = await adapter.run(domain="empty.com")

    assert len(finding.entities) == 0
    assert "no technologies" in finding.notes.lower()
    assert "empty.com" in finding.notes


@pytest.mark.asyncio
async def test_run_none_techs_returns_notes_only():
    adapter = BuiltWithAdapter()
    mock_bw = MagicMock()
    mock_bw.parse.return_value = None

    with patch.dict(sys.modules, {"builtwith": mock_bw}):
        finding = await adapter.run(domain="none.com")

    assert len(finding.entities) == 0
    assert "no technologies" in finding.notes.lower()


# ------------------------------------------------------------------
# Error handling
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_handles_exception():
    adapter = BuiltWithAdapter()
    mock_bw = MagicMock()
    mock_bw.parse.side_effect = ConnectionError("Failed to fetch")

    with patch.dict(sys.modules, {"builtwith": mock_bw}):
        finding = await adapter.run(domain="fail.com")

    assert len(finding.entities) == 0
    assert "error" in finding.notes.lower()
    assert "Failed to fetch" in finding.notes


@pytest.mark.asyncio
async def test_run_handles_timeout():
    adapter = BuiltWithAdapter()
    mock_bw = MagicMock()
    mock_bw.parse.side_effect = TimeoutError("Request timed out")

    with patch.dict(sys.modules, {"builtwith": mock_bw}):
        finding = await adapter.run(domain="slow.com")

    assert len(finding.entities) == 0
    assert "error" in finding.notes.lower()


# ------------------------------------------------------------------
# Single category result
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_single_category():
    adapter = BuiltWithAdapter()
    mock_bw = MagicMock()
    mock_bw.parse.return_value = {"web-servers": ["Apache"]}

    with patch.dict(sys.modules, {"builtwith": mock_bw}):
        finding = await adapter.run(domain="simple.com")

    assert len(finding.entities) == 1
    assert finding.entities[0].properties["web-servers"] == ["Apache"]
    assert "1 technologies" in finding.notes
    assert "1 categories" in finding.notes
