"""Tests for the Obsidian vault exporter."""



from osint_agent.models import Entity, Relationship, Source
from osint_agent.vault_export import VaultExporter, _safe_filename

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_entities():
    """Build a small test graph."""
    return [
        Entity(
            id="person:alice",
            entity_type="person",
            label="Alice Johnson",
            properties={"employer": "Acme Corp", "city": "Richmond"},
            sources=[Source(tool="peoplesearch")],
        ),
        Entity(
            id="org:acme",
            entity_type="organization",
            label="Acme Corp",
            properties={"url": "https://acme.example.com"},
            sources=[Source(tool="ddg_search"), Source(tool="usaspending")],
        ),
        Entity(
            id="email:alice@acme.com",
            entity_type="email",
            label="alice@acme.com",
            properties={},
            sources=[Source(tool="holehe")],
        ),
    ]


def _make_relationships():
    return [
        Relationship(
            source_id="person:alice",
            target_id="org:acme",
            relation_type="works_at",
        ),
        Relationship(
            source_id="person:alice",
            target_id="email:alice@acme.com",
            relation_type="has_email",
        ),
    ]


# ---------------------------------------------------------------------------
# Unit: filename sanitization
# ---------------------------------------------------------------------------

class TestSafeFilename:
    def test_normal_name(self):
        assert _safe_filename("Alice Johnson") == "Alice Johnson"

    def test_strips_unsafe_chars(self):
        assert _safe_filename('a/b:c*d?e"f') == "abcdef"

    def test_collapses_spaces(self):
        assert _safe_filename("a   b  c") == "a b c"

    def test_truncates_long_names(self):
        result = _safe_filename("x" * 200)
        assert len(result) <= 120
        assert result.endswith("...")

    def test_empty_becomes_unnamed(self):
        assert _safe_filename("") == "unnamed"
        assert _safe_filename("***") == "unnamed"

    def test_hash_and_brackets(self):
        assert _safe_filename("Issue #42 [draft]") == "Issue 42 draft"


# ---------------------------------------------------------------------------
# Integration: export_from_data
# ---------------------------------------------------------------------------

