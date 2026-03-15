"""Congress.gov adapter — legislative data search.

Queries the Library of Congress Congress.gov API for bills, members,
committee activities, and voting records. Track legislative connections
of political organizers and their Congressional allies.

Requires a free API key from api.congress.gov/sign-up (instant, no approval).
Set CONGRESS_API_KEY environment variable.
"""

import logging
import os

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

_BASE = "https://api.congress.gov/v3"
_SOURCE = lambda url=None: Source(tool="congress", source_url=url)


class CongressAdapter(ToolAdapter):
    """Search Congress.gov for members, bills, and legislative activity."""

    name = "congress"
    required_env_key = "CONGRESS_API_KEY"
    install_hint = "free key from api.congress.gov/sign-up"

    def __init__(self, timeout: int = 20):
        self.timeout = timeout

    async def run(
        self,
        query: str,
        mode: str = "member",
        **kwargs,
    ) -> Finding:
        """Search Congress.gov for members or bills.

        Args:
            query: Search term (member name, bill keyword, etc.).
            mode: "member" to search members, "bill" to search bills.
        """
        api_key = os.environ.get("CONGRESS_API_KEY", "")
        if not api_key:
            return Finding(
                notes=(
                    "Congress.gov: CONGRESS_API_KEY not set. "
                    "Get a free key at https://api.congress.gov/sign-up"
                ),
            )

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            if mode == "bill":
                return await self._search_bills(client, api_key, query)
            return await self._search_members(client, api_key, query)

    async def _search_members(
        self,
        client: httpx.AsyncClient,
        api_key: str,
        query: str,
    ) -> Finding:
        """Search for Congressional members by name.

        The /member endpoint does not support name filtering, so we
        paginate and filter client-side.
        """
        query_lower = query.lower()
        entities: list[Entity] = []
        offset = 0
        limit = 250  # API max per page.

        for _ in range(4):  # Max 1000 members (covers all of Congress).
            try:
                resp = await client.get(
                    f"{_BASE}/member",
                    params={
                        "api_key": api_key,
                        "format": "json",
                        "limit": limit,
                        "offset": offset,
                    },
                )
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                if not entities:
                    return Finding(notes=f"Congress.gov member search error: {exc}")
                break

            data = resp.json()
            members = data.get("members", [])
            if not members:
                break

            for member in members:
                name = member.get("name", "")
                if query_lower in name.lower():
                    ent = self._build_member_entity(member)
                    entities.append(ent)

            # Stop if we've found enough or no more pages.
            if len(entities) >= 20 or len(members) < limit:
                break
            offset += limit

        return Finding(
            entities=entities,
            notes=(
                f"Congress.gov: {len(entities)} member(s) matching '{query}'"
            ),
        )

    async def _search_bills(
        self,
        client: httpx.AsyncClient,
        api_key: str,
        query: str,
    ) -> Finding:
        """Search for bills by keyword (client-side title filtering).

        The Congress.gov /bill endpoint does not support text search,
        so we paginate recent bills and filter by title.
        """
        query_lower = query.lower()
        all_matches: list[dict] = []
        offset = 0
        limit = 250

        for _ in range(4):  # Scan up to 1000 recent bills.
            try:
                resp = await client.get(
                    f"{_BASE}/bill",
                    params={
                        "api_key": api_key,
                        "format": "json",
                        "limit": limit,
                        "offset": offset,
                        "sort": "updateDate+desc",
                    },
                )
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                if not all_matches:
                    return Finding(notes=f"Congress.gov bill search error: {exc}")
                break

            data = resp.json()
            bills = data.get("bills", [])
            if not bills:
                break

            for bill in bills:
                title = (bill.get("title") or "").lower()
                if query_lower in title:
                    all_matches.append(bill)

            if len(all_matches) >= 20 or len(bills) < limit:
                break
            offset += limit

        if not all_matches:
            return Finding(
                notes=f"Congress.gov: no bills matching '{query}' in recent legislation",
            )

        entities: list[Entity] = []
        relationships: list[Relationship] = []

        for bill in all_matches[:20]:
            ent = self._build_bill_entity(bill)
            entities.append(ent)

            # Link sponsors.
            sponsors = bill.get("sponsors", [])
            for sponsor in sponsors[:5]:
                sponsor_name = sponsor.get("fullName") or sponsor.get("name", "")
                if sponsor_name:
                    sponsor_id = f"person:congress:{_slug(sponsor_name)}"
                    sponsor_ent = Entity(
                        id=sponsor_id,
                        entity_type=EntityType.PERSON,
                        label=sponsor_name,
                        properties={
                            "party": sponsor.get("party", ""),
                            "state": sponsor.get("state", ""),
                        },
                        sources=[_SOURCE()],
                    )
                    if not any(e.id == sponsor_id for e in entities):
                        entities.append(sponsor_ent)
                    relationships.append(Relationship(
                        source_id=sponsor_id,
                        target_id=ent.id,
                        relation_type=RelationType.FILED,
                        properties={"role": "sponsor"},
                        sources=[_SOURCE()],
                    ))

        return Finding(
            entities=entities,
            relationships=relationships,
            notes=(
                f"Congress.gov: {len(bills)} bill(s) matching '{query}'"
            ),
        )

    def _build_member_entity(self, member: dict) -> Entity:
        """Build a person entity from a Congress.gov member record."""
        name = member.get("name", "Unknown Member")
        bioguide = member.get("bioguideId", "")
        state = member.get("state", "")
        party = member.get("partyName", "")

        terms = member.get("terms", {})
        items = terms.get("item", []) if isinstance(terms, dict) else []

        current_chamber = ""
        if items:
            latest = items[0] if isinstance(items, list) else items
            if isinstance(latest, dict):
                current_chamber = latest.get("chamber", "")

        props = {
            k: v for k, v in {
                "bioguide_id": bioguide,
                "state": state,
                "party": party,
                "chamber": current_chamber,
                "url": member.get("url"),
                "depiction_url": member.get("depiction", {}).get("imageUrl")
                    if isinstance(member.get("depiction"), dict) else None,
            }.items() if v
        }

        return Entity(
            id=f"person:congress:{bioguide or _slug(name)}",
            entity_type=EntityType.PERSON,
            label=name,
            properties=props,
            sources=[_SOURCE(
                f"https://www.congress.gov/member/{_slug(name)}/{bioguide}"
                if bioguide else None
            )],
        )

    def _build_bill_entity(self, bill: dict) -> Entity:
        """Build a document entity from a Congress.gov bill record."""
        bill_type = bill.get("type", "")
        bill_num = bill.get("number", "")
        congress = bill.get("congress", "")
        title = bill.get("title", f"{bill_type} {bill_num}")

        bill_id = f"{bill_type.lower()}{bill_num}-{congress}"
        url = bill.get("url", "")
        congress_url = f"https://www.congress.gov/bill/{congress}th-congress/{_bill_type_path(bill_type)}/{bill_num}"

        props = {
            k: v for k, v in {
                "bill_type": bill_type,
                "bill_number": bill_num,
                "congress": congress,
                "origin_chamber": bill.get("originChamber"),
                "latest_action": bill.get("latestAction", {}).get("text")
                    if isinstance(bill.get("latestAction"), dict) else None,
                "latest_action_date": bill.get("latestAction", {}).get("actionDate")
                    if isinstance(bill.get("latestAction"), dict) else None,
                "url": congress_url,
            }.items() if v
        }

        return Entity(
            id=f"document:congress:{bill_id}",
            entity_type=EntityType.DOCUMENT,
            label=title[:200],
            properties=props,
            sources=[_SOURCE(congress_url)],
        )


def _bill_type_path(bill_type: str) -> str:
    """Map bill type code to URL path segment."""
    mapping = {
        "HR": "house-bill",
        "S": "senate-bill",
        "HRES": "house-resolution",
        "SRES": "senate-resolution",
        "HJRES": "house-joint-resolution",
        "SJRES": "senate-joint-resolution",
        "HCONRES": "house-concurrent-resolution",
        "SCONRES": "senate-concurrent-resolution",
    }
    return mapping.get(bill_type.upper(), "bill")


def _slug(s: str) -> str:
    import re
    if not s:
        return "unknown"
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")[:50]
