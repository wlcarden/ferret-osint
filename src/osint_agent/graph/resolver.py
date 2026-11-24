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

    def resolve(
        self,
        entities: list[Entity],
        relationships: list[Relationship] | None = None,
    ) -> list[Relationship]:
        """Find cross-source duplicates and return ALSO_KNOWN_AS edges.

        Args:
            entities: All entities from the graph (across all tools).
            relationships: Optional existing relationships, used for
                contact-identifier dedup (shared email/phone/username
                indicates shared identity).

        Returns:
            List of ALSO_KNOWN_AS Relationship objects with confidence
            scores in properties.
        """
        out: list[Relationship] = []

        for entity_type in RESOLVABLE_TYPES:
            typed_entities = [
                e for e in entities if e.entity_type == entity_type
            ]
            if len(typed_entities) < 2:
                continue

            matches = self._resolve_type(typed_entities, entity_type)
            out.extend(matches)

        # High-confidence signals that don't require name-based blocking
        out.extend(self._resolve_by_uei(entities))
        if relationships is not None:
            out.extend(
                self._resolve_by_shared_contact(entities, relationships),
            )
        out.extend(self._resolve_ocr_variants(entities))

        # Deduplicate across passes on (source, target) pair
        seen: set[tuple[str, str]] = set()
        deduped: list[Relationship] = []
        for rel in out:
            key = tuple(sorted([rel.source_id, rel.target_id]))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(rel)
        return deduped

    # ------------------------------------------------------------------
    # Additional high-confidence signals
    # ------------------------------------------------------------------

    def _resolve_by_uei(
        self, entities: list[Entity],
    ) -> list[Relationship]:
        """Two organizations sharing a UEI property are the same entity.

        UEI is SAM.gov's 12-char unique identifier; the strongest
        available signal for corporate identity.
        """
        from collections import defaultdict
        by_uei: dict[str, list[Entity]] = defaultdict(list)
        for e in entities:
            if e.entity_type != EntityType.ORGANIZATION:
                continue
            uei = e.properties.get("uei")
            if isinstance(uei, str) and len(uei) >= 10:
                by_uei[uei.upper()].append(e)

        out: list[Relationship] = []
        for uei, group in by_uei.items():
            if len(group) < 2:
                continue
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    e1, e2 = group[i], group[j]
                    out.append(_make_aka(e1, e2, 0.95, reason=f"shared_uei={uei}"))
        return out

    def _resolve_by_shared_contact(
        self,
        entities: list[Entity],
        relationships: list[Relationship],
    ) -> list[Relationship]:
        """Two persons linked to the same email/phone/username entity
        via has_email/has_phone/has_username are the same individual.

        Official email addresses like someone@ice.dhs.gov are assigned
        1:1; entities sharing one are almost certainly the same person.
        """
        from collections import defaultdict
        person_ids = {
            e.id for e in entities if e.entity_type == EntityType.PERSON
        }
        contact_rels = {
            RelationType.HAS_EMAIL,
            RelationType.HAS_PHONE,
            RelationType.HAS_USERNAME,
        }
        # (contact_id, rel_type) -> list of person_ids linked to it
        groups: dict[tuple[str, str], list[str]] = defaultdict(list)
        for rel in relationships:
            if rel.relation_type not in contact_rels:
                continue
            if rel.source_id not in person_ids:
                continue
            groups[(rel.target_id, rel.relation_type.value)].append(rel.source_id)

        entity_by_id = {e.id: e for e in entities}
        out: list[Relationship] = []
        for (contact_id, rel_name), person_ids_list in groups.items():
            uniq = sorted(set(person_ids_list))
            if len(uniq) < 2:
                continue
            for i in range(len(uniq)):
                for j in range(i + 1, len(uniq)):
                    e1 = entity_by_id.get(uniq[i])
                    e2 = entity_by_id.get(uniq[j])
                    if not e1 or not e2:
                        continue
                    out.append(_make_aka(
                        e1, e2, 0.9,
                        reason=f"shared_{rel_name}={contact_id[:40]}",
                    ))
        return out

    def _resolve_ocr_variants(
        self, entities: list[Entity],
    ) -> list[Relationship]:
        """Match persons whose labels differ by OCR-confusable characters.

        Example: 'Gerard A. Jones' ↔ 'Gerard A. Vones' (tesseract
        frequently confuses J/V, O/0, l/1, m/rn). Very tight filter to
        avoid false positives on genuinely-distinct individuals.
        """
        ocr_confusions = {
            frozenset(["j", "v"]), frozenset(["j", "i"]),
            frozenset(["o", "0"]), frozenset(["l", "1"]),
            frozenset(["l", "i"]), frozenset(["s", "5"]),
            frozenset(["b", "8"]), frozenset(["c", "e"]),
            frozenset(["u", "n"]), frozenset(["d", "b"]),
            frozenset(["g", "q"]),
        }

        def _norm(label: str) -> str:
            s = label.lower().strip()
            s = re.split(r"[,;]", s)[0]
            return re.sub(r"\s+", " ", s).strip()

        def _is_middle_initial_only_diff(n1: str, n2: str) -> bool:
            t1, t2 = n1.split(), n2.split()
            if len(t1) != len(t2) or len(t1) < 3:
                return False
            diffs = [(a, b) for a, b in zip(t1, t2) if a != b]
            return (
                len(diffs) == 1
                and all(len(x.rstrip(".")) == 1 for x in diffs[0])
            )

        def _ocr_confusable(n1: str, n2: str) -> bool:
            if len(n1) != len(n2):
                return False
            diffs = [(c1, c2) for c1, c2 in zip(n1, n2) if c1 != c2]
            return (
                bool(diffs)
                and len(diffs) <= 2
                and all(frozenset([c1, c2]) in ocr_confusions for c1, c2 in diffs)
            )

        from collections import defaultdict
        # Bucket by (first 2, last 2, length) for blocking
        buckets: dict[tuple, list[tuple[str, Entity]]] = defaultdict(list)
        for e in entities:
            if e.entity_type != EntityType.PERSON:
                continue
            n = _norm(e.label)
            if len(n) < 7:
                continue
            buckets[(n[:2], n[-2:], len(n))].append((n, e))

        out: list[Relationship] = []
        seen: set[tuple[str, str]] = set()
        for group in buckets.values():
            for i, (n1, e1) in enumerate(group):
                for n2, e2 in group[i + 1:]:
                    if e1.id == e2.id or n1 == n2:
                        continue
                    pk = tuple(sorted([e1.id, e2.id]))
                    if pk in seen:
                        continue
                    if _is_middle_initial_only_diff(n1, n2):
                        continue
                    if not _ocr_confusable(n1, n2):
                        continue
                    seen.add(pk)
                    out.append(_make_aka(
                        e1, e2, 0.85,
                        reason="ocr_variant",
                    ))
        return out

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
    reason: str = "",
) -> Relationship:
    """Create an ALSO_KNOWN_AS relationship between two entities."""
    props = {
        "confidence": round(confidence, 3),
        "method": "entity_resolution",
        "source_label": e1.label,
        "target_label": e2.label,
    }
    if reason:
        props["reason"] = reason
    return Relationship(
        source_id=e1.id,
        target_id=e2.id,
        relation_type=RelationType.ALSO_KNOWN_AS,
        properties=props,
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
