"""Tests for the Maigret adapter's parsing logic.

These test the output normalization without running the actual CLI tool.
"""

from osint_agent.models import EntityType, RelationType
from osint_agent.tools.maigret import MaigretAdapter
from osint_agent.tools.maigret_filters import (
    BLOCKLISTED_SITES,
    is_false_positive,
)


def _sample_maigret_output():
    """Minimal maigret JSON matching real output structure."""
    return {
        "GitHub": {
            "url_user": "https://github.com/testuser",
            "status": {
                "username": "testuser",
                "site_name": "GitHub",
                "url": "https://github.com/testuser",
                "status": "Claimed",
                "ids": {"uid": "12345"},
                "tags": ["coding"],
            },
            "http_status": 200,
            "rank": 50,
        },
        "Twitter": {
            "url_user": "https://twitter.com/testuser",
            "status": {
                "username": "testuser",
                "site_name": "Twitter",
                "url": "https://twitter.com/testuser",
                "status": "Claimed",
                "ids": {},
                "tags": ["social"],
            },
            "http_status": 200,
            "rank": 10,
        },
        "FakeSite": {
            "url_user": "https://fakesite.com/testuser",
            "status": {
                "username": "testuser",
                "site_name": "FakeSite",
                "url": "https://fakesite.com/testuser",
                "status": "Unknown",
                "ids": {},
                "tags": [],
            },
            "http_status": 404,
            "rank": 999,
        },
    }


def test_parse_creates_username_entity():
    adapter = MaigretAdapter()
    finding = adapter._parse_results("testuser", _sample_maigret_output())
    usernames = [
        e for e in finding.entities
        if e.entity_type == EntityType.USERNAME
        and e.id == "username:testuser"
    ]
    assert len(usernames) == 1
    assert usernames[0].label == "testuser"


def test_parse_creates_account_entities_for_claimed_only():
    adapter = MaigretAdapter()
    finding = adapter._parse_results("testuser", _sample_maigret_output())
    accounts = [e for e in finding.entities if e.entity_type == EntityType.ACCOUNT]
    # GitHub and Twitter are Claimed; FakeSite is Unknown
    assert len(accounts) == 2
    platforms = {e.properties["platform"] for e in accounts}
    assert platforms == {"GitHub", "Twitter"}


def test_parse_creates_has_account_relationships():
    adapter = MaigretAdapter()
    finding = adapter._parse_results("testuser", _sample_maigret_output())
    has_account = [r for r in finding.relationships if r.relation_type == RelationType.HAS_ACCOUNT]
    assert len(has_account) == 2
    assert all(r.source_id == "username:testuser" for r in has_account)


def test_parse_extracts_platform_ids_as_also_known_as():
    adapter = MaigretAdapter()
    finding = adapter._parse_results("testuser", _sample_maigret_output())
    aka = [r for r in finding.relationships if r.relation_type == RelationType.ALSO_KNOWN_AS]
    # GitHub has uid:12345, Twitter has no extracted IDs
    assert len(aka) == 1
    assert aka[0].target_id == "username:uid:12345"


def test_parse_empty_output():
    adapter = MaigretAdapter()
    finding = adapter._parse_results("nobody", {})
    assert len(finding.entities) == 1  # Just the username entity
    assert len(finding.relationships) == 0
    assert "0 accounts" in finding.notes


def test_all_entities_have_sources():
    adapter = MaigretAdapter()
    finding = adapter._parse_results("testuser", _sample_maigret_output())
    for entity in finding.entities:
        assert len(entity.sources) >= 1
        assert entity.sources[0].tool == "maigret"


# ------------------------------------------------------------------
# False positive filtering
# ------------------------------------------------------------------

def _sample_with_false_positives():
    """Maigret output including known false-positive sites."""
    return {
        "GitHub": {
            "url_user": "https://github.com/testuser",
            "status": {"status": "Claimed", "ids": {}, "tags": ["coding"]},
            "http_status": 200,
            "rank": 50,
        },
        "AdultFriendFinder": {
            "url_user": "https://adultfriendfinder.com/profile/testuser",
            "status": {"status": "Claimed", "ids": {}, "tags": ["dating"]},
            "http_status": 200,
            "rank": 2857,
        },
        "Bibsonomy": {
            "url_user": "https://www.bibsonomy.org/user/testuser",
            "status": {"status": "Claimed", "ids": {}, "tags": []},
            "http_status": 200,
            "rank": 5668,
        },
        "getmyuni": {
            "url_user": "https://www.getmyuni.com/user/testuser",
            "status": {"status": "Claimed", "ids": {}, "tags": ["in"]},
            "http_status": 404,
            "rank": 8345,
        },
    }


