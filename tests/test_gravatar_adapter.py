"""Tests for the Gravatar adapter — email to identity bridge."""

import hashlib
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from osint_agent.tools.gravatar import (
    GravatarAdapter,
    _identify_platform,
    _extract_username_from_url,
    _slugify,
)
from osint_agent.models import EntityType, RelationType


@pytest.fixture
def adapter():
    return GravatarAdapter(timeout=10)


@pytest.fixture
def mock_gravatar_response():
    """Canned Gravatar profile response."""
    return {
        "entry": [
            {
                "id": "12345",
                "hash": "abc123",
                "profileUrl": "https://gravatar.com/testuser",
                "preferredUsername": "testuser",
                "displayName": "Test User",
                "name": {
                    "givenName": "Test",
                    "familyName": "User",
                },
                "currentLocation": "Portland, OR",
                "aboutMe": "Software developer and OSINT enthusiast",
                "urls": [
                    {
                        "value": "https://github.com/testuser",
                        "title": "GitHub",
                    },
                    {
                        "value": "https://twitter.com/testuser42",
                        "title": "Twitter",
                    },
                    {
                        "value": "https://testuser.dev",
                        "title": "Personal Site",
                    },
                ],
                "photos": [
                    {
                        "value": "https://gravatar.com/avatar/abc123",
                        "type": "thumbnail",
                    },
                ],
            },
        ],
    }


# ------------------------------------------------------------------
# Availability
# ------------------------------------------------------------------

def test_is_available(adapter):
    """should always be available (only needs httpx)"""
    assert adapter.is_available() is True


# ------------------------------------------------------------------
# Helper functions
# ------------------------------------------------------------------

def test_identify_platform_github():
    assert _identify_platform("https://github.com/user") == "GitHub"


def test_identify_platform_twitter():
    assert _identify_platform("https://twitter.com/user") == "Twitter"
    assert _identify_platform("https://x.com/user") == "Twitter"


def test_identify_platform_reddit():
    assert _identify_platform("https://reddit.com/user/foo") == "Reddit"


def test_identify_platform_unknown():
    assert _identify_platform("https://example.com/profile") == ""


def test_extract_username_github():
    assert _extract_username_from_url("https://github.com/testuser") == "testuser"


def test_extract_username_twitter():
    assert _extract_username_from_url("https://twitter.com/testuser42") == "testuser42"


def test_extract_username_filters_paths():
    """should not extract 'about' or 'help' as usernames"""
    assert _extract_username_from_url("https://github.com/about") == ""


def test_extract_username_no_match():
    assert _extract_username_from_url("https://example.com/profile") == ""


def test_slugify():
    slug = _slugify("https://github.com/testuser")
    assert "github_com_testuser" in slug
    assert len(slug) <= 80


def test_slugify_long_url():
    """should truncate very long URLs"""
    slug = _slugify("https://example.com/" + "a" * 200)
    assert len(slug) <= 80


# ------------------------------------------------------------------
# Finding construction
# ------------------------------------------------------------------

def test_build_finding(adapter, mock_gravatar_response):
    """should build complete finding from Gravatar profile"""
    email = "test@example.com"
    email_hash = hashlib.md5(email.encode()).hexdigest()
    finding = adapter._build_finding(email, email_hash, mock_gravatar_response)

    # Should have person entity
    persons = [e for e in finding.entities if e.entity_type == EntityType.PERSON]
    assert len(persons) == 1
    assert persons[0].label == "Test User"
    assert persons[0].properties["location"] == "Portland, OR"

    # Should have email entity
    emails = [e for e in finding.entities if e.entity_type == EntityType.EMAIL]
    assert len(emails) == 1
    assert emails[0].label == "test@example.com"

    # Should have username entity
    usernames = [e for e in finding.entities if e.entity_type == EntityType.USERNAME]
    assert len(usernames) == 1
    assert usernames[0].label == "testuser"

    # Should have account entities for linked URLs
    accounts = [e for e in finding.entities if e.entity_type == EntityType.ACCOUNT]
    assert len(accounts) == 3  # GitHub, Twitter, personal site
    platforms = {a.properties.get("platform") for a in accounts}
    assert "GitHub" in platforms
    assert "Twitter" in platforms

    # Should have HAS_EMAIL, HAS_USERNAME, HAS_ACCOUNT relationships
    rel_types = {r.relation_type for r in finding.relationships}
    assert RelationType.HAS_EMAIL in rel_types
    assert RelationType.HAS_USERNAME in rel_types
    assert RelationType.HAS_ACCOUNT in rel_types


def test_build_finding_minimal(adapter):
    """should handle profile with only display name"""
    data = {
        "entry": [
            {
                "profileUrl": "https://gravatar.com/abc",
                "displayName": "Mystery Person",
            },
        ],
    }
    finding = adapter._build_finding("x@test.com", "abc", data)
    persons = [e for e in finding.entities if e.entity_type == EntityType.PERSON]
    assert len(persons) == 1
    assert persons[0].label == "Mystery Person"


def test_build_finding_empty_entry(adapter):
    """should handle empty profile entries"""
    data = {"entry": [{}]}
    finding = adapter._build_finding("x@test.com", "abc", data)
    # No person entity created when there's no name
    persons = [e for e in finding.entities if e.entity_type == EntityType.PERSON]
    assert len(persons) == 0
    assert "empty" in finding.notes.lower() or len(finding.entities) == 0


def test_build_finding_no_entries(adapter):
    """should handle response with empty entry list"""
    data = {"entry": []}
    finding = adapter._build_finding("x@test.com", "abc", data)
    assert "empty" in finding.notes.lower()


def test_build_finding_extracts_usernames_from_urls(adapter, mock_gravatar_response):
    """should extract usernames from linked GitHub/Twitter URLs"""
    finding = adapter._build_finding(
        "test@example.com", "abc", mock_gravatar_response,
    )
    accounts = [e for e in finding.entities if e.entity_type == EntityType.ACCOUNT]
    github_acct = next(a for a in accounts if a.properties.get("platform") == "GitHub")
    assert github_acct.properties["username"] == "testuser"

    twitter_acct = next(a for a in accounts if a.properties.get("platform") == "Twitter")
    assert twitter_acct.properties["username"] == "testuser42"


# ------------------------------------------------------------------
# HTTP integration (mocked)
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_no_profile(adapter):
    """should return notes finding for 404"""
    mock_resp = MagicMock()
    mock_resp.status_code = 404

    with patch("osint_agent.tools.gravatar.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        finding = await adapter.run("nobody@nowhere.com")
        assert "no profile" in finding.notes.lower()


@pytest.mark.asyncio
async def test_run_hashes_email(adapter):
    """should MD5 hash the email for the API call"""
    mock_resp = MagicMock()
    mock_resp.status_code = 404

    with patch("osint_agent.tools.gravatar.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        await adapter.run("Test@Example.COM")
        call_url = mock_client.get.call_args[0][0]
        expected_hash = hashlib.md5(b"test@example.com").hexdigest()
        assert expected_hash in call_url


# ------------------------------------------------------------------
# Registry
# ------------------------------------------------------------------

def test_registered_in_registry():
    """should be registered and routable by email input type"""
    from osint_agent.tools.registry import ToolRegistry, INPUT_ROUTING

    assert "gravatar" in INPUT_ROUTING["email"]
    registry = ToolRegistry()
    adapter = registry.get("gravatar")
    assert adapter is not None
    assert adapter.is_available()
