"""Obsidian vault exporter.

Generates a folder of Markdown files with YAML frontmatter and wikilinks
from the entity/relationship graph, suitable for opening as an Obsidian vault.
"""

from __future__ import annotations

import re
from pathlib import Path

from osint_agent.models import Entity, Relationship, Source


# Characters illegal or problematic in filenames across OS + Obsidian.
_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*#^[\]{}]')
# Collapse multiple whitespace runs.
_COLLAPSE = re.compile(r"\s+")

# Properties to skip in vault pages (too noisy or collide with frontmatter).
_SKIP_PROPS = frozenset({
    "raw_data", "extracted_ids",  # Large unstructured blobs.
    "tags",  # Collides with Obsidian's tags frontmatter key.
})


def _enum_val(v) -> str:
    """Extract string value from an enum or return as-is."""
    return v.value if hasattr(v, "value") else str(v)


def _safe_filename(label: str) -> str:
    """Sanitize a label into a valid, readable filename stem."""
    name = _UNSAFE_CHARS.sub("", label)
    name = _COLLAPSE.sub(" ", name).strip()
    # Truncate long names (Obsidian handles 200+ char filenames poorly).
    if len(name) > 120:
        name = name[:117] + "..."
    return name or "unnamed"


def _yaml_val(v) -> str:
    """Format a value for YAML frontmatter."""
    if isinstance(v, list):
        if not v:
            return "[]"
        # Use flow style for short lists, block for long.
        items = [str(x) for x in v]
        flow = "[" + ", ".join(items) + "]"
        if len(flow) < 80:
            return flow
        return "\n" + "".join(f"  - {x}\n" for x in items).rstrip("\n")
    if isinstance(v, dict):
        return str(v)
    if isinstance(v, bool):
        return "true" if v else "false"
    s = str(v)
    # Quote strings that would confuse YAML parsers.
    if any(c in s for c in ":{}\n[]") or s.startswith(("- ", "# ")):
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


class VaultExporter:
    """Generates an Obsidian vault directory from graph data."""

    async def export(
        self,
        store,
        output_dir: str | Path,
        investigation_name: str = "",
        investigation_id: int | None = None,
    ) -> dict:
        """Query the store and write vault files.

        Returns a summary dict with counts.
        """
        if investigation_id is not None:
            entity_rows = await store.query(f"inv:{investigation_id}:all_nodes")
            rel_rows = await store.query(f"inv:{investigation_id}:all_edges")
        else:
            entity_rows = await store.query("all_nodes")
            rel_rows = await store.query("all_edges")

        entities = [_reconstruct_entity(r) for r in entity_rows]
        relationships = [_reconstruct_relationship(r) for r in rel_rows]

        return self.export_from_data(
            entities, relationships, output_dir, investigation_name,
        )

    def export_from_data(
        self,
        entities: list[Entity],
        relationships: list[Relationship],
        output_dir: str | Path,
        title: str = "",
    ) -> dict:
        """Build vault files from entity/relationship lists (sync, testable)."""
        root = Path(output_dir)

        # --- Build filename registry (unique per entity) ---
        filenames: dict[str, str] = {}  # entity_id → filename stem
        stem_counts: dict[str, int] = {}

        for ent in entities:
            stem = _safe_filename(ent.label)
            stem_counts[stem] = stem_counts.get(stem, 0) + 1

        # Second pass: disambiguate duplicates with type suffix.
        used_stems: dict[str, int] = {}
        for ent in entities:
            stem = _safe_filename(ent.label)
            if stem_counts[stem] > 1:
                candidate = f"{stem} ({_enum_val(ent.entity_type)})"
            else:
                candidate = stem
            # If still colliding (same label + same type), add counter.
            if candidate in used_stems:
                used_stems[candidate] += 1
                candidate = f"{candidate} {used_stems[candidate]}"
            else:
                used_stems[candidate] = 1
            filenames[ent.id] = candidate

        # --- Build adjacency index ---
        # entity_id → list of (relationship, other_entity_id, direction)
        adjacency: dict[str, list[tuple[Relationship, str, str]]] = {
            e.id: [] for e in entities
        }
        entity_map = {e.id: e for e in entities}

        for rel in relationships:
            if rel.source_id in adjacency:
                adjacency[rel.source_id].append((rel, rel.target_id, "→"))
            if rel.target_id in adjacency:
                adjacency[rel.target_id].append((rel, rel.source_id, "←"))

        # --- Write entity pages ---
        type_dirs: set[str] = set()
        for ent in entities:
            etype = _enum_val(ent.entity_type)
            type_dir = root / etype
            type_dirs.add(etype)
            type_dir.mkdir(parents=True, exist_ok=True)

            page = _render_entity_page(
                ent, filenames, adjacency.get(ent.id, []), entity_map,
            )
            (type_dir / f"{filenames[ent.id]}.md").write_text(page)

        # --- Write index MOC ---
        root.mkdir(parents=True, exist_ok=True)
        index = _render_index(
            entities, filenames, title, len(relationships),
        )
        (root / "index.md").write_text(index)

        return {
            "entities": len(entities),
            "relationships": len(relationships),
            "files": len(entities) + 1,  # +1 for index
            "type_folders": sorted(type_dirs),
        }


