"""Structured report generator with corroboration-aware attribution.

Reads investigation data (entities, relationships, leads) and produces
a markdown report that surfaces:
  - Canonical subject profiles with confidence badges
  - Corroboration evidence for every cross-source link
  - Rejected candidates that failed corroboration (and why)
  - Source provenance for every claim

Designed to prevent the kind of false attribution that occurs when
name-only matches are narrated as fact.
"""

from collections import defaultdict
from datetime import UTC, datetime

from osint_agent.graph.corroboration import CorroborationPolicy
from osint_agent.graph.resolver import (
    EntityResolver,
    _cross_source_pairs,
    _extract_source,
    _normalize_for_blocking,
    _pair_key,
)
from osint_agent.models import Entity, EntityType, Relationship, RelationType, Source


def _reconstruct_entity(row: dict) -> Entity:
    """Reconstruct an Entity from a SqliteStore query result dict.

    Store dicts flatten properties into top-level keys alongside
    id, entity_type, label, sources. This reverses that.
    """
    meta_keys = {"id", "entity_type", "label", "sources"}
    properties = {k: v for k, v in row.items() if k not in meta_keys}
    sources = []
    for s in row.get("sources", []):
        sources.append(Source(
            tool=s.get("tool", "unknown"),
            source_url=s.get("source_url"),
            confidence=s.get("confidence", 1.0),
        ))
    return Entity(
        id=row["id"],
        entity_type=EntityType(row["entity_type"]),
        label=row["label"],
        properties=properties,
        sources=sources,
    )


def _reconstruct_relationship(row: dict) -> Relationship:
    """Reconstruct a Relationship from a SqliteStore query result dict."""
    meta_keys = {"source", "target", "relation_type", "sources"}
    properties = {k: v for k, v in row.items() if k not in meta_keys}
    sources = []
    for s in row.get("sources", []):
        sources.append(Source(
            tool=s.get("tool", "unknown"),
            source_url=s.get("source_url"),
            confidence=s.get("confidence", 1.0),
        ))
    return Relationship(
        source_id=row["source"],
        target_id=row["target"],
        relation_type=RelationType(row["relation_type"]),
        properties=properties,
        sources=sources,
    )


