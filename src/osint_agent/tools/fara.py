"""FARA adapter — Foreign Agents Registration Act lobbying data.

Queries the DOJ's FARA eFile API for individuals and organizations
registered as agents of foreign governments in the US. Returns
registrant details, foreign principals they represent, and filing
documents.

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

_BASE = "https://efile.fara.gov/api/v1"
_SOURCE = lambda url=None: Source(tool="fara", source_url=url)


class FaraAdapter(ToolAdapter):
    """Search FARA for foreign agent registrations and lobbying relationships."""

    name = "fara"

    def __init__(self, timeout: int = 30):
        self.timeout = timeout

    def is_available(self) -> bool:
        return True  # Only needs httpx

    async def run(
        self,
        name: str,
        registration_number: int | None = None,
        **kwargs,
    ) -> Finding:
        """Search FARA for a registrant by name or look up by registration number.

        Args:
            name: Person or organization name to search among registrants.
            registration_number: Direct lookup by FARA registration number.
        """
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            if registration_number:
                return await self._lookup_registration(client, registration_number)
            return await self._search_name(client, name)

    async def _search_name(
        self,
        client: httpx.AsyncClient,
        name: str,
    ) -> Finding:
        """Search active and terminated registrants for a name match."""
        entities: list[Entity] = []
        relationships: list[Relationship] = []
        name_lower = name.lower()

        # Search both active and terminated registrants.
        for status in ("Active", "Terminated"):
            try:
                resp = await client.get(f"{_BASE}/Registrants/json/{status}")
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                logger.warning("FARA %s registrants fetch failed: %s", status, exc)
                continue

            data = resp.json()
            rows = _extract_rows(data)

            for row in rows:
                reg_name = row.get("Name", "")
                if name_lower not in reg_name.lower():
                    continue

                reg_num = row.get("Registration_Number")
                ent = self._build_registrant_entity(row, status.lower())
                entities.append(ent)

                # Fetch foreign principals for this registrant.
                fp_ents, fp_rels = await self._fetch_foreign_principals(
                    client, reg_num, ent,
                )
                entities.extend(fp_ents)
                relationships.extend(fp_rels)

                # Fetch documents for this registrant.
                doc_ents, doc_rels = await self._fetch_documents(
                    client, reg_num, ent,
                )
                entities.extend(doc_ents)
                relationships.extend(doc_rels)

        if not entities:
            return Finding(notes=f"FARA: no registrants matching '{name}'")

        registrant_count = sum(
            1 for e in entities if e.properties.get("registration_number")
        )
        return Finding(
            entities=entities,
            relationships=relationships,
            notes=(
                f"FARA: {registrant_count} registrant(s) matching '{name}', "
                f"{len(relationships)} relationship(s)"
            ),
        )

    async def _lookup_registration(
        self,
        client: httpx.AsyncClient,
        reg_num: int,
    ) -> Finding:
        """Look up a specific registration by number."""
        entities: list[Entity] = []
        relationships: list[Relationship] = []

        # Try active first, then terminated.
        for status in ("Active", "Terminated"):
            try:
                resp = await client.get(f"{_BASE}/Registrants/json/{status}")
                resp.raise_for_status()
            except httpx.HTTPError:
                continue

            rows = _extract_rows(resp.json())
            for row in rows:
                if row.get("Registration_Number") == reg_num:
                    ent = self._build_registrant_entity(row, status.lower())
                    entities.append(ent)

                    fp_ents, fp_rels = await self._fetch_foreign_principals(
                        client, reg_num, ent,
                    )
                    entities.extend(fp_ents)
                    relationships.extend(fp_rels)

                    doc_ents, doc_rels = await self._fetch_documents(
                        client, reg_num, ent,
                    )
                    entities.extend(doc_ents)
                    relationships.extend(doc_rels)

                    return Finding(
                        entities=entities,
                        relationships=relationships,
                        notes=(
                            f"FARA: registration #{reg_num} — "
                            f"{row.get('Name', '?')} ({status})"
                        ),
                    )

        return Finding(notes=f"FARA: no registration found for #{reg_num}")

    async def _fetch_foreign_principals(
        self,
        client: httpx.AsyncClient,
        reg_num: int | None,
        registrant_ent: Entity,
    ) -> tuple[list[Entity], list[Relationship]]:
        """Fetch foreign principals for a registration."""
        if not reg_num:
            return [], []

        entities: list[Entity] = []
        relationships: list[Relationship] = []

        for status in ("Active", "Terminated"):
            try:
                resp = await client.get(
                    f"{_BASE}/ForeignPrincipals/json/{status}/{reg_num}",
                )
                resp.raise_for_status()
            except httpx.HTTPError:
                continue

            rows = _extract_rows(resp.json())
            for row in rows:
                fp_name = row.get("FP_NAME", "Unknown Principal")
                country = row.get("COUNTRY_NAME", "")
                fp_id = f"organization:fara_fp:{reg_num}:{_slug(fp_name)}"

                fp_ent = Entity(
                    id=fp_id,
                    entity_type=EntityType.ORGANIZATION,
                    label=fp_name,
                    properties={
                        k: v for k, v in {
                            "country": country,
                            "city": row.get("CITY"),
                            "state": row.get("STATE"),
                            "address": row.get("ADDRESS_1"),
                            "registration_date": row.get("FP_REG_DATE"),
                            "fara_status": status.lower(),
                            "entity_type": "foreign_principal",
                        }.items() if v
                    },
                    sources=[_SOURCE(
                        "https://efile.fara.gov/ords/fara/f?p=1235:10"
                    )],
                )
                entities.append(fp_ent)

                relationships.append(Relationship(
                    source_id=registrant_ent.id,
                    target_id=fp_id,
                    relation_type=RelationType.AFFILIATED_WITH,
                    properties={
                        "relationship": "represents_as_foreign_agent",
                        "country": country,
                    },
                    sources=[_SOURCE()],
                ))

        return entities, relationships

    async def _fetch_documents(
        self,
        client: httpx.AsyncClient,
        reg_num: int | None,
        registrant_ent: Entity,
    ) -> tuple[list[Entity], list[Relationship]]:
        """Fetch filing documents for a registration (capped at 10)."""
        if not reg_num:
            return [], []

        try:
            resp = await client.get(f"{_BASE}/RegDocs/json/{reg_num}")
            resp.raise_for_status()
        except httpx.HTTPError:
            return [], []

        rows = _extract_rows(resp.json())
        entities: list[Entity] = []
        relationships: list[Relationship] = []

        for row in rows[:10]:
            doc_type = row.get("Document_Type", "Filing")
            stamp = row.get("Stamped_Date", "")
            doc_url = row.get("Url", "")

            doc_ent = Entity(
                id=f"document:fara:{reg_num}:{_slug(doc_type)}:{_slug(stamp)}",
                entity_type=EntityType.DOCUMENT,
                label=f"FARA {doc_type} — Reg #{reg_num}",
                properties={
                    k: v for k, v in {
                        "document_type": doc_type,
                        "stamped_date": stamp,
                        "url": doc_url if doc_url else None,
                        "registration_number": reg_num,
                    }.items() if v
                },
                sources=[_SOURCE(doc_url or None)],
            )
            entities.append(doc_ent)

            relationships.append(Relationship(
                source_id=registrant_ent.id,
                target_id=doc_ent.id,
                relation_type=RelationType.FILED,
                properties={"document_type": doc_type},
                sources=[_SOURCE()],
            ))

        return entities, relationships

    def _build_registrant_entity(self, row: dict, status: str) -> Entity:
        """Build an entity from a FARA registrant record."""
        reg_num = row.get("Registration_Number")
        name = row.get("Name", "Unknown Registrant")

        props = {
            k: v for k, v in {
                "registration_number": reg_num,
                "registration_date": row.get("Registration_Date"),
                "city": row.get("City"),
                "state": row.get("State"),
                "address": row.get("Address_1"),
                "zip": str(row.get("Zip", "")) if row.get("Zip") else None,
                "fara_status": status,
                "entity_type": "foreign_agent_registrant",
                "url": (
                    f"https://efile.fara.gov/ords/fara"
                    f"/f?p=1235:10::::RP,10:P10_REG_NUMBER:{reg_num}"
                    if reg_num else None
                ),
            }.items() if v
        }

        return Entity(
            id=f"organization:fara:{reg_num or _slug(name)}",
            entity_type=EntityType.ORGANIZATION,
            label=name,
            properties=props,
            sources=[_SOURCE(
                "https://efile.fara.gov/ords/fara/f?p=1235:10"
            )],
        )


def _extract_rows(data: dict) -> list[dict]:
    """Extract rows from FARA API response (handles varying key names)."""
    for key in data:
        inner = data[key]
        if isinstance(inner, dict) and "ROW" in inner:
            rows = inner["ROW"]
            return rows if isinstance(rows, list) else [rows]
    return []


def _slug(s: str) -> str:
    import re
    if not s:
        return "unknown"
    return re.sub(r"[^a-z0-9]+", "_", str(s).lower()).strip("_")[:50]
