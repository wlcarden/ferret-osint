"""Tests for the Cytoscape.js graph exporter."""

from osint_agent.graph_export import GraphExporter, _trunc


def _sample_entities():
    return [
        {
            "id": "person:john_doe",
            "entity_type": "person",
            "label": "John Doe",
            "sources": [{"tool": "ddg_search"}],
            "url": "https://example.com/john",
        },
        {
            "id": "org:acme",
            "entity_type": "organization",
            "label": "Acme Corp",
            "sources": [{"tool": "usaspending"}, {"tool": "ddg_search"}],
        },
        {
            "id": "account:github:johndoe",
            "entity_type": "account",
            "label": "johndoe on GitHub",
            "sources": [{"tool": "maigret"}],
            "platform": "GitHub",
            "url": "https://github.com/johndoe",
        },
    ]


def _sample_relationships():
    return [
        {
            "source": "person:john_doe",
            "target": "org:acme",
            "relation_type": "works_at",
            "sources": [{"tool": "ddg_search"}],
        },
        {
            "source": "person:john_doe",
            "target": "account:github:johndoe",
            "relation_type": "has_account",
            "sources": [{"tool": "maigret"}],
            "platform": "GitHub",
        },
    ]


def test_export_produces_html():
    exporter = GraphExporter()
    html = exporter.export_from_data(
        _sample_entities(), _sample_relationships(), "Test Graph",
    )
    assert "<!DOCTYPE html>" in html
    assert "cytoscape" in html
    assert "Test Graph" in html


def test_export_embeds_entity_data():
    exporter = GraphExporter()
    html = exporter.export_from_data(
        _sample_entities(), _sample_relationships(),
    )
    assert "John Doe" in html
    assert "Acme Corp" in html
    assert "johndoe on GitHub" in html


def test_export_embeds_relationship_types():
    exporter = GraphExporter()
    html = exporter.export_from_data(
        _sample_entities(), _sample_relationships(),
    )
    assert "works_at" in html
    assert "has_account" in html


def test_export_includes_entity_types():
    exporter = GraphExporter()
    html = exporter.export_from_data(
        _sample_entities(), _sample_relationships(),
    )
    assert '"person"' in html
    assert '"organization"' in html
    assert '"account"' in html


def test_export_includes_source_tools():
    exporter = GraphExporter()
    html = exporter.export_from_data(
        _sample_entities(), _sample_relationships(),
    )
    assert "ddg_search" in html
    assert "maigret" in html
    assert "usaspending" in html


def test_dangling_edges_filtered():
    """Edges referencing non-existent nodes should be excluded."""
    entities = [_sample_entities()[0]]  # Only John Doe
    rels = _sample_relationships()  # Both rels, but Acme and GitHub don't exist
    exporter = GraphExporter()
    html = exporter.export_from_data(entities, rels)
    # The edge targets (org:acme, account:github:johndoe) don't exist as nodes,
    # so the edges should not appear in the graph data.
    # Note: "works_at" appears in REL_COLORS constant, so check the data payload.
    assert "org:acme" not in html
    assert "account:github:johndoe" not in html


def test_empty_graph():
    exporter = GraphExporter()
    html = exporter.export_from_data([], [])
    assert "<!DOCTYPE html>" in html
    assert "cytoscape" in html


def test_properties_included():
    exporter = GraphExporter()
    html = exporter.export_from_data(
        _sample_entities(), _sample_relationships(),
    )
    assert "https://github.com/johndoe" in html
    assert "GitHub" in html


def test_raw_data_excluded():
    """raw_data should not appear in the export to keep JSON small."""
    entities = [{
        "id": "person:test",
        "entity_type": "person",
        "label": "Test",
        "sources": [{"tool": "test"}],
        "raw_data": {"huge": "payload" * 100},
    }]
    exporter = GraphExporter()
    html = exporter.export_from_data(entities, [])
    assert "payload" not in html


def test_extracted_ids_excluded():
    """extracted_ids should not appear in the export."""
    entities = [{
        "id": "account:test",
        "entity_type": "account",
        "label": "Test",
        "sources": [{"tool": "maigret"}],
        "extracted_ids": {"uid": "12345", "follower_count": "99"},
    }]
    exporter = GraphExporter()
    html = exporter.export_from_data(entities, [])
    assert "follower_count" not in html


def test_html_title_escaped():
    exporter = GraphExporter()
    html = exporter.export_from_data([], [], title='<script>alert("xss")</script>')
    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html


def test_trunc_short():
    assert _trunc("hello", 10) == "hello"


def test_trunc_exact():
    assert _trunc("hello", 5) == "hello"


def test_trunc_long():
    result = _trunc("a very long string here", 10)
    assert len(result) == 10
    assert result.endswith("\u2026")


def test_default_title():
    exporter = GraphExporter()
    html = exporter.export_from_data(_sample_entities(), [])
    assert "OSINT Investigation Graph" in html


def test_node_properties_skip_empty():
    """Empty/null properties should not appear in node data."""
    entities = [{
        "id": "person:test",
        "entity_type": "person",
        "label": "Test",
        "sources": [],
        "url": "",
        "tags": [],
        "platform": None,
        "rank": 42,
    }]
    exporter = GraphExporter()
    html = exporter.export_from_data(entities, [])
    # rank should appear (non-empty), but url/tags/platform should not
    assert '"rank":42' in html or '"rank": 42' in html
