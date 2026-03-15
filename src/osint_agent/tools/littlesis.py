"""LittleSis adapter — power network relationship mapping.

Queries the LittleSis.org database of 400K+ entities and 1.6M
relationships in US business and government. Maps connections between
politicians, lobbyists, corporate boards, and government officials.
No API key or authentication required.
"""

import logging

import httpx

from osint_agent.models import (
    Entity,
    EntityType,
    Finding,
    Relationship,
    RelationType,
    Source,
)
from osint_agent.tools.base import ToolAdapter

logger = logging.getLogger(__name__)

_BASE = "https://littlesis.org/api"
_SOURCE = lambda: Source(tool="littlesis")

# LittleSis primary_ext → our EntityType.
_TYPE_MAP = {
    "Person": EntityType.PERSON,
    "Org": EntityType.ORGANIZATION,
}

# LittleSis relationship category_id → our RelationType + label.
_REL_CATEGORIES = {
    1: ("position", RelationType.WORKS_AT),
    2: ("education", RelationType.AFFILIATED_WITH),
    3: ("membership", RelationType.AFFILIATED_WITH),
    4: ("family", RelationType.CONNECTED_TO),
    5: ("donation", RelationType.DONATED_TO),
    6: ("transaction", RelationType.TRANSACTED_WITH),
    7: ("lobbying", RelationType.AFFILIATED_WITH),
    8: ("social", RelationType.CONNECTED_TO),
    9: ("professional", RelationType.CONNECTED_TO),
    10: ("ownership", RelationType.OWNS),
    11: ("hierarchy", RelationType.CONTROLS),
    12: ("generic", RelationType.CONNECTED_TO),
}


class LittleSisAdapter(ToolAdapter):
    """Search LittleSis for power network relationships."""

    name = "littlesis"

    def __init__(self, timeout: int = 20):
        self.timeout = timeout

    def is_available(self) -> bool:
        return True  # Only needs httpx

    async def run(self, name: str, **kwargs) -> Finding:
        """Search LittleSis for an entity and its relationships.

        Args:
            name: Person or organization name to search.
        """
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            # Step 1: Search for the entity.
            try:
                resp = await client.get(
                    f"{_BASE}/entities/search",
                    params={"q": name},
                )
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 503:
                    return Finding(notes="LittleSis rate limit exceeded. Try again later.")
                return Finding(notes=f"LittleSis search error: {exc.response.status_code}")
            except httpx.HTTPError as exc:
                logger.warning("LittleSis search failed: %s", exc)
                return Finding(notes=f"LittleSis error: {exc}")

            data = resp.json()
            results = data.get("data", [])
            if not results:
                return Finding(notes=f"LittleSis: no results for '{name}'")

            entities: list[Entity] = []
            relationships: list[Relationship] = []

            # Process top results (usually 1-3 relevant matches).
            for item in results[:3]:
                ent, rels = await self._process_entity(client, item)
                entities.append(ent)
                # Collect related entities + relationships.
                for rel_ent, rel in rels:
                    # Avoid duplicate entities.
                    if not any(e.id == rel_ent.id for e in entities):
                        entities.append(rel_ent)
                    relationships.append(rel)

            match_count = len(results)
            rel_count = len(relationships)
            return Finding(
                entities=entities,
                relationships=relationships,
                notes=(
                    f"LittleSis: {match_count} match(es) for '{name}', "
                    f"{rel_count} relationships mapped"
                ),
            )

    async def _process_entity(
        self,
        client: httpx.AsyncClient,
        item: dict,
    ) -> tuple[Entity, list[tuple[Entity, Relationship]]]:
        """Build an entity and fetch its relationships."""
        attrs = item.get("attributes", {})
        ls_id = item.get("id", "")
        name = attrs.get("name", "unknown")
        ext = attrs.get("primary_ext", "")
        etype = _TYPE_MAP.get(ext, EntityType.ORGANIZATION)

        props = {}
        for key in ("blurb", "website", "start_date", "end_date", "summary"):
            val = attrs.get(key)
            if val:
                props[key] = val if len(str(val)) <= 500 else str(val)[:497] + "..."

        types = attrs.get("types", [])
        if types:
            props["littlesis_types"] = types

        aliases = attrs.get("aliases", [])
        if aliases:
            props["aliases"] = aliases

        entity = Entity(
            id=f"{etype.value}:littlesis:{ls_id}",
            entity_type=etype,
            label=name,
            properties={
                **props,
                "littlesis_id": ls_id,
                "url": f"https://littlesis.org/entities/{ls_id}",
            },
            sources=[_SOURCE()],
        )

        # Fetch relationships.
        rels: list[tuple[Entity, Relationship]] = []
        try:
            resp = await client.get(
                f"{_BASE}/entities/{ls_id}/relationships",
            )
            resp.raise_for_status()
            rel_data = resp.json().get("data", [])
        except Exception as exc:
            logger.debug("Failed to fetch relationships for %s: %s", ls_id, exc)
            return entity, rels

        for rel_item in rel_data[:30]:  # Cap to avoid huge graphs.
            rel_attrs = rel_item.get("attributes", {})
            cat_id = rel_attrs.get("category_id")
            cat_label, rel_type = _REL_CATEGORIES.get(
                cat_id, ("unknown", RelationType.CONNECTED_TO),
            )

            # Determine the other entity in this relationship.
            entity1_id = str(rel_attrs.get("entity1_id", ""))
            entity2_id = str(rel_attrs.get("entity2_id", ""))
            if entity1_id == str(ls_id):
                other_id = entity2_id
            else:
                other_id = entity1_id

            _other_name = (
                rel_attrs.get("entity2_id")
                if entity1_id == str(ls_id)
                else rel_attrs.get("entity1_id")
            )

            # We need the other entity's name. Check for it in attributes.
            desc1 = rel_attrs.get("description1", "")
            desc2 = rel_attrs.get("description2", "")

            # Build a minimal other entity — we don't have its full details.
            other_ent = Entity(
                id=f"organization:littlesis:{other_id}",
                entity_type=EntityType.ORGANIZATION,  # Default, may be person.
                label=f"LittleSis entity {other_id}",
                properties={
                    "littlesis_id": other_id,
                    "url": f"https://littlesis.org/entities/{other_id}",
                },
                sources=[_SOURCE()],
            )

            rel_props = {}
            if desc1:
                rel_props["description1"] = desc1
            if desc2:
                rel_props["description2"] = desc2
            amount = rel_attrs.get("amount")
            if amount:
                rel_props["amount"] = amount
            start = rel_attrs.get("start_date")
            end = rel_attrs.get("end_date")
            if start:
                rel_props["start_date"] = start
            if end:
                rel_props["end_date"] = end

            rel = Relationship(
                source_id=entity.id,
                target_id=other_ent.id,
                relation_type=rel_type,
                properties={**rel_props, "category": cat_label},
                sources=[_SOURCE()],
            )
            rels.append((other_ent, rel))

        return entity, rels