def test_blocklisted_sites_filtered():
    """should exclude accounts on blocklisted sites"""
    adapter = MaigretAdapter()
    finding = adapter._parse_results("testuser", _sample_with_false_positives())
    accounts = [e for e in finding.entities if e.entity_type == EntityType.ACCOUNT]
    platforms = {e.properties["platform"] for e in accounts}
    assert "GitHub" in platforms
    assert "AdultFriendFinder" not in platforms
    assert "Bibsonomy" not in platforms


def test_http_404_filtered():
    """should exclude accounts that returned HTTP 404"""
    adapter = MaigretAdapter()
    finding = adapter._parse_results("testuser", _sample_with_false_positives())
    accounts = [e for e in finding.entities if e.entity_type == EntityType.ACCOUNT]
    platforms = {e.properties["platform"] for e in accounts}
    assert "getmyuni" not in platforms


def test_filtered_count_in_notes():
    """should report filtered count in finding notes"""
    adapter = MaigretAdapter()
    finding = adapter._parse_results("testuser", _sample_with_false_positives())
    assert "1 accounts" in finding.notes  # Only GitHub passes
    assert "3 false positives filtered" in finding.notes


def test_no_relationships_for_filtered_accounts():
    """should not create HAS_ACCOUNT rels for filtered sites"""
    adapter = MaigretAdapter()
    finding = adapter._parse_results("testuser", _sample_with_false_positives())
    has_account = [r for r in finding.relationships if r.relation_type == RelationType.HAS_ACCOUNT]
    target_ids = {r.target_id for r in has_account}
    assert "account:adultfriendfinder:testuser" not in target_ids
    assert "account:github:testuser" in target_ids


# ------------------------------------------------------------------
# is_false_positive unit tests
# ------------------------------------------------------------------

def test_is_fp_blocklisted():
    """should flag blocklisted sites"""
    result = is_false_positive("AdultFriendFinder")
    assert result is not None
    assert "blocklisted" in result


def test_is_fp_http_404():
    """should flag HTTP 404 responses"""
    result = is_false_positive("SomeSite", http_status=404)
    assert result is not None
    assert "404" in result


def test_is_fp_http_403():
    """should flag HTTP 403 responses"""
    result = is_false_positive("SomeSite", http_status=403)
    assert result is not None


def test_is_fp_legitimate():
    """should return None for legitimate results"""
    result = is_false_positive("GitHub", http_status=200)
    assert result is None


def test_metadata_ids_not_extracted():
    """should not create entities for metadata fields like follower_count"""
    raw = {
        "GitHub": {
            "url_user": "https://github.com/testuser",
            "status": {
                "status": "Claimed",
                "ids": {
                    "uid": "12345",           # real identifier — keep
                    "follower_count": "100",   # metadata — skip
                    "is_verified": "True",     # metadata — skip
                    "type": "User",            # metadata — skip
                    "username": "testuser",    # real identifier — keep
                },
                "tags": [],
            },
            "http_status": 200,
        },
    }
    adapter = MaigretAdapter()
    finding = adapter._parse_results("testuser", raw)
    xref_ids = [
        e.id for e in finding.entities
        if e.entity_type == EntityType.USERNAME and ":" in e.id.split(":", 1)[1]
    ]
    # Should only have uid:12345 and username:testuser, not follower_count etc.
    assert "username:uid:12345" in xref_ids
    assert "username:username:testuser" in xref_ids
    assert not any("follower_count" in x for x in xref_ids)
    assert not any("is_verified" in x for x in xref_ids)
    assert not any("type" in x for x in xref_ids)


def test_blocklist_contains_known_offenders():
    """should contain all empirically confirmed false-positive sites"""
    expected = {
        "AdultFriendFinder", "authorSTREAM", "Bibsonomy", "Blu-ray",
        "getmyuni", "hashnode", "Kaggle", "TechPowerUp", "Tom's guide",
    }
    assert expected == BLOCKLISTED_SITES
