"""LittleSis adapter — power network relationship mapping.

Queries the LittleSis.org database of 400K+ entities and 1.6M
relationships in US business and government. Maps connections between
politicians, lobbyists, corporate boards, and government officials.
No API key or authentication required.
"""

import asyncio
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
            resp_json = resp.json()
            rel_data = resp_json.get("data", [])
        except Exception as exc:
            logger.debug("Failed to fetch relationships for %s: %s", ls_id, exc)
            return entity, rels

        # Parse included entities from JSON:API response (if present).
        included_map: dict[str, dict] = {}
        for inc in resp_json.get("included", []):
            inc_id = str(inc.get("id", ""))
            inc_attrs = inc.get("attributes", {})
            if inc_id and inc_attrs:
                included_map[inc_id] = inc_attrs

        # Collect unique other-entity IDs needing resolution.
        other_ids_needed: set[str] = set()
        for rel_item in rel_data[:30]:
            rel_attrs = rel_item.get("attributes", {})
            entity1_id = str(rel_attrs.get("entity1_id", ""))
            entity2_id = str(rel_attrs.get("entity2_id", ""))
            other_id = entity2_id if entity1_id == str(ls_id) else entity1_id
            if other_id and other_id not in included_map:
                other_ids_needed.add(other_id)

        # Batch-fetch names for entities not in included data.
        if other_ids_needed:
            fetched = await self._batch_fetch_entities(
                client, other_ids_needed,
            )
            for eid, attrs in fetched.items():
                included_map[eid] = attrs

        # Build relationship entities with resolved names.
        for rel_item in rel_data[:30]:
            rel_attrs = rel_item.get("attributes", {})
            cat_id = rel_attrs.get("category_id")
            cat_label, rel_type = _REL_CATEGORIES.get(
                cat_id, ("unknown", RelationType.CONNECTED_TO),
            )

            entity1_id = str(rel_attrs.get("entity1_id", ""))
            entity2_id = str(rel_attrs.get("entity2_id", ""))
            other_id = (
                entity2_id if entity1_id == str(ls_id) else entity1_id
            )

            # Resolve name and type from included/fetched data.
            other_attrs = included_map.get(other_id, {})
            other_name = (
                other_attrs.get("name")
                or f"LittleSis entity {other_id}"
            )
            other_ext = other_attrs.get("primary_ext", "")
            other_etype = _TYPE_MAP.get(other_ext, EntityType.ORGANIZATION)

            other_ent = Entity(
                id=f"{other_etype.value}:littlesis:{other_id}",
                entity_type=other_etype,
                label=other_name,
                properties={
                    "littlesis_id": other_id,
                    "url": f"https://littlesis.org/entities/{other_id}",
                },
                sources=[_SOURCE()],
            )

            rel_props = {}
            desc1 = rel_attrs.get("description1", "")
            desc2 = rel_attrs.get("description2", "")
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

    async def _batch_fetch_entities(
        self,
        client: httpx.AsyncClient,
        entity_ids: set[str],
    ) -> dict[str, dict]:
        """Fetch basic details for related entities by ID.

        Returns a mapping of entity_id -> {"name": ..., "primary_ext": ...}.
        Caps at 15 concurrent fetches for rate-limit friendliness.
        """
        results: dict[str, dict] = {}
        ids = list(entity_ids)[:15]
        sem = asyncio.Semaphore(5)

        async def fetch(eid: str) -> None:
            async with sem:
                try:
                    resp = await client.get(f"{_BASE}/entities/{eid}")
                    resp.raise_for_status()
                    data = resp.json().get("data", {})
                    attrs = data.get("attributes", {})
                    results[eid] = {
                        "name": attrs.get("name", ""),
                        "primary_ext": attrs.get("primary_ext", ""),
                    }
                except Exception:
                    pass

        await asyncio.gather(*(fetch(eid) for eid in ids))
        return results
