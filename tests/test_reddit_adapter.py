"""Tests for the Reddit adapter — profile, post history, and analysis."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from datetime import UTC, datetime

from osint_agent.tools.reddit import RedditAdapter, _POLITICAL_SUBREDDITS
from osint_agent.models import EntityType, RelationType


@pytest.fixture
def adapter():
    return RedditAdapter(timeout=10)


@pytest.fixture
def mock_about():
    """Canned Reddit user about response."""
    return {
        "name": "testuser42",
        "created_utc": 1577836800.0,  # 2020-01-01 00:00:00 UTC
        "link_karma": 1500,
        "comment_karma": 25000,
        "total_karma": 26500,
        "is_gold": False,
        "is_mod": True,
        "verified": False,
    }


@pytest.fixture
def mock_posts():
    """Canned Reddit submitted listing."""
    return [
        {
            "subreddit": "Portland",
            "title": "Best coffee shops in SE Portland?",
            "selftext": "I'm from Portland, OR and looking for recs",
            "score": 42,
            "num_comments": 15,
            "created_utc": 1700000000.0,
            "permalink": "/r/Portland/comments/abc/best_coffee_shops/",
            "id": "abc123",
        },
        {
            "subreddit": "conservative",
            "title": "Thoughts on recent policy",
            "selftext": "",
            "score": 10,
            "num_comments": 5,
            "created_utc": 1700100000.0,
            "permalink": "/r/conservative/comments/def/thoughts/",
            "id": "def456",
        },
        {
            "subreddit": "Portland",
            "title": "Sunset from Mt Tabor",
            "selftext": "",
            "score": 200,
            "num_comments": 30,
            "created_utc": 1700200000.0,
            "permalink": "/r/Portland/comments/ghi/sunset/",
            "id": "ghi789",
        },
    ]


@pytest.fixture
def mock_comments():
    """Canned Reddit comments listing."""
    return [
        {
            "subreddit": "Portland",
            "body": "I live in Portland and this is accurate",
            "score": 5,
            "created_utc": 1700050000.0,
            "link_title": "Portland housing market",
        },
        {
            "subreddit": "AskReddit",
            "body": "Great question. I think...",
            "score": 100,
            "created_utc": 1700150000.0,
            "link_title": "What's your unpopular opinion?",
        },
        {
            "subreddit": "conservative",
            "body": "I agree with this take",
            "score": 3,
            "created_utc": 1700250000.0,
            "link_title": "Policy discussion",
        },
    ]


# ------------------------------------------------------------------
# Availability
# ------------------------------------------------------------------

def test_is_available(adapter):
    """should always be available (only needs httpx)"""
    assert adapter.is_available() is True


# ------------------------------------------------------------------
# Analysis functions
# ------------------------------------------------------------------

def test_analyze_subreddits(adapter, mock_posts, mock_comments):
    """should cluster subreddits and identify political ones"""
    result = adapter._analyze_subreddits(mock_posts, mock_comments)

    assert result["unique_subreddits"] == 3
    top_subs = dict(result["top_subreddits"])
    assert top_subs["portland"] == 3  # 2 posts + 1 comment
    assert top_subs["conservative"] == 2

    political = dict(result["political_subreddits"])
    assert "conservative" in political


def test_analyze_temporal(adapter, mock_posts, mock_comments):
    """should compute peak hours and estimate timezone"""
    result = adapter._analyze_temporal(mock_posts, mock_comments)

    assert len(result["peak_hours"]) > 0
    assert result["estimated_timezone"]  # Should produce a UTC±N string
    assert result["estimated_timezone"].startswith("UTC")


def test_analyze_temporal_empty(adapter):
    """should handle empty input"""
    result = adapter._analyze_temporal([], [])
    assert result["peak_hours"] == []
    assert result["estimated_timezone"] == ""


def test_extract_locations(adapter, mock_posts, mock_comments):
    """should extract self-disclosed locations from text"""
    locations = adapter._extract_locations(mock_posts, mock_comments)
    # "I'm from Portland, OR" and "I live in Portland" should match
    location_lower = [l.lower() for l in locations]
    assert any("portland" in loc for loc in location_lower)


def test_extract_locations_deduplicates(adapter):
    """should not return duplicate locations"""
    posts = [
        {"title": "", "selftext": "I'm from Portland, OR"},
        {"title": "", "selftext": "I'm from Portland, OR"},
    ]
    locations = adapter._extract_locations(posts, [])
    portland_count = sum(1 for l in locations if "Portland" in l)
    assert portland_count == 1


# ------------------------------------------------------------------
# Finding construction
# ------------------------------------------------------------------

def test_build_finding(adapter, mock_about, mock_posts, mock_comments):
    """should build complete finding with entities and relationships"""
    finding = adapter._build_finding("testuser42", mock_about, mock_posts, mock_comments)

    # Should have the account entity
    account = next(
        e for e in finding.entities if e.entity_type == EntityType.ACCOUNT
    )
    assert account.id == "account:reddit:testuser42"
    assert account.properties["username"] == "testuser42"
    assert account.properties["total_karma"] == 26500
    assert account.properties["post_count"] == 3
    assert account.properties["comment_count"] == 3
    assert "top_subreddits" in account.properties
    assert "political_subreddits" in account.properties

    # Should have subreddit entities
    subreddit_entities = [
        e for e in finding.entities if e.entity_type == EntityType.ORGANIZATION
    ]
    assert len(subreddit_entities) > 0
    sub_labels = {e.label for e in subreddit_entities}
    assert "r/portland" in sub_labels

    # Should have AFFILIATED_WITH relationships
    affiliations = [
        r for r in finding.relationships
        if r.relation_type == RelationType.AFFILIATED_WITH
    ]
    assert len(affiliations) > 0

    # Should have notes
    assert "testuser42" in finding.notes
    assert "26,500" in finding.notes  # formatted karma


def test_build_finding_empty_history(adapter, mock_about):
    """should handle user with no posts or comments"""
    finding = adapter._build_finding("emptyuser", mock_about, [], [])

    entities = [e for e in finding.entities if e.entity_type == EntityType.ACCOUNT]
    assert len(entities) == 1
    assert entities[0].properties["post_count"] == 0


# ------------------------------------------------------------------
# HTTP integration (mocked)
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_user_not_found(adapter):
    """should return notes finding for 404"""
    mock_resp = MagicMock()
    mock_resp.status_code = 404

    with patch("osint_agent.tools.reddit.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        finding = await adapter.run("nonexistent_user_xyz")
        assert "not found" in finding.notes


@pytest.mark.asyncio
async def test_run_strips_prefix(adapter):
    """should strip u/ prefix from username"""
    mock_resp_about = MagicMock()
    mock_resp_about.status_code = 404

    with patch("osint_agent.tools.reddit.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp_about)
        mock_client_cls.return_value = mock_client

        # Should work whether you pass "u/testuser" or "testuser"
        await adapter.run("u/testuser")
        call_url = mock_client.get.call_args_list[0][0][0]
        assert "u/u/" not in call_url  # Should not double the prefix


# ------------------------------------------------------------------
# Political subreddit set
# ------------------------------------------------------------------

def test_political_subreddits_coverage():
    """should include key tracking targets"""
    # These are commonly monitored in antifascist OSINT
    assert "conservative" in _POLITICAL_SUBREDDITS
    assert "conspiracy" in _POLITICAL_SUBREDDITS
    assert "the_donald" in _POLITICAL_SUBREDDITS


# ------------------------------------------------------------------
# Registry
# ------------------------------------------------------------------

def test_registered_in_registry():
    """should be registered and routable by username input type"""
    from osint_agent.tools.registry import ToolRegistry, INPUT_ROUTING

    assert "reddit" in INPUT_ROUTING["username"]
    registry = ToolRegistry()
    adapter = registry.get("reddit")
    assert adapter is not None
    assert adapter.is_available()
