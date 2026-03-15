"""Tests for the people search adapter."""

from unittest.mock import MagicMock, patch

import pytest

from osint_agent.tools.peoplesearch import (
    PeopleSearchAdapter,
    _build_search_urls,
    _is_challenge_page,
    _normalize_state,
    _parse_name,
    _parse_spokeo_jsonld,
    _spokeo_person_to_record,
    _try_parse,
)

# ------------------------------------------------------------------
# Name parsing
# ------------------------------------------------------------------

def test_parse_name_two_parts():
    """should split 'First Last' into (first, last)"""
    assert _parse_name("Thomas Jacob") == ("Thomas", "Jacob")


def test_parse_name_comma_format():
    """should handle 'Last, First' format"""
    assert _parse_name("Jacob, Thomas") == ("Thomas", "Jacob")


def test_parse_name_three_parts():
    """should use first and last tokens for 3+ part names"""
    assert _parse_name("Thomas M Jacob") == ("Thomas", "Jacob")


def test_parse_name_single():
    """should return empty last name for single name"""
    assert _parse_name("Thomas") == ("Thomas", "")


def test_parse_name_strips_whitespace():
    """should strip leading/trailing whitespace"""
    assert _parse_name("  Thomas Jacob  ") == ("Thomas", "Jacob")


# ------------------------------------------------------------------
# State normalization
# ------------------------------------------------------------------

def test_normalize_state_full_name():
    """should return (full, abbrev) for full state name"""
    assert _normalize_state("Virginia") == ("virginia", "VA")


def test_normalize_state_abbreviation():
    """should return (full, abbrev) for abbreviation"""
    assert _normalize_state("VA") == ("virginia", "VA")


def test_normalize_state_case_insensitive():
    """should handle any case"""
    assert _normalize_state("virginia") == ("virginia", "VA")
    assert _normalize_state("va") == ("virginia", "VA")


def test_normalize_state_unknown():
    """should return empty for unrecognized state"""
    assert _normalize_state("Narnia") == ("", "")


def test_normalize_state_multi_word():
    """should handle multi-word state names"""
    assert _normalize_state("New York") == ("new york", "NY")
    assert _normalize_state("West Virginia") == ("west virginia", "WV")


# ------------------------------------------------------------------
# URL building
# ------------------------------------------------------------------

def test_build_urls_with_state():
    """should generate URLs for all 6 sites with state filtering"""
    urls = _build_search_urls("Thomas", "Jacob", "virginia", "VA", "")
    assert len(urls) == 6
    site_names = [name for name, _ in urls]
    assert "TruePeopleSearch" in site_names
    assert "FastPeopleSearch" in site_names
    assert "ThatsThem" in site_names
    assert "Spokeo" in site_names
    assert "CyberBackgroundChecks" in site_names
    assert "Radaris" in site_names


def test_build_urls_without_state():
    """should generate URLs without state parameters"""
    urls = _build_search_urls("Thomas", "Jacob", "", "", "")
    assert len(urls) == 6
    for _, url in urls:
        assert "Thomas" in url or "thomas" in url
        assert "Jacob" in url or "jacob" in url


def test_build_urls_truepeoplesearch_format():
    """should format TruePeopleSearch URL correctly"""
    urls = _build_search_urls("Thomas", "Jacob", "virginia", "VA", "")
    tps_url = [url for name, url in urls if name == "TruePeopleSearch"][0]
    assert "truepeoplesearch.com/results" in tps_url
    assert "name=" in tps_url
    assert "citystatezip=" in tps_url


def test_build_urls_fastpeoplesearch_format():
    """should format FastPeopleSearch URL with dashes"""
    urls = _build_search_urls("Thomas", "Jacob", "virginia", "VA", "")
    fps_url = [url for name, url in urls if name == "FastPeopleSearch"][0]
    assert "fastpeoplesearch.com/name/thomas-jacob" in fps_url
    assert "virginia" in fps_url


def test_build_urls_thatsthem_format():
    """should format That's Them URL with capitalized names"""
    urls = _build_search_urls("Thomas", "Jacob", "virginia", "VA", "")
    tt_url = [url for name, url in urls if name == "ThatsThem"][0]
    assert "thatsthem.com/name/Thomas-Jacob/Virginia" in tt_url


def test_build_urls_with_city():
    """should include city in location-aware URLs"""
    urls = _build_search_urls("Thomas", "Jacob", "virginia", "VA", "Herndon")
    tps_url = [url for name, url in urls if name == "TruePeopleSearch"][0]
    assert "Herndon" in tps_url