class TestVaultExporter:
    def test_creates_directory_structure(self, tmp_path):
        exporter = VaultExporter()
        entities = _make_entities()
        rels = _make_relationships()

        summary = exporter.export_from_data(entities, rels, tmp_path, "Test")

        assert summary["entities"] == 3
        assert summary["relationships"] == 2
        assert summary["files"] == 4  # 3 entities + index
        assert (tmp_path / "index.md").exists()
        assert (tmp_path / "person" / "Alice Johnson.md").exists()
        assert (tmp_path / "organization" / "Acme Corp.md").exists()
        assert (tmp_path / "email" / "alice@acme.com.md").exists()

    def test_index_contains_wikilinks(self, tmp_path):
        exporter = VaultExporter()
        exporter.export_from_data(
            _make_entities(), _make_relationships(), tmp_path, "Test",
        )
        index = (tmp_path / "index.md").read_text()

        assert "# Test" in index
        assert "[[person/Alice Johnson|Alice Johnson]]" in index
        assert "[[organization/Acme Corp|Acme Corp]]" in index
        assert "3 entities, 2 relationships" in index

    def test_entity_page_has_frontmatter(self, tmp_path):
        exporter = VaultExporter()
        exporter.export_from_data(
            _make_entities(), _make_relationships(), tmp_path, "Test",
        )
        page = (tmp_path / "person" / "Alice Johnson.md").read_text()

        assert page.startswith("---\n")
        # Colon in entity ID triggers YAML quoting.
        assert 'entity_id: "person:alice"' in page
        assert "type: person" in page
        assert "label: Alice Johnson" in page
        assert "employer: Acme Corp" in page
        assert "sources: [peoplesearch]" in page

    def test_entity_page_has_wikilink_connections(self, tmp_path):
        exporter = VaultExporter()
        exporter.export_from_data(
            _make_entities(), _make_relationships(), tmp_path, "Test",
        )
        page = (tmp_path / "person" / "Alice Johnson.md").read_text()

        assert "## Connections" in page
        assert "### works_at" in page
        assert "[[organization/Acme Corp|Acme Corp]]" in page
        assert "### has_email" in page
        assert "[[email/alice@acme.com|alice@acme.com]]" in page

    def test_entity_page_has_tags(self, tmp_path):
        exporter = VaultExporter()
        exporter.export_from_data(
            _make_entities(), _make_relationships(), tmp_path, "Test",
        )
        page = (tmp_path / "person" / "Alice Johnson.md").read_text()

        assert "tags:" in page
        assert "person" in page
        assert "source/peoplesearch" in page

    def test_url_properties_are_linked(self, tmp_path):
        exporter = VaultExporter()
        exporter.export_from_data(
            _make_entities(), _make_relationships(), tmp_path, "Test",
        )
        page = (tmp_path / "organization" / "Acme Corp.md").read_text()

        assert "[https://acme.example.com](https://acme.example.com)" in page

    def test_source_provenance_section(self, tmp_path):
        exporter = VaultExporter()
        exporter.export_from_data(
            _make_entities(), _make_relationships(), tmp_path, "Test",
        )
        page = (tmp_path / "organization" / "Acme Corp.md").read_text()

        assert "## Sources" in page
        assert "- ddg_search" in page
        assert "- usaspending" in page

    def test_duplicate_labels_disambiguated(self, tmp_path):
        entities = [
            Entity(
                id="person:john-1",
                entity_type="person",
                label="John Smith",
                sources=[Source(tool="test")],
            ),
            Entity(
                id="org:john-co",
                entity_type="organization",
                label="John Smith",
                sources=[Source(tool="test")],
            ),
        ]
        exporter = VaultExporter()
        _summary = exporter.export_from_data(entities, [], tmp_path, "Test")

        assert (tmp_path / "person" / "John Smith (person).md").exists()
        assert (tmp_path / "organization" / "John Smith (organization).md").exists()

    def test_empty_graph(self, tmp_path):
        exporter = VaultExporter()
        summary = exporter.export_from_data([], [], tmp_path, "Empty")

        assert summary["entities"] == 0
        assert summary["files"] == 1  # just the index
        index = (tmp_path / "index.md").read_text()
        assert "0 entities" in index

    def test_entity_without_connections(self, tmp_path):
        entities = [
            Entity(
                id="phone:555",
                entity_type="phone",
                label="555-1234",
                sources=[Source(tool="phoneinfoga")],
            ),
        ]
        exporter = VaultExporter()
        exporter.export_from_data(entities, [], tmp_path, "Test")

        page = (tmp_path / "phone" / "555-1234.md").read_text()
        # Should not have a Connections section if none exist.
        assert "## Connections" not in page

    def test_bidirectional_connections(self, tmp_path):
        """Both source and target of a relationship see the connection."""
        exporter = VaultExporter()
        exporter.export_from_data(
            _make_entities(), _make_relationships(), tmp_path, "Test",
        )
        # Acme Corp is the *target* of works_at from Alice.
        org_page = (tmp_path / "organization" / "Acme Corp.md").read_text()
        assert "### works_at" in org_page
        assert "[[person/Alice Johnson|Alice Johnson]]" in org_page

    def test_yaml_special_chars_escaped(self, tmp_path):
        entities = [
            Entity(
                id="doc:tricky",
                entity_type="document",
                label="Case 12-345",
                properties={"description": "Contains: colons and {braces}"},
                sources=[Source(tool="courtlistener")],
            ),
        ]
        exporter = VaultExporter()
        exporter.export_from_data(entities, [], tmp_path, "Test")

        page = (tmp_path / "document" / "Case 12-345.md").read_text()
        # The colon/braces value should be quoted in frontmatter.
        assert '"Contains: colons and {braces}"' in page
