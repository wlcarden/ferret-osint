"""Entity resolution — links same-entity nodes across tool sources.

Each OSINT tool creates entities with source-specific IDs (e.g.,
person:patent:jane_doe, person:fec:jane_doe).  The EntityResolver
finds entities that likely represent the same real-world thing and
creates ALSO_KNOWN_AS relationships between them with confidence
scores.

Design:
  1. Block by entity type (only compare PERSON↔PERSON, ORG↔ORG)
  2. Group candidates by normalized name
  3. Score each candidate pair using multiple signals
  4. Emit ALSO_KNOWN_AS edges above a confidence threshold

For PERSON entities, a weighted corroboration model prevents false
linkage from common name matches (see corroboration.py).
"""

import re
from collections import defaultdict

from osint_agent.graph.corroboration import CorroborationPolicy, CorroborationResult
from osint_agent.models import (
    Entity,
    EntityType,
    Relationship,
    RelationType,
    Source,
)

# Entity types worth resolving (DOCUMENTs have unique IDs by nature)
RESOLVABLE_TYPES = {EntityType.PERSON, EntityType.ORGANIZATION}

# Minimum confidence to create an ALSO_KNOWN_AS link
CONFIDENCE_THRESHOLD = 0.6

# Properties that boost confidence when they match
PERSON_BOOST_FIELDS = ["city", "state", "title", "phone", "company"]
ORG_BOOST_FIELDS = ["city", "state", "address", "duns", "zip"]