# ------------------------------------------------------------------
# Challenge page detection
# ------------------------------------------------------------------

def test_challenge_page_cloudflare():
    """should detect Cloudflare challenge pages"""
    assert _is_challenge_page("<title>Just a moment...</title>")
    assert _is_challenge_page("Captcha Challenge - TruePeopleSearch.com")
    assert _is_challenge_page("Security Challenge")
    assert _is_challenge_page('<div id="_cf_chl_opt">')


def test_challenge_page_normal():
    """should not flag normal HTML"""
    assert not _is_challenge_page("<html><body>Results for Thomas Jacob</body></html>")
    assert not _is_challenge_page("")


# ------------------------------------------------------------------
# HTML parsing
# ------------------------------------------------------------------

def test_try_parse_unknown_site():
    """should return None for unsupported sites"""
    assert _try_parse("Spokeo", "<html></html>") is None


def test_try_parse_empty_html():
    """should return None for empty HTML"""
    assert _try_parse("TruePeopleSearch", "") is None


def test_try_parse_no_results():
    """should return None when no matching elements found"""
    assert _try_parse("TruePeopleSearch", "<html><body>No results</body></html>") is None


# ------------------------------------------------------------------
# Spokeo JSON-LD parsing
# ------------------------------------------------------------------

SPOKEO_PERSON_JSONLD = {
    "@context": "http://schema.org",
    "@type": "Person",
    "name": "Thomas M Jacob",
    "additionalName": ["Thomas Mark Jacob", "Tommy Jacob"],
    "homeLocation": [
        {
            "@type": "Place",
            "address": {
                "@type": "PostalAddress",
                "streetAddress": "8989 Brook Rd",
                "addressLocality": "Mc Lean",
                "addressRegion": "VA",
                "postalCode": "22102",
            },
        },
    ],
    "relatedTo": [
        {"@type": "Person", "name": "Joseph Jacob", "url": "https://www.spokeo.com/Joseph-Jacob"},
        {"@type": "Person", "name": "Mary Jacob", "url": "https://www.spokeo.com/Mary-Jacob"},
    ],
    "url": "https://www.spokeo.com/Thomas-Jacob/Virginia/Mc-Lean/p12345",
}


def test_spokeo_person_to_record():
    """should extract name, addresses, relatives, aliases from Person JSON-LD"""
    record = _spokeo_person_to_record(SPOKEO_PERSON_JSONLD)
    assert record["name"] == "Thomas M Jacob"
    assert record["aliases"] == ["Thomas Mark Jacob", "Tommy Jacob"]
    assert "8989 Brook Rd" in record["address"]
    assert "Mc Lean" in record["address"]
    assert len(record["all_addresses"]) == 1
    assert record["relatives"] == ["Joseph Jacob", "Mary Jacob"]
    assert record["profile_url"] == "https://www.spokeo.com/Thomas-Jacob/Virginia/Mc-Lean/p12345"


def test_spokeo_person_no_name():
    """should return None for Person without name"""
    assert _spokeo_person_to_record({"@type": "Person"}) is None


def test_spokeo_person_empty_locations():
    """should handle Person with no homeLocation"""
    record = _spokeo_person_to_record({
        "@type": "Person",
        "name": "Test Person",
        "homeLocation": [],
        "relatedTo": [],
    })
    assert record["name"] == "Test Person"
    assert "address" not in record
    assert "relatives" not in record


def test_parse_spokeo_jsonld_from_html():
    """should extract Person objects from script tags"""
    import json
    html = f'''<html>
    <script type="application/ld+json">{json.dumps({"@type": "WebPage", "name": "Test"})}</script>
    <script type="application/ld+json">{json.dumps([SPOKEO_PERSON_JSONLD])}</script>
    </html>'''
    records = _parse_spokeo_jsonld(html)
    assert records is not None
    assert len(records) == 1
    assert records[0]["name"] == "Thomas M Jacob"


def test_parse_spokeo_jsonld_skips_non_person():
    """should ignore non-Person JSON-LD objects"""
    import json
    html = f'''<html>
    <script type="application/ld+json">{json.dumps({"@type": "Organization"})}</script>
    </html>'''
    records = _parse_spokeo_jsonld(html)
    assert records is None


def test_parse_spokeo_jsonld_handles_malformed():
    """should handle malformed JSON gracefully"""
    html = '<script type="application/ld+json">not valid json</script>'
    records = _parse_spokeo_jsonld(html)
    assert records is None


