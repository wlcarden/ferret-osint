"""Tests for theme — enum coverage, data integrity, JS serializers."""

import json
import re

from osint_agent.models import EntityType, RelationType
from osint_agent.theme import (
    CATPPUCCIN,
    REL_COLORS,
    TIMELINE_EXTRA_COLORS,
    TYPE_COLORS,
    TYPE_SHAPES,
    rel_colors_js,
    type_colors_js,
    type_shapes_js,
)

HEX_RE = re.compile(r"^#[0-9a-f]{6}$")


# ------------------------------------------------------------------
# Enum coverage
# ------------------------------------------------------------------


def test_type_colors_covers_all_entity_types():
    """should have a color for every EntityType enum value"""
    for et in EntityType:
        assert et.value in TYPE_COLORS, f"missing TYPE_COLORS entry for {et.value}"


def test_type_shapes_covers_all_entity_types():
    """should have a shape for every EntityType enum value"""
    for et in EntityType:
        assert et.value in TYPE_SHAPES, f"missing TYPE_SHAPES entry for {et.value}"


def test_rel_colors_covers_all_relation_types():
    """should have a color for every RelationType enum value"""
    for rt in RelationType:
        assert rt.value in REL_COLORS, f"missing REL_COLORS entry for {rt.value}"


def test_type_colors_has_no_extra_keys():
    """should not have keys that aren't in EntityType"""
    valid = {et.value for et in EntityType}
    for key in TYPE_COLORS:
        assert key in valid, f"TYPE_COLORS has unknown key '{key}'"


def test_type_shapes_has_no_extra_keys():
    """should not have keys that aren't in EntityType"""
    valid = {et.value for et in EntityType}
    for key in TYPE_SHAPES:
        assert key in valid, f"TYPE_SHAPES has unknown key '{key}'"


def test_rel_colors_has_no_extra_keys():
    """should not have keys that aren't in RelationType"""
    valid = {rt.value for rt in RelationType}
    for key in REL_COLORS:
        assert key in valid, f"REL_COLORS has unknown key '{key}'"


# ------------------------------------------------------------------
# Hex format validation
# ------------------------------------------------------------------


def test_type_color_values_are_valid_hex():
    """should use lowercase 6-digit hex for all entity colors"""
    for key, val in TYPE_COLORS.items():
        assert HEX_RE.match(val), f"TYPE_COLORS['{key}'] = '{val}' is not valid hex"


def test_rel_color_values_are_valid_hex():
    """should use lowercase 6-digit hex for all relationship colors"""
    for key, val in REL_COLORS.items():
        assert HEX_RE.match(val), f"REL_COLORS['{key}'] = '{val}' is not valid hex"


def test_catppuccin_values_are_valid_hex():
    """should use lowercase 6-digit hex for all palette entries"""
    for key, val in CATPPUCCIN.items():
        assert HEX_RE.match(val), f"CATPPUCCIN['{key}'] = '{val}' is not valid hex"


def test_timeline_extra_colors_are_valid_hex():
    """should use lowercase 6-digit hex for all extra color entries"""
    for key, val in TIMELINE_EXTRA_COLORS.items():
        assert HEX_RE.match(val), f"TIMELINE_EXTRA_COLORS['{key}'] = '{val}' is not valid hex"


# ------------------------------------------------------------------
# JS serializers
# ------------------------------------------------------------------


def test_type_colors_js_is_valid_json():
    """should produce parseable JSON"""
    data = json.loads(type_colors_js())
    assert isinstance(data, dict)
    assert "person" in data


def test_type_colors_js_includes_extras():
    """should merge TIMELINE_EXTRA_COLORS into output"""
    data = json.loads(type_colors_js())
    for key in TIMELINE_EXTRA_COLORS:
        assert key in data, f"type_colors_js() missing extra key '{key}'"


def test_type_shapes_js_is_valid_json():
    """should produce parseable JSON"""
    data = json.loads(type_shapes_js())
    assert isinstance(data, dict)
    assert data["person"] == "ellipse"


def test_rel_colors_js_is_valid_json():
    """should produce parseable JSON"""
    data = json.loads(rel_colors_js())
    assert isinstance(data, dict)
    assert "has_email" in data


def test_type_shapes_js_covers_type_colors():
    """should have a shape for every type in TYPE_COLORS"""
    shapes = json.loads(type_shapes_js())
    for key in TYPE_COLORS:
        assert key in shapes, f"type_shapes_js() missing key '{key}' that is in TYPE_COLORS"