class ReportGenerator:
    """Generates structured investigation reports with corroboration evidence.

    Works with either a SqliteStore (async) or raw entity/relationship
    lists (sync, for testing).
    """

    def __init__(
        self,
        resolver: EntityResolver | None = None,
        policy: CorroborationPolicy | None = None,
    ):
        self.resolver = resolver or EntityResolver()
        self.policy = policy or self.resolver.policy

    async def generate(
        self,
        store,
        investigation_id: int | None = None,
        investigation_name: str = "",
    ) -> str:
        """Generate a report from SqliteStore data."""
        if investigation_id is not None:
            entity_rows = await store.query(f"inv:{investigation_id}:all_nodes")
            rel_rows = await store.query(f"inv:{investigation_id}:all_edges")
        else:
            entity_rows = await store.query("all_nodes")
            rel_rows = await store.query("all_edges")
        leads = await store.get_leads(investigation_id=investigation_id)

        entities = [_reconstruct_entity(r) for r in entity_rows]
        relationships = [_reconstruct_relationship(r) for r in rel_rows]

        return self.generate_from_data(
            entities=entities,
            relationships=relationships,
            leads=leads,
            investigation_name=investigation_name,
        )

    def generate_from_data(
        self,
        entities: list[Entity],
        relationships: list[Relationship],
        leads: list[dict] | None = None,
        investigation_name: str = "",
    ) -> str:
        """Generate a report from in-memory data."""
        leads = leads or []

        # Only treat person-to-person AKA links as corroboration evidence.
        # Non-person AKA links (e.g. account↔username from Maigret) are
        # structural and don't carry corroboration metadata.
        person_ids = {
            e.id for e in entities if e.entity_type == EntityType.PERSON
        }
        aka_rels = [
            r for r in relationships
            if r.relation_type == RelationType.ALSO_KNOWN_AS
            and r.source_id in person_ids
            and r.target_id in person_ids
        ]
        other_rels = [
            r for r in relationships
            if r.relation_type != RelationType.ALSO_KNOWN_AS
            or r.source_id not in person_ids
            or r.target_id not in person_ids
        ]

        sections = [
            self._render_header(investigation_name),
            self._render_toc(entities, aka_rels, other_rels, leads),
            self._render_summary(entities, relationships, aka_rels, leads),
            self._render_subject_profiles(entities, aka_rels),
            self._render_attribution(entities, aka_rels),
            self._render_rejected_candidates(entities, aka_rels),
            self._render_entities_by_type(entities, aka_rels),
            self._render_relationships(other_rels, entities),
            self._render_leads(leads),
            self._render_source_index(entities),
        ]

        # Join non-empty sections with horizontal rules
        non_empty = [s for s in sections if s]
        return "\n\n---\n\n".join(non_empty)

    # ── Section renderers ───────────────────────────────────────────

    def _render_header(self, name: str) -> str:
        now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
        title = (
            f"# Investigation Report: {name}"
            if name else "# Investigation Report"
        )
        return f"{title}\n\n*Generated: {now}*"

    def _render_toc(
        self,
        entities: list[Entity],
        aka_rels: list[Relationship],
        other_rels: list[Relationship],
        leads: list[dict],
    ) -> str:
        """Render a table of contents linking to report sections."""
        items = ["- [Executive Summary](#executive-summary)"]
        persons = [
            e for e in entities
            if e.entity_type == EntityType.PERSON
        ]
        if persons:
            items.append(
                "- [Subject Profiles](#subject-profiles)",
            )
        if aka_rels:
            items.append(
                "- [Entity Attribution](#entity-attribution)",
            )
        if entities:
            items.append(
                "- [Entities by Type](#entities-by-type)",
            )
        if other_rels:
            items.append("- [Relationships](#relationships)")
        if leads:
            items.append("- [Lead Queue](#lead-queue)")
        items.append("- [Source Index](#source-index)")
        return "\n".join(items)

    def _render_summary(
        self,
        entities: list[Entity],
        relationships: list[Relationship],
        aka_rels: list[Relationship],
        leads: list[dict],
    ) -> str:
        sources = set()
        for e in entities:
            for s in e.sources:
                sources.add(s.tool)

        confirmed = sum(
            1 for r in aka_rels
            if r.properties.get("corroboration_level") == "confirmed"
        )
        probable = sum(
            1 for r in aka_rels
            if r.properties.get("corroboration_level") == "probable"
        )
        pending = sum(1 for l in leads if l.get("status") == "pending")

        lines = [
            "## Executive Summary",
            "",
            f"- **{len(entities)}** entities across **{len(sources)}** sources",
            f"- **{len(aka_rels)}** cross-source links"
            f" ({confirmed} confirmed, {probable} probable)",
            f"- **{len(leads)}** leads ({pending} pending)",
        ]
        return "\n".join(lines)

    def _render_subject_profiles(
        self,
        entities: list[Entity],
        aka_rels: list[Relationship],
    ) -> str:
        """Render canonical profiles for person entities with confidence badges."""
        persons = [e for e in entities if e.entity_type == EntityType.PERSON]
        if not persons:
            return ""

        # Find person entities that are part of AKA clusters
        linked_ids = set()
        for rel in aka_rels:
            linked_ids.add(rel.source_id)
            linked_ids.add(rel.target_id)

        # Build clusters: sets of entity IDs connected by AKA links
        clusters = self._build_aka_clusters(aka_rels)

        # Also include unlinked persons as singleton clusters
        clustered_ids = set()
        for cluster in clusters:
            clustered_ids.update(cluster)
        for p in persons:
            if p.id not in clustered_ids:
                clusters.append({p.id})

        if not clusters:
            return ""

        entity_map = {e.id: e for e in entities}
        lines = ["## Subject Profiles"]

        for cluster in clusters:
            cluster_entities = [
                entity_map[eid] for eid in cluster if eid in entity_map
            ]
            person_entities = [
                e for e in cluster_entities
                if e.entity_type == EntityType.PERSON
            ]
            if not person_entities:
                continue

            profile = self.resolver.get_canonical_profile(
                entity_id=person_entities[0].id,
                entities=entities,
                aka_relationships=aka_rels,
            )

            # Determine overall confidence from AKA links in this cluster
            cluster_rels = [
                r for r in aka_rels
                if r.source_id in cluster and r.target_id in cluster
            ]
            confidence_badge = self._confidence_badge(cluster_rels)

            lines.append("")
            lines.append(
                f"### {profile.get('label', 'Unknown')}"
            )
            lines.append(f"**{confidence_badge}**")
            lines.append("")

            # Properties table
            props = profile.get("merged_properties", {})
            if props:
                lines.append("| Field | Value |")
                lines.append("|-------|-------|")
                for key, value in sorted(props.items()):
                    if value:
                        lines.append(f"| {key} | {value} |")
                lines.append("")

            # Sources
            source_tools = set()
            for e in person_entities:
                for s in e.sources:
                    source_tools.add(s.tool)
            if source_tools:
                lines.append(f"*Sources: {', '.join(sorted(source_tools))}*")

            # Aliases
            aliases = profile.get("aliases", [])
            if aliases:
                lines.append("")
                lines.append("**Linked entity IDs:**")
                for alias in aliases:
                    lines.append(f"- `{alias}`")

        return "\n".join(lines)

    def _render_attribution(
        self,
        entities: list[Entity],
        aka_rels: list[Relationship],
    ) -> str:
        """Render corroboration evidence for each cross-source link."""
        if not aka_rels:
            return ""

        lines = ["## Entity Attribution"]

        # Group by corroboration level
        confirmed = [
            r for r in aka_rels
            if r.properties.get("corroboration_level") == "confirmed"
        ]
        probable = [
            r for r in aka_rels
            if r.properties.get("corroboration_level") == "probable"
        ]
        other = [
            r for r in aka_rels
            if r.properties.get("corroboration_level") not in (
                "confirmed", "probable",
            )
        ]

        if confirmed:
            lines.append("")
            lines.append("### Confirmed Links")
            for rel in confirmed:
                lines.extend(self._render_single_attribution(rel))

        if probable:
            lines.append("")
            lines.append("### Probable Links")
            for rel in probable:
                lines.extend(self._render_single_attribution(rel))

        if other:
            lines.append("")
            lines.append("### Other Links")
            for rel in other:
                lines.extend(self._render_single_attribution(rel))

        return "\n".join(lines)

    def _render_single_attribution(self, rel: Relationship) -> list[str]:
        """Render a single ALSO_KNOWN_AS link with corroboration details."""
        props = rel.properties
        src_label = props.get("source_label", rel.source_id)
        tgt_label = props.get("target_label", rel.target_id)
        confidence = props.get("confidence", 0)
        level = props.get("corroboration_level", "unknown")
        weight = props.get("corroboration_weight", 0)
        method = props.get("method", "unknown")

        lines = [
            "",
            f"**{src_label}** ({_extract_source(rel.source_id)})"
            f" ↔ **{tgt_label}** ({_extract_source(rel.target_id)})"
            f" — {level.upper()} ({confidence:.0%})",
        ]

        factors = props.get("corroboration_factors", [])
        if factors:
            lines.append(f"  - Total weight: {weight}")
            for f in factors:
                lines.append(
                    f"  - {f['field']}: {f['category']} ({f['weight']})"
                )
        elif method == "entity_resolution":
            lines.append(f"  - Method: name similarity scoring (confidence: {confidence:.0%})")

        return lines

    def _render_rejected_candidates(
        self,
        entities: list[Entity],
        aka_rels: list[Relationship],
    ) -> str:
        """Find same-name person entities that failed corroboration and explain why.

        Groups rejections by normalized name to avoid repeating the same
        explanation for every pairwise combination (e.g. 1 courtlistener
        entity × 6 peoplesearch entities = 6 identical entries).
        """
        persons = [e for e in entities if e.entity_type == EntityType.PERSON]
        if len(persons) < 2:
            return ""

        # Build set of linked pairs
        linked_pairs: set[tuple[str, str]] = set()
        for rel in aka_rels:
            linked_pairs.add(_pair_key(rel.source_id, rel.target_id))

        # Group by normalized name
        name_groups: dict[str, list[Entity]] = defaultdict(list)
        for e in persons:
            key = _normalize_for_blocking(e.label)
            if key:
                name_groups[key].append(e)

        # Collect rejections grouped by name key
        rejection_groups: dict[str, list[tuple[Entity, Entity, object]]] = (
            defaultdict(list)
        )

        for key, group in name_groups.items():
            if len(group) < 2:
                continue
            cross_pairs = _cross_source_pairs(group)
            for e1, e2 in cross_pairs:
                pk = _pair_key(e1.id, e2.id)
                if pk in linked_pairs:
                    continue
                result = self.policy.evaluate(e1, e2, name_similarity=1.0)
                rejection_groups[key].append((e1, e2, result))

        if not rejection_groups:
            return ""

        lines = [
            "## Rejected Candidates",
            "",
            "These entity pairs share a name but did not meet the "
            "corroboration threshold for linking.",
        ]

        for key, rejections in sorted(rejection_groups.items()):
            e1, e2, result = rejections[0]

            sources_in_group = set()
            for r_e1, r_e2, _ in rejections:
                sources_in_group.add(_extract_source(r_e1.id))
                sources_in_group.add(_extract_source(r_e2.id))

            lines.append("")
            pair_count = len(rejections)
            if pair_count == 1:
                lines.append(
                    f"> **{e1.label}** ({_extract_source(e1.id)})"
                    f" <-> **{e2.label}** ({_extract_source(e2.id)})"
                    f" -- NOT LINKED (weight: {result.total_weight},"
                    f" threshold: {self.policy.probable_threshold})",
                )
            else:
                lines.append(
                    f"> **\"{key}\"** -- {pair_count} cross-source"
                    f" pairs NOT LINKED"
                    f" (best weight: {result.total_weight},"
                    f" threshold: {self.policy.probable_threshold})",
                )
                lines.append(
                    f"> Sources:"
                    f" {', '.join(sorted(sources_in_group))}",
                )

            if result.factors:
                lines.append("> Factors present:")
                for f in result.factors:
                    lines.append(
                        f">   - {f.field}:"
                        f" {f.category} ({f.weight})",
                    )
            else:
                lines.append(
                    "> No corroborating factors found",
                )

            missing = self._identify_missing_factors(
                e1, e2, result,
            )
            if missing:
                lines.append(f"> Missing: {missing}")

        return "\n".join(lines)

    def _render_entities_by_type(
        self,
        entities: list[Entity],
        aka_rels: list[Relationship] | None = None,
    ) -> str:
        """Render entities grouped by type, sorted by corroboration strength.

        Entities with more sources appear first within each type group.
        Inline badges indicate confidence level.
        """
        if not entities:
            return ""

        # Build lookup: entity_id → set of AKA-linked entity IDs
        linked_ids: set[str] = set()
        corroboration_levels: dict[str, str] = {}
        if aka_rels:
            for rel in aka_rels:
                linked_ids.add(rel.source_id)
                linked_ids.add(rel.target_id)
                level = rel.properties.get("corroboration_level", "")
                # Store the best level for each entity
                for eid in (rel.source_id, rel.target_id):
                    existing = corroboration_levels.get(eid, "")
                    if level == "confirmed" or (level == "probable" and existing != "confirmed"):
                        corroboration_levels[eid] = level

        by_type: dict[str, list[Entity]] = defaultdict(list)
        for e in entities:
            by_type[e.entity_type.value].append(e)

        lines = ["## Entities by Type"]
        for etype in sorted(by_type.keys()):
            group = by_type[etype]
            # Sort: confirmed first, then probable, then by source count desc, then alphabetical
            group.sort(key=lambda e: (
                0 if corroboration_levels.get(e.id) == "confirmed"
                else 1 if corroboration_levels.get(e.id) == "probable"
                else 2 if len(e.sources) > 1
                else 3,
                -len(e.sources),
                e.label.lower(),
            ))

            lines.append("")
            lines.append(f"### {etype.title()} ({len(group)})")
            lines.append("")
            for e in group:
                source_tools = ", ".join(s.tool for s in e.sources)
                detail = ""
                if e.properties.get("url"):
                    detail = f" — {e.properties['url']}"
                elif e.properties.get("platform"):
                    detail = f" ({e.properties['platform']})"

                # Confidence badge
                level = corroboration_levels.get(e.id, "")
                if level == "confirmed":
                    badge = " `CONFIRMED`"
                elif level == "probable":
                    badge = " `PROBABLE`"
                elif len(e.sources) > 1:
                    badge = f" `{len(e.sources)} sources`"
                else:
                    badge = ""

                lines.append(f"- **{e.label}**{badge}{detail} *[{source_tools}]*")

        return "\n".join(lines)

    def _render_relationships(
        self,
        relationships: list[Relationship],
        entities: list[Entity],
    ) -> str:
        """Render non-AKA relationships grouped by type."""
        if not relationships:
            return ""

        entity_map = {e.id: e for e in entities}
        by_type: dict[str, list[Relationship]] = defaultdict(list)
        for r in relationships:
            by_type[r.relation_type.value].append(r)

        lines = ["## Relationships"]
        for rtype in sorted(by_type.keys()):
            group = by_type[rtype]
            lines.append("")
            lines.append(f"### {rtype} ({len(group)})")
            lines.append("")
            for r in group[:50]:
                src = entity_map.get(r.source_id)
                tgt = entity_map.get(r.target_id)
                src_label = src.label if src else r.source_id
                tgt_label = tgt.label if tgt else r.target_id
                # Show key properties inline
                prop_parts = []
                for k, v in sorted(r.properties.items()):
                    if k in ("raw_data",) or v is None:
                        continue
                    prop_parts.append(f"{k}: {v}")
                props_str = (
                    f" ({', '.join(prop_parts)})"
                    if prop_parts else ""
                )
                lines.append(
                    f"- {src_label} -> {tgt_label}{props_str}",
                )
            if len(group) > 50:
                lines.append(f"- *... and {len(group) - 50} more*")

        return "\n".join(lines)

    def _render_leads(self, leads: list[dict]) -> str:
        """Render the lead queue as a table."""
        if not leads:
            return ""

        lines = [
            "## Lead Queue",
            "",
            "| Priority | Type | Value | Status | Notes |",
            "|----------|------|-------|--------|-------|",
        ]
        for lead in sorted(leads, key=lambda l: -l.get("score", 0)):
            score = lead.get("score", 0)
            lines.append(
                f"| {score:.1f} | {lead.get('lead_type', '')} "
                f"| {lead.get('value', '')} | {lead.get('status', '')} "
                f"| {lead.get('notes', '')} |"
            )

        return "\n".join(lines)

    def _render_source_index(self, entities: list[Entity]) -> str:
        """Render a source provenance index."""
        if not entities:
            return ""

        tool_entities: dict[str, int] = defaultdict(int)
        tool_urls: dict[str, set[str]] = defaultdict(set)
        for e in entities:
            for s in e.sources:
                tool_entities[s.tool] += 1
                if s.source_url:
                    tool_urls[s.tool].add(s.source_url)

        lines = [
            "## Source Index",
            "",
            "| Tool | Entities | Sample URLs |",
            "|------|----------|-------------|",
        ]
        for tool in sorted(tool_entities.keys()):
            count = tool_entities[tool]
            urls = sorted(tool_urls.get(tool, set()))
            url_sample = ", ".join(urls[:3])
            if len(urls) > 3:
                url_sample += f" (+{len(urls) - 3} more)"
            lines.append(f"| {tool} | {count} | {url_sample} |")

        return "\n".join(lines)

    # ── Helpers ──────────────────────────────────────────────────────

    def _build_aka_clusters(
        self,
        aka_rels: list[Relationship],
    ) -> list[set[str]]:
        """Build connected components from ALSO_KNOWN_AS relationships.

        Uses union-find to group entity IDs that are transitively linked.
        """
        parent: dict[str, str] = {}

        def find(x: str) -> str:
            if x not in parent:
                parent[x] = x
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: str, b: str) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        for rel in aka_rels:
            union(rel.source_id, rel.target_id)

        clusters: dict[str, set[str]] = defaultdict(set)
        for eid in parent:
            clusters[find(eid)].add(eid)

        return list(clusters.values())

    def _confidence_badge(self, cluster_rels: list[Relationship]) -> str:
        """Generate a confidence badge for a cluster of AKA links."""
        if not cluster_rels:
            return "Single source"

        levels = [
            r.properties.get("corroboration_level", "unknown")
            for r in cluster_rels
        ]
        confidences = [
            r.properties.get("confidence", 0)
            for r in cluster_rels
        ]

        if all(l == "confirmed" for l in levels):
            label = "CONFIRMED"
        elif any(l == "confirmed" for l in levels):
            label = "CONFIRMED / PROBABLE"
        elif any(l == "probable" for l in levels):
            label = "PROBABLE"
        else:
            label = "LOW CONFIDENCE"

        source_count = len(cluster_rels) + 1
        min_conf = min(confidences) if confidences else 0
        return f"{label} — {source_count} corroborating sources (min confidence: {min_conf:.0%})"

    def _identify_missing_factors(
        self,
        e1: Entity,
        e2: Entity,
        result,
    ) -> str:
        """Describe what corroborating evidence is missing."""
        present_fields = {f.field for f in result.factors}
        missing_parts = []

        if "email" not in present_fields:
            has_email = bool(
                e1.properties.get("email") or e2.properties.get("email")
            )
            if has_email:
                missing_parts.append("email mismatch")
            else:
                missing_parts.append("no email data")

        if "phone" not in present_fields and "phone_number" not in present_fields:
            missing_parts.append("no phone data")

        if "employer" not in present_fields and "company" not in present_fields:
            has_emp = bool(
                e1.properties.get("employer") or e2.properties.get("employer")
                or e1.properties.get("company") or e2.properties.get("company")
            )
            if has_emp:
                missing_parts.append("employer mismatch")
            else:
                missing_parts.append("no employer data")

        if not missing_parts:
            missing_parts.append("no unique or semi-unique corroborating factors")

        return ", ".join(missing_parts)
