"""Canonical visual constants for the OSINT toolkit.

Single source of truth for entity-type colors, relationship colors,
Cytoscape node shapes, and the Catppuccin Mocha palette.  Consumed by
HTML template renderers (graph_export, timeline) via JS serializers
and by the Rich CLI layer (console.py) for terminal output.
"""

from __future__ import annotations

import json

# ── Catppuccin Mocha palette ──────────────────────────────────────

CATPPUCCIN = {
    "rosewater": "#f5e0dc",
    "flamingo": "#f2cdcd",
    "pink": "#f5c2e7",
    "mauve": "#cba6f7",
    "red": "#f38ba8",
    "maroon": "#eba0ac",
    "peach": "#fab387",
    "yellow": "#f9e2af",
    "green": "#a6e3a1",
    "teal": "#94e2d5",
    "sky": "#89dceb",
    "sapphire": "#74c7ec",
    "blue": "#89b4fa",
    "lavender": "#b4befe",
    "text": "#cdd6f4",
    "subtext1": "#bac2de",
    "subtext0": "#a6adc8",
    "overlay2": "#9399b2",
    "overlay1": "#7f849c",
    "overlay0": "#6c7086",
    "surface2": "#585b70",
    "surface1": "#45475a",
    "surface0": "#313244",
    "base": "#1e1e2e",
    "mantle": "#181825",
    "crust": "#11111b",
}


# ── Entity type → hex color ──────────────────────────────────────

TYPE_COLORS: dict[str, str] = {
    "person": "#89b4fa",         # blue
    "organization": "#f38ba8",   # red
    "account": "#a6e3a1",        # green
    "document": "#74c7ec",       # sapphire
    "domain": "#cba6f7",         # mauve
    "email": "#fab387",          # peach
    "username": "#94e2d5",       # teal
    "phone": "#f9e2af",          # yellow
    "address": "#eba0ac",        # maroon
    "property": "#f2cdcd",       # flamingo
    "vehicle": "#b4befe",        # lavender
    "event": "#f5e0dc",          # rosewater
    "location": "#89dceb",       # sky
}


# ── Cytoscape node shapes (graph_export only) ────────────────────

TYPE_SHAPES: dict[str, str] = {
    "person": "ellipse",
    "organization": "round-rectangle",
    "account": "ellipse",
    "document": "rectangle",
    "domain": "diamond",
    "email": "ellipse",
    "username": "ellipse",
    "phone": "ellipse",
    "address": "round-rectangle",
    "property": "round-rectangle",
    "vehicle": "round-rectangle",
    "event": "star",
    "location": "diamond",
}


# ── Relationship type → edge hex color ───────────────────────────

REL_COLORS: dict[str, str] = {
    # Identity
    "has_email": "#585b70",
    "has_phone": "#585b70",
    "has_username": "#585b70",
    "has_account": "#585b70",
    "has_address": "#585b70",
    "also_known_as": "#89b4fa",
    # Organizational
    "works_at": "#f38ba8",
    "officer_of": "#f38ba8",
    "owns": "#f38ba8",
    "controls": "#f38ba8",
    "affiliated_with": "#f38ba8",
    # Financial
    "donated_to": "#fab387",
    "transacted_with": "#fab387",
    # Legal
    "party_to": "#eba0ac",
    "filed": "#eba0ac",
    # Social
    "follows": "#a6e3a1",
    "connected_to": "#a6e3a1",
    "mentioned": "#585b70",
    # Spatial
    "participated_in": "#89b4fa",
    "occurred_at": "#f5e0dc",
    "located_at": "#89dceb",
    # Temporal
    "preceded_by": "#9399b2",
}


# ── Extended type colors for HTML timeline ───────────────────────
# The timeline may encounter entity types not in EntityType (e.g.
# "fec_committee" from adapter-specific IDs, "investigation" for
# activity events).  These extras supplement TYPE_COLORS in timeline
# templates only.

TIMELINE_EXTRA_COLORS: dict[str, str] = {
    "fec_committee": "#f38ba8",
    "ip_address": "#eba0ac",
    "url": "#89dceb",
    "investigation": "#6c7086",
}


# ── JS serializers for HTML template injection ───────────────────

def type_colors_js() -> str:
    """TYPE_COLORS as a JS object literal (including timeline extras)."""
    merged = {**TYPE_COLORS, **TIMELINE_EXTRA_COLORS}
    return json.dumps(merged, separators=(",", ":"))


def type_shapes_js() -> str:
    """TYPE_SHAPES as a JS object literal."""
    return json.dumps(TYPE_SHAPES, separators=(",", ":"))


def rel_colors_js() -> str:
    """REL_COLORS as a JS object literal."""
    return json.dumps(REL_COLORS, separators=(",", ":"))