def test_try_parse_routes_spokeo():
    """should route Spokeo to JSON-LD parser"""
    import json
    html = f'<script type="application/ld+json">{json.dumps([SPOKEO_PERSON_JSONLD])}</script>'
    records = _try_parse("Spokeo", html)
    assert records is not None
    assert records[0]["name"] == "Thomas M Jacob"


# ------------------------------------------------------------------
# Adapter basics
# ------------------------------------------------------------------

def test_adapter_name():
    """should have correct tool name"""
    adapter = PeopleSearchAdapter()
    assert adapter.name == "peoplesearch"


def test_adapter_is_available():
    """should be available when curl_cffi is installed"""
    adapter = PeopleSearchAdapter()
    assert adapter.is_available() is True


def test_adapter_is_available_without_curl_cffi():
    """should not be available when curl_cffi missing"""
    adapter = PeopleSearchAdapter()
    with patch.dict("sys.modules", {"curl_cffi": None}):
        with patch("builtins.__import__", side_effect=ImportError):
            assert adapter.is_available() is False


# ------------------------------------------------------------------
# Full adapter run (mocked HTTP)
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_returns_finding_with_urls():
    """should return Finding with search URLs even when all sites blocked"""
    adapter = PeopleSearchAdapter()

    # Mock curl_cffi to return 403 for all sites
    mock_resp = MagicMock()
    mock_resp.status_code = 403
    mock_resp.text = "403 Forbidden"

    with patch("osint_agent.tools.peoplesearch.PeopleSearchAdapter._scrape_sites") as mock_scrape:
        from osint_agent.tools.peoplesearch import _SiteResult
        mock_scrape.return_value = [
            _SiteResult("TruePeopleSearch", "https://example.com/tps", "blocked", error="HTTP 403"),
            _SiteResult("FastPeopleSearch", "https://example.com/fps", "blocked", error="HTTP 403"),
        ]
        finding = await adapter.run(query="Thomas Jacob", state="Virginia")

    assert finding.entities
    person = finding.entities[0]
    assert person.label == "Thomas Jacob"
    assert person.properties["state"] == "VA"
    assert len(person.sources) == 2
    assert "blocked" in finding.notes


@pytest.mark.asyncio
async def test_run_requires_first_and_last():
    """should return error note for single name"""
    adapter = PeopleSearchAdapter()
    finding = await adapter.run(query="Thomas")
    assert "first and last name" in finding.notes


@pytest.mark.asyncio
async def test_run_with_scraped_data():
    """should create sub-person, address, phone, relative entities from scraped records"""
    adapter = PeopleSearchAdapter()

    with patch("osint_agent.tools.peoplesearch.PeopleSearchAdapter._scrape_sites") as mock_scrape:
        from osint_agent.tools.peoplesearch import _SiteResult
        mock_scrape.return_value = [
            _SiteResult(
                "Spokeo",
                "https://www.spokeo.com/Thomas-Jacob/VA",
                "scraped",
                records=[{
                    "name": "Thomas M Jacob",
                    "address": "8989 Brook Rd, Mc Lean, VA, 22102",
                    "all_addresses": ["8989 Brook Rd, Mc Lean, VA, 22102"],
                    "aliases": ["Tommy Jacob"],
                    "profile_url": "https://www.spokeo.com/Thomas-Jacob/Virginia/Mc-Lean/p12345",
                    "relatives": ["Joseph Jacob", "Mary Jacob"],
                }],
            ),
        ]
        finding = await adapter.run(query="Thomas Jacob", state="VA")

    # Should have: target person + sub-person + address + 2 relatives = 5 entities
    assert len(finding.entities) >= 5

    types = [e.entity_type.value for e in finding.entities]
    assert types.count("person") >= 3  # Target + sub-person + 2 relatives
    assert types.count("address") >= 1

    # Sub-person should have aliases and profile URL
    sub_persons = [e for e in finding.entities if "Thomas M Jacob" in e.label]
    assert len(sub_persons) == 1
    assert sub_persons[0].properties.get("aliases") == ["Tommy Jacob"]
    assert "spokeo.com" in sub_persons[0].properties.get("profile_url", "")

    # Should have relationships
    rel_types = [r.relation_type.value for r in finding.relationships]
    assert "has_address" in rel_types
    assert rel_types.count("connected_to") == 2


# ------------------------------------------------------------------
# Registry integration
# ------------------------------------------------------------------

def test_registry_includes_peoplesearch():
    """should be registered and routed for person_name input type"""
    from osint_agent.tools.registry import INPUT_ROUTING, ToolRegistry

    assert "peoplesearch" in INPUT_ROUTING["person_name"]
    registry = ToolRegistry()
    assert registry.get("peoplesearch") is not None
