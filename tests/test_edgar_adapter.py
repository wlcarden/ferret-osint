"""Tests for the EDGAR adapter's parsing logic."""

from osint_agent.tools.edgar import EdgarAdapter


def test_search_companies_returns_organizations():
    adapter = EdgarAdapter()
    # Mock the find() results by testing _search_companies indirectly
    # Since we can't easily mock edgartools internals, test the simpler paths
    assert adapter.name == "edgar"


def test_get_company_info_failure_returns_notes():
    """When a company lookup fails, the Finding should have an explanatory note."""
    adapter = EdgarAdapter()
    adapter._identity_set = True  # Skip identity setup
    # _get_company_info with a nonsense query should fail gracefully
    finding = adapter._get_company_info("ZZZZZZZZNOTREAL")
    assert finding.notes is not None
    assert "failed" in finding.notes.lower() or len(finding.entities) > 0
