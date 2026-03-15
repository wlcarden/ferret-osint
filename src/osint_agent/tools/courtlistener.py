"""CourtListener/RECAP tool adapter — federal court records."""

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

BASE_URL = "https://www.courtlistener.com/api/rest/v4"


class CourtListenerAdapter(ToolAdapter):
    """Queries the CourtListener REST API for federal court records.

    Provides:
    - Docket search by party name, case name, or keyword
    - Opinion full-text search
    - Party extraction from docket data
    """

    name = "courtlistener"
    required_env_key = "COURTLISTENER_API_KEY"

    def __init__(self, timeout: int = 30):
        self.timeout = timeout
        self.api_key = os.getenv("COURTLISTENER_API_KEY", "")

    def _headers(self) -> dict:
        return {"Authorization": f"Token {self.api_key}"}

    async def run(self, query: str, mode: str = "dockets", max_results: int = 20) -> Finding:
        """Search CourtListener.

        Args:
            query: Search term (person name, company name, case keyword).
            mode: "dockets" for case search, "opinions" for opinion text search.
            max_results: Maximum results to return.
        """
        if mode == "opinions":
            return await self._search_opinions(query, max_results)
        return await self._search_dockets(query, max_results)

    async def _search_dockets(self, query: str, max_results: int) -> Finding:
        """Search dockets via the v4 search endpoint."""
        entities = []
        relationships = []

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(
                f"{BASE_URL}/search/",
                headers=self._headers(),
                params={
                    "q": query,
                    "type": "r",  # RECAP docket search
                    "page_size": min(max_results, 20),
                },
            )
            resp.raise_for_status()
            data = resp.json()

        results = data.get("results", [])
        for docket in results:
            docket_id = docket.get("docket_id", docket.get("id", ""))
            case_name = docket.get("caseName", docket.get("case_name", "Unknown Case"))
            court = docket.get("court", "")
            date_filed = docket.get("dateFiled", docket.get("date_filed", ""))
            docket_number = docket.get("docketNumber", docket.get("docket_number", ""))
            cause = docket.get("cause", "")

            doc_entity_id = f"document:cl:{docket_id}"
            entities.append(Entity(
                id=doc_entity_id,
                entity_type=EntityType.DOCUMENT,
                label=case_name,
                properties={
                    "court": court,
                    "date_filed": date_filed,
                    "docket_number": docket_number,
                    "cause": cause,
                    "source_system": "courtlistener",
                },
                sources=[Source(
                    tool=self.name,
                    source_url=f"https://www.courtlistener.com/docket/{docket_id}/",
                    raw_data=docket,
                )],
            ))

        return Finding(
            entities=entities,
            relationships=relationships,
            notes=f"CourtListener docket search for '{query}': {len(results)} results",
        )

    async def _search_opinions(self, query: str, max_results: int) -> Finding:
        """Full-text search of court opinions."""
        entities = []

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(
                f"{BASE_URL}/search/",
                headers=self._headers(),
                params={
                    "q": query,
                    "type": "o",  # opinions
                    "order_by": "score desc",
                    "page_size": min(max_results, 20),
                },
            )
            resp.raise_for_status()
            data = resp.json()

        results = data.get("results", [])
        for opinion in results:
            op_id = opinion.get("id", "")
            case_name = opinion.get("caseName", opinion.get("case_name", "Unknown"))
            court = opinion.get("court", "")
            date_filed = opinion.get("dateFiled", opinion.get("date_filed", ""))
            snippet = opinion.get("snippet", "")

            entities.append(Entity(
                id=f"document:cl:opinion:{op_id}",
                entity_type=EntityType.DOCUMENT,
                label=case_name,
                properties={
                    "document_type": "opinion",
                    "court": court,
                    "date_filed": date_filed,
                    "snippet": snippet[:500],
                    "source_system": "courtlistener",
                },
                sources=[Source(
                    tool=self.name,
                    source_url=f"https://www.courtlistener.com/opinion/{op_id}/",
                    raw_data=opinion,
                )],
            ))

        return Finding(
            entities=entities,
            notes=f"CourtListener opinion search for '{query}': {len(results)} results",
        )

    async def search_party(self, name: str, max_results: int = 20) -> Finding:
        """Search for a person/organization as a party in court cases.

        This is the primary investigative use — "has this person been in court?"
        Creates PARTY_TO relationships between the person and case documents.
        """
        entities = []
        relationships = []

        # Search dockets where this name appears as a party
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(
                f"{BASE_URL}/search/",
                headers=self._headers(),
                params={
                    "q": f'"{name}"',
                    "type": "r",
                    "page_size": min(max_results, 20),
                },
            )
            resp.raise_for_status()
            data = resp.json()

        # Create a person/org entity for the search target
        person_id = f"person:cl_search:{name.lower().replace(' ', '_')}"
        person = Entity(
            id=person_id,
            entity_type=EntityType.PERSON,
            label=name,
            sources=[Source(tool=self.name)],
        )
        entities.append(person)

        results = data.get("results", [])
        for docket in results:
            docket_id = docket.get("docket_id", docket.get("id", ""))
            case_name = docket.get("caseName", docket.get("case_name", "Unknown"))
            court = docket.get("court", "")
            date_filed = docket.get("dateFiled", docket.get("date_filed", ""))

            doc_id = f"document:cl:{docket_id}"
            entities.append(Entity(
                id=doc_id,
                entity_type=EntityType.DOCUMENT,
                label=case_name,
                properties={
                    "court": court,
                    "date_filed": date_filed,
                    "docket_number": docket.get("docketNumber", docket.get("docket_number", "")),
                    "source_system": "courtlistener",
                },
                sources=[Source(
                    tool=self.name,
                    source_url=f"https://www.courtlistener.com/docket/{docket_id}/",
                )],
            ))

            relationships.append(Relationship(
                source_id=person_id,
                target_id=doc_id,
                relation_type=RelationType.PARTY_TO,
                properties={"case_name": case_name, "date_filed": date_filed},
                sources=[Source(
                    tool=self.name,
                    source_url=f"https://www.courtlistener.com/docket/{docket_id}/",
                )],
            ))

        return Finding(
            entities=entities,
            relationships=relationships,
            notes=f"CourtListener party search for '{name}': {len(results)} cases found",
        )
