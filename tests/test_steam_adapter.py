"""Tests for the Steam Community adapter."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from osint_agent.tools.steam import SteamAdapter, _xml_text
from osint_agent.models import EntityType, RelationType


@pytest.fixture
def adapter():
    return SteamAdapter(timeout=10)


SAMPLE_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<profile>
    <steamID64>76561197975428147</steamID64>
    <steamID><![CDATA[TestPlayer]]></steamID>
    <onlineState>offline</onlineState>
    <privacyState>public</privacyState>
    <visibilityState>3</visibilityState>
    <avatarFull><![CDATA[https://avatars.steamstatic.com/abc_full.jpg]]></avatarFull>
    <vacBanned>0</vacBanned>
    <realname><![CDATA[John Doe]]></realname>
    <location><![CDATA[Portland, Oregon]]></location>
    <memberSince>April 13, 2005</memberSince>
    <summary><![CDATA[No information given.]]></summary>
    <customURL><![CDATA[testplayer]]></customURL>
</profile>"""

ERROR_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<response>
    <error>The specified profile could not be found.</error>
</response>"""

MINIMAL_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<profile>
    <steamID64>76561198000000000</steamID64>
    <steamID><![CDATA[AnonPlayer]]></steamID>
    <onlineState>online</onlineState>
    <privacyState>public</privacyState>
    <vacBanned>0</vacBanned>
    <memberSince>January 1, 2020</memberSince>
    <customURL><![CDATA[anonplayer]]></customURL>
</profile>"""

VAC_BANNED_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<profile>
    <steamID64>76561198000000001</steamID64>
    <steamID><![CDATA[BannedPlayer]]></steamID>
    <privacyState>public</privacyState>
    <vacBanned>1</vacBanned>
    <memberSince>March 5, 2018</memberSince>
    <customURL><![CDATA[bannedplayer]]></customURL>
</profile>"""


# ------------------------------------------------------------------
# Availability
# ------------------------------------------------------------------

def test_is_available(adapter):
    """should always be available (only needs httpx)"""
    assert adapter.is_available() is True


# ------------------------------------------------------------------
# XML parsing
# ------------------------------------------------------------------

def test_xml_text_helper():
    """should safely extract text from XML elements"""
    import xml.etree.ElementTree as ET
    root = ET.fromstring("<r><name>Alice</name><empty/></r>")
    assert _xml_text(root, "name") == "Alice"
    assert _xml_text(root, "empty") == ""
    assert _xml_text(root, "missing") == ""
    assert _xml_text(root, "missing", "default") == "default"


def test_parse_full_profile(adapter):
    """should extract all fields from a complete profile"""
    finding = adapter._parse_xml(SAMPLE_XML, "testplayer", "https://steamcommunity.com/id/testplayer")

    # Account entity
    accounts = [e for e in finding.entities if e.entity_type == EntityType.ACCOUNT]
    assert len(accounts) == 1
    acct = accounts[0]
    assert acct.id == "account:steam:testplayer"
    assert acct.properties["steam_id64"] == "76561197975428147"
    assert acct.properties["persona_name"] == "TestPlayer"
    assert acct.properties["real_name"] == "John Doe"
    assert acct.properties["location"] == "Portland, Oregon"
    assert acct.properties["member_since"] == "April 13, 2005"
    assert "bio" not in acct.properties  # "No information given." is filtered

    # Person entity (because real_name is set)
    persons = [e for e in finding.entities if e.entity_type == EntityType.PERSON]
    assert len(persons) == 1
    assert persons[0].label == "John Doe"
    assert persons[0].properties.get("location") == "Portland, Oregon"

    # Relationship: person → account
    assert len(finding.relationships) == 1
    assert finding.relationships[0].relation_type == RelationType.HAS_ACCOUNT

    # Notes
    assert "John Doe" in finding.notes
    assert "Portland, Oregon" in finding.notes


def test_parse_minimal_profile(adapter):
    """should handle profile without real name or location"""
    finding = adapter._parse_xml(MINIMAL_XML, "anonplayer", "https://steamcommunity.com/id/anonplayer")

    accounts = [e for e in finding.entities if e.entity_type == EntityType.ACCOUNT]
    assert len(accounts) == 1
    assert accounts[0].properties["persona_name"] == "AnonPlayer"

    # No person entity without real_name
    persons = [e for e in finding.entities if e.entity_type == EntityType.PERSON]
    assert len(persons) == 0
    assert len(finding.relationships) == 0


def test_parse_vac_banned(adapter):
    """should flag VAC-banned accounts"""
    finding = adapter._parse_xml(VAC_BANNED_XML, "bannedplayer", "https://steamcommunity.com/id/bannedplayer")

    accounts = [e for e in finding.entities if e.entity_type == EntityType.ACCOUNT]
    assert accounts[0].properties.get("vac_banned") is True
    assert "VAC BANNED" in finding.notes


def test_parse_error_response(adapter):
    """should handle Steam error XML"""
    finding = adapter._parse_xml(ERROR_XML, "nobody", "https://steamcommunity.com/id/nobody")
    assert "could not be found" in finding.notes


def test_parse_invalid_xml(adapter):
    """should handle malformed XML"""
    finding = adapter._parse_xml("not xml at all", "test", "http://test")
    assert "parse error" in finding.notes.lower()


# ------------------------------------------------------------------
# HTTP integration (mocked)
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_not_found(adapter):
    """should return notes for 404/500"""
    mock_resp = MagicMock()
    mock_resp.status_code = 500

    with patch("osint_agent.tools.steam.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        finding = await adapter.run("nonexistent_user")
        assert "no profile" in finding.notes.lower()


@pytest.mark.asyncio
async def test_run_success(adapter):
    """should parse a successful response"""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = SAMPLE_XML

    with patch("osint_agent.tools.steam.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        finding = await adapter.run("testplayer")
        accounts = [e for e in finding.entities if e.entity_type == EntityType.ACCOUNT]
        assert len(accounts) == 1
        assert accounts[0].properties["persona_name"] == "TestPlayer"


# ------------------------------------------------------------------
# Registry
# ------------------------------------------------------------------

def test_registered_in_registry():
    """should be registered and routable by username input type"""
    from osint_agent.tools.registry import ToolRegistry, INPUT_ROUTING

    assert "steam" in INPUT_ROUTING["username"]
    registry = ToolRegistry()
    adapter = registry.get("steam")
    assert adapter is not None
    assert adapter.is_available()