def _render_entity_page(
    entity: Entity,
    filenames: dict[str, str],
    connections: list[tuple[Relationship, str, str]],
    entity_map: dict[str, Entity],
) -> str:
    """Render a single entity as an Obsidian markdown page."""
    lines: list[str] = []

    # --- YAML frontmatter ---
    lines.append("---")
    lines.append(f"entity_id: {_yaml_val(entity.id)}")
    lines.append(f"type: {_enum_val(entity.entity_type)}")
    lines.append(f"label: {_yaml_val(entity.label)}")

    # Sources as tool list.
    tools = sorted({s.tool for s in entity.sources if s.tool})
    if tools:
        lines.append(f"sources: {_yaml_val(tools)}")

    # Properties.
    for key in sorted(entity.properties):
        if key in _SKIP_PROPS:
            continue
        val = entity.properties[key]
        if val in (None, "", [], {}):
            continue
        lines.append(f"{key}: {_yaml_val(val)}")

    # Tags for Obsidian graph view.
    tags = [_enum_val(entity.entity_type)]
    if tools:
        tags.extend(f"source/{t}" for t in tools)
    lines.append(f"tags: {_yaml_val(tags)}")

    lines.append("---")
    lines.append("")

    # --- Body ---
    lines.append(f"# {entity.label}")
    lines.append("")

    # Properties table.
    if entity.properties:
        lines.append("## Properties")
        lines.append("")
        for key in sorted(entity.properties):
            if key in _SKIP_PROPS:
                continue
            val = entity.properties[key]
            if val in (None, "", [], {}):
                continue
            display = _format_prop_value(val)
            lines.append(f"- **{key}**: {display}")
        lines.append("")

    # Connections with wikilinks, grouped by relationship type.
    if connections:
        lines.append("## Connections")
        lines.append("")

        by_type: dict[str, list[tuple[str, str, str]]] = {}
        for rel, other_id, direction in connections:
            if other_id not in filenames:
                continue
            other = entity_map.get(other_id)
            if not other:
                continue
            rtype = _enum_val(rel.relation_type)
            if rtype not in by_type:
                by_type[rtype] = []
            other_fname = filenames[other_id]
            other_folder = _enum_val(other.entity_type)
            by_type[rtype].append((direction, other_folder, other_fname))

        for rtype in sorted(by_type):
            lines.append(f"### {rtype}")
            lines.append("")
            for direction, folder, fname in sorted(
                by_type[rtype], key=lambda x: x[2].lower(),
            ):
                lines.append(
                    f"- {direction} [[{folder}/{fname}|{fname}]]"
                )
            lines.append("")

    # Source provenance.
    if entity.sources:
        lines.append("## Sources")
        lines.append("")
        for src in entity.sources:
            parts = [src.tool]
            if src.retrieved_at:
                parts.append(str(src.retrieved_at)[:10])
            lines.append(f"- {' | '.join(parts)}")
        lines.append("")

    return "\n".join(lines)


def _render_index(
    entities: list[Entity],
    filenames: dict[str, str],
    title: str,
    rel_count: int,
) -> str:
    """Render the vault index (Map of Content) page."""
    lines: list[str] = []
    lines.append("---")
    lines.append("tags: [MOC]")
    lines.append("---")
    lines.append("")
    lines.append(f"# {title or 'Investigation Vault'}")
    lines.append("")
    lines.append(f"> {len(entities)} entities, {rel_count} relationships")
    lines.append("")

    # Group by type.
    by_type: dict[str, list[Entity]] = {}
    for ent in entities:
        etype = _enum_val(ent.entity_type)
        if etype not in by_type:
            by_type[etype] = []
        by_type[etype].append(ent)

    for etype in sorted(by_type):
        group = sorted(by_type[etype], key=lambda e: e.label.lower())
        lines.append(f"## {etype} ({len(group)})")
        lines.append("")
        for ent in group:
            fname = filenames[ent.id]
            lines.append(f"- [[{etype}/{fname}|{ent.label}]]")
        lines.append("")

    return "\n".join(lines)


def _format_prop_value(val) -> str:
    """Format a property value for markdown display."""
    if isinstance(val, list):
        return ", ".join(str(x) for x in val)
    if isinstance(val, dict):
        return str(val)
    s = str(val)
    if s.startswith(("http://", "https://")):
        display = s if len(s) <= 60 else s[:57] + "..."
        return f"[{display}]({s})"
    return s


# --- Reconstruction helpers (mirror report.py) ---

def _reconstruct_entity(row: dict) -> Entity:
    """Build an Entity from a store query row."""
    props = {
        k: v for k, v in row.items()
        if k not in ("id", "entity_type", "label", "sources")
        and v not in (None, "", [], {})
    }
    sources = [
        Source(tool=s.get("tool", "unknown"), collected_at=s.get("collected_at"))
        for s in row.get("sources", [])
    ]
    return Entity(
        id=row["id"],
        entity_type=row["entity_type"],
        label=row["label"],
        properties=props,
        sources=sources,
    )


def _reconstruct_relationship(row: dict) -> Relationship:
    """Build a Relationship from a store query row."""
    props = {
        k: v for k, v in row.items()
        if k not in ("source", "target", "relation_type", "source_id", "target_id")
        and v not in (None, "", [], {})
    }
    return Relationship(
        source_id=row.get("source_id", row.get("source")),
        target_id=row.get("target_id", row.get("target")),
        relation_type=row["relation_type"],
        properties=props,
    )
