"""MuckRock adapter — FOIA request search and agency lookup.

Queries MuckRock.com's database of 115K+ FOIA requests and 30K+
government agencies. Find what documents others have already requested
from law enforcement, government agencies, and public bodies.

Uses MuckRock's public API v1 (no auth needed for read access).
Note: The v1 API's name filter is exact-match only, so this adapter
fetches pages and filters client-side for substring matches.
"""

import logging

import httpx

from osint_agent.models import (
    Entity,
    EntityType,
    Finding,
    Source,
)
from osint_agent.tools.base import ToolAdapter

logger = logging.getLogger(__name__)

_BASE = "https://www.muckrock.com/api_v1"
_SOURCE = lambda url=None: Source(tool="muckrock", source_url=url)

# Max pages to scan for client-side filtering.
_MAX_PAGES = 5
_PAGE_SIZE = 100


class MuckRockAdapter(ToolAdapter):
    """Search MuckRock for FOIA requests and government agencies."""

    name = "muckrock"

    def __init__(self, timeout: int = 20):
        self.timeout = timeout

    def is_available(self) -> bool:
        return True  # Only needs httpx

    async def run(
        self,
        query: str,
        mode: str = "foia",
        **kwargs,
    ) -> Finding:
        """Search MuckRock for FOIA requests or agencies.

        Args:
            query: Search terms (agency name, topic, etc.).
            mode: "foia" to search FOIA requests, "agency" to search agencies.
        """
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            if mode == "agency":
                return await self._search_agencies(client, query)
            return await self._search_foia(client, query)

    async def _search_foia(
        self,
        client: httpx.AsyncClient,
        query: str,
    ) -> Finding:
        """Search FOIA requests by matching query against titles and slugs."""
        entities: list[Entity] = []
        query_lower = query.lower()

        # Scan pages of recent FOIA requests, filter client-side.
        url = f"{_BASE}/foia/"
        for page in range(_MAX_PAGES):
            try:
                resp = await client.get(
                    url,
                    params={
                        "page_size": _PAGE_SIZE,
                        "ordering": "-datetime_submitted",
                        "page": page + 1,
                    },
                    headers={"Accept": "application/json"},
                )
                resp.raise_for_status()
                data = resp.json()
            except httpx.HTTPError as exc:
                if page == 0:
                    return Finding(notes=f"MuckRock error: {exc}")
                break

            for foia in data.get("results", []):
                title = (foia.get("title") or "").lower()
                slug = (foia.get("slug") or "").lower()
                if query_lower in title or query_lower in slug:
                    ent = self._build_foia_entity(foia)
                    if ent:
                        entities.append(ent)

            # Stop if we have enough results or no more pages.
            if len(entities) >= 25 or not data.get("next"):
                break

        if not entities:
            return Finding(
                notes=f"MuckRock: no FOIA requests matching '{query}' in recent filings",
            )

        return Finding(
            entities=entities,
            notes=(
                f"MuckRock: {len(entities)} FOIA request(s) matching '{query}'"
            ),
        )

    async def _search_agencies(
        self,
        client: httpx.AsyncClient,
        query: str,
    ) -> Finding:
        """Search for government agencies by scanning and filtering."""
        entities: list[Entity] = []
        query_lower = query.lower()

        # Scan pages of agencies, filter client-side.
        for page in range(_MAX_PAGES):
            try:
                resp = await client.get(
                    f"{_BASE}/agency/",
                    params={
                        "page_size": _PAGE_SIZE,
                        "page": page + 1,
                    },
                    headers={"Accept": "application/json"},
                )
                resp.raise_for_status()
                data = resp.json()
            except httpx.HTTPError as exc:
                if page == 0:
                    return Finding(notes=f"MuckRock agency search error: {exc}")
                break

            for agency in data.get("results", []):
                name = (agency.get("name") or "").lower()
                if query_lower in name:
                    ent = self._build_agency_entity(agency)
                    entities.append(ent)

            if len(entities) >= 20 or not data.get("next"):
                break

        if not entities:
            return Finding(
                notes=f"MuckRock: no agencies matching '{query}'",
            )

        return Finding(
            entities=entities,
            notes=(
                f"MuckRock: {len(entities)} agency/agencies matching '{query}'"
            ),
        )

    def _build_foia_entity(self, foia: dict) -> Entity | None:
        """Build a document entity from a FOIA request."""
        foia_id = foia.get("id")
        title = foia.get("title") or foia.get("slug", f"FOIA #{foia_id}")
        if not title:
            title = f"FOIA #{foia_id}"

        status = foia.get("status", "")
        url = f"https://www.muckrock.com/foi/{foia.get('slug', '')}-{foia_id}/"

        props = {}
        for key in ("status", "datetime_submitted", "datetime_done",
                     "tracking_id", "price"):
            val = foia.get(key)
            if val is not None and val != "" and val != "0.00":
                props[key] = val

        agency_id = foia.get("agency")
        if agency_id:
            props["agency_id"] = agency_id

        username = foia.get("username")
        if username:
            props["requester"] = username

        tags = foia.get("tags", [])
        if tags:
            props["foia_tags"] = tags

        return Entity(
            id=f"document:muckrock:{foia_id}",
            entity_type=EntityType.DOCUMENT,
            label=title,
            properties={
                **props,
                "url": url,
                "document_source": "muckrock",
                "foia_status": status,
            },
            sources=[_SOURCE(url)],
        )

    def _build_agency_entity(self, agency: dict) -> Entity:
        """Build an organization entity from a MuckRock agency."""
        agency_id = agency.get("id")
        name = agency.get("name", f"Agency #{agency_id}")

        props = {}
        for key in ("status", "appeal_agency", "requires_proxy",
                     "average_response_time", "fee_rate", "success_rate"):
            val = agency.get(key)
            if val is not None and val != "":
                props[key] = val

        jurisdiction = agency.get("jurisdiction")
        if isinstance(jurisdiction, dict):
            props["jurisdiction"] = jurisdiction.get("name", "")
            props["jurisdiction_level"] = jurisdiction.get("level", "")
        elif jurisdiction is not None:
            props["jurisdiction_id"] = jurisdiction

        return Entity(
            id=f"organization:muckrock:{agency_id}",
            entity_type=EntityType.ORGANIZATION,
            label=name,
            properties={
                **props,
                "url": f"https://www.muckrock.com/agency/{agency.get('slug', '')}-{agency_id}/",
                "agency_type": "government",
            },
            sources=[_SOURCE(
                f"https://www.muckrock.com/agency/{agency.get('slug', '')}-{agency_id}/"
            )],
        )
