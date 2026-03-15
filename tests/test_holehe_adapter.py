"""Tests for the Holehe adapter's parsing and permutation logic."""

from osint_agent.models import EntityType, RelationType
from osint_agent.tools.holehe_adapter import HoleheAdapter


def _sample_holehe_results():
    """Minimal holehe output matching real format."""
    return [
        {
            "name": "github",
            "domain": "github.com",
            "method": "register",
            "frequent_rate_limit": False,
            "rateLimit": False,
            "exists": True,
            "emailrecovery": None,
            "phoneNumber": None,
            "others": None,
        },
        {
            "name": "spotify",
            "domain": "spotify.com",
            "method": "login",
            "frequent_rate_limit": False,
            "rateLimit": False,
            "exists": True,
            "emailrecovery": "j***e@gmail.com",
            "phoneNumber": "+1***45",
            "others": None,
        },
        {
            "name": "twitter",
            "domain": "twitter.com",
            "method": "login",
            "frequent_rate_limit": False,
            "rateLimit": False,
            "exists": False,
            "emailrecovery": None,
            "phoneNumber": None,
            "others": None,
        },
    ]


def test_parse_creates_email_entity():
    adapter = HoleheAdapter()
    finding = adapter._parse_results("test@example.com", _sample_holehe_results())
    emails = [e for e in finding.entities if e.id == "email:test@example.com"]
    assert len(emails) == 1
    assert emails[0].entity_type == EntityType.EMAIL


def test_parse_creates_accounts_for_exists_only():
    adapter = HoleheAdapter()
    finding = adapter._parse_results("test@example.com", _sample_holehe_results())
    accounts = [e for e in finding.entities if e.entity_type == EntityType.ACCOUNT]
    # github exists=True, spotify exists=True, twitter exists=False
    assert len(accounts) == 2
    platforms = {e.properties["platform"] for e in accounts}
    assert platforms == {"github", "spotify"}


def test_parse_captures_recovery_email():
    adapter = HoleheAdapter()
    finding = adapter._parse_results("test@example.com", _sample_holehe_results())
    recovery_emails = [e for e in finding.entities if e.id == "email:j***e@gmail.com"]
    assert len(recovery_emails) == 1
    # Should have a HAS_EMAIL relationship from the spotify account
    recovery_rels = [
        r for r in finding.relationships
        if r.relation_type == RelationType.HAS_EMAIL and r.target_id == "email:j***e@gmail.com"
    ]
    assert len(recovery_rels) == 1
    assert recovery_rels[0].source_id == "account:spotify:test@example.com"


def test_parse_captures_phone_number():
    adapter = HoleheAdapter()
    finding = adapter._parse_results("test@example.com", _sample_holehe_results())
    phones = [e for e in finding.entities if e.entity_type == EntityType.PHONE]
    assert len(phones) == 1
    assert phones[0].label == "+1***45"


def test_parse_no_results():
    adapter = HoleheAdapter()
    no_hits = [{"name": "x", "domain": "x.com", "exists": False}]
    finding = adapter._parse_results("nobody@example.com", no_hits)
    assert len(finding.entities) == 1  # Just the email entity
    assert "0 registrations" in finding.notes


def test_all_entities_have_sources():
    adapter = HoleheAdapter()
    finding = adapter._parse_results("test@example.com", _sample_holehe_results())
    for entity in finding.entities:
        assert len(entity.sources) >= 1
        assert entity.sources[0].tool == "holehe"


# --- Permutation generation tests ---


class TestGeneratePermutations:
    """Tests for _generate_permutations static method."""

    def test_standard_patterns(self):
        """should produce all 8 expected patterns for typical input"""
        perms = HoleheAdapter._generate_permutations(
            "Bill", "Beckwith", "ois.com",
        )
        assert perms == [
            "bbeckwith@ois.com",
            "beckwith@ois.com",
            "billbeckwith@ois.com",
            "bill.beckwith@ois.com",
            "beckwithb@ois.com",
            "bill@ois.com",
            "b.beckwith@ois.com",
            "beckwith.b@ois.com",
        ]

    def test_all_lowercase(self):
        """should lowercase all name parts regardless of input casing"""
        perms = HoleheAdapter._generate_permutations(
            "JANE", "DOE", "example.com",
        )
        for p in perms:
            local = p.split("@")[0]
            assert local == local.lower()

    def test_domain_preserved(self):
        """should use the domain exactly as provided"""
        perms = HoleheAdapter._generate_permutations(
            "Alice", "Smith", "MyCompany.org",
        )
        for p in perms:
            assert p.endswith("@MyCompany.org")

    def test_single_char_first_name_deduplicates(self):
        """should deduplicate when first name is a single character"""
        perms = HoleheAdapter._generate_permutations(
            "J", "Doe", "x.com",
        )
        # first initial == full first name, so some patterns collapse
        assert len(perms) == len(set(perms))
        # "jdoe@x.com" should appear once, not twice
        assert perms.count("jdoe@x.com") == 1
        # "j@x.com" should appear once
        assert perms.count("j@x.com") == 1

    def test_whitespace_stripped(self):
        """should strip leading/trailing whitespace from inputs"""
        perms = HoleheAdapter._generate_permutations(
            "  Bob  ", "  Jones  ", "test.com",
        )
        assert "bobjones@test.com" in perms
        assert "bjones@test.com" in perms

    def test_returns_list_of_strings(self):
        """should return a non-empty list of valid email strings"""
        perms = HoleheAdapter._generate_permutations(
            "Test", "User", "domain.com",
        )
        assert isinstance(perms, list)
        assert len(perms) > 0
        for p in perms:
            assert isinstance(p, str)
            assert "@" in p
            local, domain = p.split("@")
            assert len(local) > 0
            assert domain == "domain.com"

    def test_no_duplicates(self):
        """should never contain duplicate entries"""
        perms = HoleheAdapter._generate_permutations(
            "A", "B", "c.com",
        )
        assert len(perms) == len(set(perms))

    def test_order_preserved(self):
        """should maintain the canonical pattern order"""
        perms = HoleheAdapter._generate_permutations(
            "Jane", "Smith", "corp.io",
        )
        # First pattern is always initial+last
        assert perms[0] == "jsmith@corp.io"
        # Second is last-only
        assert perms[1] == "smith@corp.io"
        # Third is first+last (no separator)
        assert perms[2] == "janesmith@corp.io"
        # Fourth is first.last
        assert perms[3] == "jane.smith@corp.io"