class EntityResolver:
    """Finds and links duplicate entities across tool sources.

    For PERSON entities, uses a weighted corroboration model that
    requires multiple independent factors before creating a link.
    For ORGANIZATION entities, uses the original scoring model where
    name similarity is sufficient.

    Usage:
        resolver = EntityResolver()
        aka_relationships = resolver.resolve(entities)
        # aka_relationships is a list of ALSO_KNOWN_AS Relationship objects
    """

    def __init__(self, policy: CorroborationPolicy | None = None):
        self.policy = policy or CorroborationPolicy()

    def resolve(self, entities: list[Entity]) -> list[Relationship]:
        """Find cross-source duplicates and return ALSO_KNOWN_AS edges.

        Args:
            entities: All entities from the graph (across all tools).

        Returns:
            List of ALSO_KNOWN_AS Relationship objects with confidence
            scores in properties.
        """
        relationships: list[Relationship] = []

        for entity_type in RESOLVABLE_TYPES:
            typed_entities = [
                e for e in entities if e.entity_type == entity_type
            ]
            if len(typed_entities) < 2:
                continue

            matches = self._resolve_type(typed_entities, entity_type)
            relationships.extend(matches)

        return relationships

    def _resolve_type(
        self,
        entities: list[Entity],
        entity_type: EntityType,
    ) -> list[Relationship]:
        """Resolve duplicates within a single entity type."""
        # Group by normalized name (blocking step)
        name_groups: dict[str, list[Entity]] = defaultdict(list)
        for entity in entities:
            key = _normalize_for_blocking(entity.label)
            if key:
                name_groups[key].append(entity)

        relationships: list[Relationship] = []
        already_linked: set[tuple[str, str]] = set()

        # Both PERSON and ORGANIZATION now use corroboration
        corroboration_type = {
            EntityType.PERSON: "person",
            EntityType.ORGANIZATION: "organization",
        }.get(entity_type)

        # Exact normalized name matches
        for key, group in name_groups.items():
            if len(group) < 2:
                continue
            # Only link entities from different sources
            cross_source_pairs = _cross_source_pairs(group)
            for e1, e2 in cross_source_pairs:
                pair_key = _pair_key(e1.id, e2.id)
                if pair_key in already_linked:
                    continue
                already_linked.add(pair_key)

                if corroboration_type:
                    result = self.policy.evaluate(
                        e1, e2,
                        name_similarity=1.0,
                        entity_type=corroboration_type,
                    )
                    if result.level != "insufficient":
                        relationships.append(
                            _make_aka_corroborated(e1, e2, result),
                        )
                else:
                    confidence = _score_pair(e1, e2, entity_type)
                    if confidence >= CONFIDENCE_THRESHOLD:
                        relationships.append(_make_aka(e1, e2, confidence))

        # Token-overlap matching for near-misses
        # (e.g., "Acme Corp" vs "Acme Corporation")
        all_keys = list(name_groups.keys())
        for i, key_a in enumerate(all_keys):
            for key_b in all_keys[i + 1:]:
                similarity = _token_overlap(key_a, key_b)
                if similarity < 0.7:
                    continue
                # Check cross-source pairs between these two groups
                for e1 in name_groups[key_a]:
                    for e2 in name_groups[key_b]:
                        if _extract_source(e1.id) == _extract_source(e2.id):
                            continue
                        pair_key = _pair_key(e1.id, e2.id)
                        if pair_key in already_linked:
                            continue
                        already_linked.add(pair_key)

                        if corroboration_type:
                            result = self.policy.evaluate(
                                e1, e2,
                                name_similarity=similarity,
                                entity_type=corroboration_type,
                            )
                            if result.level != "insufficient":
                                relationships.append(
                                    _make_aka_corroborated(e1, e2, result),
                                )
                        else:
                            confidence = _score_pair(
                                e1, e2, entity_type,
                                name_similarity=similarity,
                            )
                            if confidence >= CONFIDENCE_THRESHOLD:
                                relationships.append(
                                    _make_aka(e1, e2, confidence),
                                )

        return relationships

    def get_canonical_profile(
        self,
        entity_id: str,
        entities: list[Entity],
        aka_relationships: list[Relationship],
    ) -> dict:
        """Build a merged profile from an entity and all its AKAs.

        Returns a dict with:
          - canonical_id: the input entity_id
          - label: preferred label
          - aliases: list of all linked entity IDs
          - merged_properties: union of all properties
          - sources: combined source list
          - confidence: minimum confidence across links
        """
        # Find all linked entities via ALSO_KNOWN_AS
        linked_ids = {entity_id}
        for rel in aka_relationships:
            if rel.source_id == entity_id:
                linked_ids.add(rel.target_id)
            elif rel.target_id == entity_id:
                linked_ids.add(rel.source_id)

        entity_map = {e.id: e for e in entities}
        linked_entities = [
            entity_map[eid] for eid in linked_ids if eid in entity_map
        ]

        if not linked_entities:
            return {"canonical_id": entity_id, "aliases": [], "merged_properties": {}}

        # Merge properties (later sources overwrite, but collect all)
        merged_props: dict = {}
        all_sources: list[dict] = []
        labels: list[str] = []

        for entity in linked_entities:
            labels.append(entity.label)
            for key, value in entity.properties.items():
                if value and (key not in merged_props or not merged_props[key]):
                    merged_props[key] = value
            for source in entity.sources:
                all_sources.append({
                    "tool": source.tool,
                    "source_url": source.source_url,
                    "entity_id": entity.id,
                })

        # Prefer the longest label (usually most complete name)
        preferred_label = max(labels, key=len) if labels else ""

        # Min confidence across links
        confidences = []
        for rel in aka_relationships:
            if rel.source_id in linked_ids and rel.target_id in linked_ids:
                confidences.append(
                    rel.properties.get("confidence", 1.0),
                )

        return {
            "canonical_id": entity_id,
            "label": preferred_label,
            "entity_type": linked_entities[0].entity_type.value,
            "aliases": sorted(linked_ids - {entity_id}),
            "merged_properties": merged_props,
            "sources": all_sources,
            "confidence": min(confidences) if confidences else 1.0,
        }


# ── Scoring helpers ──────────────────────────────────────────────


