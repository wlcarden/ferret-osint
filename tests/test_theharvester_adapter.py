"""Tests for theHarvester adapter's parsing logic."""

from osint_agent.models import EntityType, RelationType
from osint_agent.tools.theharvester import TheHarvesterAdapter


def _sample_harvester_output():
    return {
        "emails": ["alice@example.com", "bob@example.com", ""],
        "hosts": ["mail.example.com:1.2.3.4", "dev.example.com", "example.com"],
        "ips": ["1.2.3.4", "5.6.7.8"],
    }


def test_parse_creates_domain_entity():
    adapter = TheHarvesterAdapter()
    finding = adapter._parse_results("example.com", _sample_harvester_output())
    domains = [e for e in finding.entities if e.id == "domain:example.com"]
    assert len(domains) == 1


def test_parse_creates_email_entities():
    adapter = TheHarvesterAdapter()
    finding = adapter._parse_results("example.com", _sample_harvester_output())
    emails = [e for e in finding.entities if e.entity_type == EntityType.EMAIL]
    assert len(emails) == 2  # alice and bob, empty string skipped


def test_parse_creates_subdomain_entities():
    adapter = TheHarvesterAdapter()
    finding = adapter._parse_results("example.com", _sample_harvester_output())
    subdomains = [
        e for e in finding.entities
        if e.entity_type == EntityType.DOMAIN and e.id != "domain:example.com"
    ]
    # mail.example.com and dev.example.com (example.com itself is skipped)
    assert len(subdomains) == 2


def test_parse_creates_owns_relationships():
    adapter = TheHarvesterAdapter()
    finding = adapter._parse_results("example.com", _sample_harvester_output())
    owns = [r for r in finding.relationships if r.relation_type == RelationType.OWNS]
    assert len(owns) == 2
    assert all(r.source_id == "domain:example.com" for r in owns)


def test_parse_creates_email_relationships():
    adapter = TheHarvesterAdapter()
    finding = adapter._parse_results("example.com", _sample_harvester_output())
    has_email = [r for r in finding.relationships if r.relation_type == RelationType.HAS_EMAIL]
    assert len(has_email) == 2


def test_parse_empty_output():
    adapter = TheHarvesterAdapter()
    finding = adapter._parse_results("empty.com", {"emails": [], "hosts": [], "ips": []})
    assert len(finding.entities) == 1  # Just the domain
    assert "0 emails" in finding.notes
