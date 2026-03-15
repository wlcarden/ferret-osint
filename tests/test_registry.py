"""Tests for the tool registry."""

from osint_agent.tools.registry import ToolRegistry, INPUT_ROUTING


def test_registry_creates_all_adapters():
    registry = ToolRegistry()
    avail = registry.available()
    # Should have entries for all adapters regardless of availability
    assert "maigret" in avail
    assert "holehe" in avail
    assert "edgar" in avail
    assert "courtlistener" in avail
    assert "openfec" in avail
    assert "wayback" in avail
    assert "exiftool" in avail
    assert "phoneinfoga" in avail
    assert "theharvester" in avail


def test_registry_respects_config_disable():
    registry = ToolRegistry(tool_config={"maigret": False})
    assert "maigret" not in registry.available()


def test_registry_get_by_name():
    registry = ToolRegistry()
    adapter = registry.get("maigret")
    assert adapter is not None
    assert adapter.name == "maigret"


def test_registry_get_nonexistent():
    registry = ToolRegistry()
    assert registry.get("nonexistent") is None


def test_input_routing_covers_expected_types():
    expected = {"username", "email", "phone", "domain", "company", "person_name", "url", "ip", "police_agency", "image_file"}
    assert set(INPUT_ROUTING.keys()) == expected


def test_for_input_type_returns_available_only():
    registry = ToolRegistry()
    # Whatever tools are available for username should be a subset of what's registered
    tools = registry.for_input_type("username")
    for tool in tools:
        assert tool.is_available()


def test_summary_output():
    registry = ToolRegistry()
    summary = registry.summary()
    assert "Tool Registry:" in summary
    assert "tools available" in summary