def _score_pair(
    e1: Entity,
    e2: Entity,
    entity_type: EntityType,
    name_similarity: float = 1.0,
) -> float:
    """Score the likelihood that two entities are the same.

    Base score comes from name similarity (1.0 = exact normalized match).
    Boosted by matching properties. Penalized by conflicting properties.
    """
    # Start with name similarity as base
    score = name_similarity * 0.6

    # Property matching boost
    boost_fields = (
        PERSON_BOOST_FIELDS
        if entity_type == EntityType.PERSON
        else ORG_BOOST_FIELDS
    )

    matches = 0
    conflicts = 0
    compared = 0

    for field in boost_fields:
        v1 = e1.properties.get(field, "")
        v2 = e2.properties.get(field, "")
        if not v1 or not v2:
            continue
        compared += 1
        if _normalize_for_blocking(str(v1)) == _normalize_for_blocking(str(v2)):
            matches += 1
        else:
            conflicts += 1

    if compared > 0:
        # Each matching property adds up to 0.4 total boost
        score += (matches / max(compared, 1)) * 0.4
        # Conflicting properties reduce confidence
        score -= (conflicts / max(compared, 1)) * 0.2

    return max(0.0, min(1.0, score))


def _normalize_for_blocking(name: str) -> str:
    """Normalize a name for blocking/grouping.

    Lowercase, strip whitespace, remove punctuation, collapse spaces.
    """
    if not name:
        return ""
    name = name.lower().strip()
    # Remove common suffixes that vary across sources
    for suffix in [
        " inc", " inc.", " llc", " llc.",
        " corp", " corp.", " corporation",
        " co", " co.", " company",
        " ltd", " ltd.", " limited",
    ]:
        if name.endswith(suffix):
            name = name[:-len(suffix)]
    name = re.sub(r"[^a-z0-9\s]", "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name


def _token_overlap(a: str, b: str) -> float:
    """Compute token-level Jaccard similarity between two strings.

    Returns a float between 0.0 and 1.0.
    """
    tokens_a = set(a.split())
    tokens_b = set(b.split())
    if not tokens_a or not tokens_b:
        return 0.0
    intersection = tokens_a & tokens_b
    union = tokens_a | tokens_b
    return len(intersection) / len(union)


def _extract_source(entity_id: str) -> str:
    """Extract the source prefix from an entity ID.

    'person:patent:jane_doe' -> 'patent'
    'org:fec:C001234' -> 'fec'
    """
    parts = entity_id.split(":", 2)
    return parts[1] if len(parts) >= 2 else ""


def _cross_source_pairs(
    entities: list[Entity],
) -> list[tuple[Entity, Entity]]:
    """Generate all pairs of entities from different tool sources."""
    pairs = []
    for i, e1 in enumerate(entities):
        src1 = _extract_source(e1.id)
        for e2 in entities[i + 1:]:
            src2 = _extract_source(e2.id)
            if src1 != src2:
                pairs.append((e1, e2))
    return pairs


def _pair_key(id1: str, id2: str) -> tuple[str, str]:
    """Create a canonical key for an entity pair (order-independent)."""
    return (min(id1, id2), max(id1, id2))


def _make_aka(
    e1: Entity, e2: Entity, confidence: float,
) -> Relationship:
    """Create an ALSO_KNOWN_AS relationship between two entities."""
    return Relationship(
        source_id=e1.id,
        target_id=e2.id,
        relation_type=RelationType.ALSO_KNOWN_AS,
        properties={
            "confidence": round(confidence, 3),
            "method": "entity_resolution",
            "source_label": e1.label,
            "target_label": e2.label,
        },
        sources=[Source(
            tool="entity_resolver",
            confidence=confidence,
        )],
    )


def _make_aka_corroborated(
    e1: Entity, e2: Entity, result: CorroborationResult,
) -> Relationship:
    """Create an ALSO_KNOWN_AS relationship with corroboration details."""
    return Relationship(
        source_id=e1.id,
        target_id=e2.id,
        relation_type=RelationType.ALSO_KNOWN_AS,
        properties={
            "confidence": result.confidence,
            "method": "corroboration",
            "corroboration_level": result.level,
            "corroboration_weight": result.total_weight,
            "corroboration_factors": [
                {"field": f.field, "weight": f.weight, "category": f.category}
                for f in result.factors
            ],
            "source_label": e1.label,
            "target_label": e2.label,
        },
        sources=[Source(
            tool="entity_resolver",
            confidence=result.confidence,
        )],
    )
