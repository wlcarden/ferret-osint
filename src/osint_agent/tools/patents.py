"""USPTO PatentsView tool adapter — patent search by inventor, assignee, or keyword."""

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

BASE_URL = "https://search.patentsview.org/api/v1/patent/"


class PatentsAdapter(ToolAdapter):
    """Queries the USPTO PatentsView PatentSearch API for patent data.

    Provides:
    - Inventor search (who has patents?)
    - Assignee/company search (what patents does an org hold?)
    - Keyword search (patents mentioning a topic)

    Requires a PATENTSVIEW_API_KEY environment variable.
    Request one at: https://patentsview-support.atlassian.net/servicedesk/customer/portal/1
    """

    name = "patents"

    def __init__(self, timeout: int = 30):
        self.timeout = timeout
        self.api_key = os.getenv("PATENTSVIEW_API_KEY", "")

    def is_available(self) -> bool:
        return bool(self.api_key)

    async def run(
        self,
        query: str,
        mode: str = "inventor",
        max_results: int = 25,
    ) -> Finding:
        """Search USPTO PatentsView.

        Args:
            query: Inventor name, company name, or keyword.
            mode: "inventor" to search by inventor name,
                  "assignee" to search by company/assignee,
                  "keyword" to search by title keyword.
            max_results: Maximum results to return.
        """
        if mode == "assignee":
            return await self._search_assignee(query, max_results)
        elif mode == "keyword":
            return await self._search_keyword(query, max_results)
        return await self._search_inventor(query, max_results)

    def _headers(self) -> dict:
        """Build request headers with API key."""
        return {"X-Api-Key": self.api_key}

    def _build_inventor_query(self, query: str) -> dict:
        """Build the query clause for inventor name search."""
        parts = query.strip().split(None, 1)
        if len(parts) == 2:
            first, last = parts
            return {
                "_and": [
                    {
                        "_contains": {
                            "inventors.inventor_name_last": last,
                        },
                    },
                    {
                        "_contains": {
                            "inventors.inventor_name_first": first,
                        },
                    },
                ]
            }
        return {"_contains": {"inventors.inventor_name_last": parts[0]}}

    async def _fetch_patents(
        self,
        query_clause: dict,
        fields: list[str],
        max_results: int,
    ) -> dict:
        """GET from PatentsView and return the parsed JSON response."""
        import json

        params = {
            "q": json.dumps(query_clause),
            "f": json.dumps(fields),
            "o": json.dumps({"size": min(max_results, 1000)}),
            "s": json.dumps([{"patent_date": "desc"}]),
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(
                BASE_URL,
                params=params,
                headers=self._headers(),
            )
            resp.raise_for_status()
            return resp.json()

    def _parse_patents(self, data: dict) -> Finding:
        """Convert PatentsView response into entities and relationships."""
        entities: list[Entity] = []
        relationships: list[Relationship] = []
        seen_inventors: set[str] = set()
        seen_assignees: set[str] = set()

        patents = data.get("patents") or []

        for patent in patents:
            patent_id = patent.get("patent_id", "")
            patent_title = patent.get("patent_title", "")
            patent_date = patent.get("patent_date", "")
            abstract = patent.get("patent_abstract", "") or ""

            doc_id = f"document:patent:{patent_id}"
            source = Source(
                tool=self.name,
                source_url=(
                    f"https://patents.google.com/patent/US{patent_id}"
                ),
            )

            entities.append(Entity(
                id=doc_id,
                entity_type=EntityType.DOCUMENT,
                label=patent_title,
                properties={
                    "patent_number": patent_id,
                    "patent_date": patent_date,
                    "abstract": abstract[:500],
                    "source_system": "patentsview",
                },
                sources=[source],
            ))

            # Process inventors (nested array in new API)
            for inv in patent.get("inventors") or []:
                first = inv.get("inventor_name_first", "") or ""
                last = inv.get("inventor_name_last", "") or ""
                if not last:
                    continue
                full_name = f"{first} {last}".strip()
                person_id = (
                    f"person:patent:"
                    f"{full_name.lower().replace(' ', '_')}"
                )

                if person_id not in seen_inventors:
                    seen_inventors.add(person_id)
                    props: dict = {"source_system": "patentsview"}
                    city = inv.get("inventor_city", "")
                    state = inv.get("inventor_state", "")
                    country = inv.get("inventor_country", "")
                    if city:
                        props["city"] = city
                    if state:
                        props["state"] = state
                    if country:
                        props["country"] = country
                    entities.append(Entity(
                        id=person_id,
                        entity_type=EntityType.PERSON,
                        label=full_name,
                        properties=props,
                        sources=[source],
                    ))

                relationships.append(Relationship(
                    source_id=person_id,
                    target_id=doc_id,
                    relation_type=RelationType.FILED,
                    properties={},
                    sources=[source],
                ))

            # Process assignees (nested array in new API)
            for asn in patent.get("assignees") or []:
                org_name = asn.get("assignee_organization", "") or ""
                if not org_name:
                    first = (
                        asn.get("assignee_individual_name_first", "") or ""
                    )
                    last = (
                        asn.get("assignee_individual_name_last", "") or ""
                    )
                    org_name = f"{first} {last}".strip()
                if not org_name:
                    continue
                normalized = org_name.lower().replace(" ", "_")
                org_id = f"org:patent:{normalized}"

                if org_id not in seen_assignees:
                    seen_assignees.add(org_id)
                    entities.append(Entity(
                        id=org_id,
                        entity_type=EntityType.ORGANIZATION,
                        label=org_name,
                        properties={"source_system": "patentsview"},
                        sources=[source],
                    ))

                relationships.append(Relationship(
                    source_id=org_id,
                    target_id=doc_id,
                    relation_type=RelationType.OWNS,
                    properties={},
                    sources=[source],
                ))

        total = data.get("total_hits", len(patents))
        return Finding(
            entities=entities,
            relationships=relationships,
            notes=(
                f"PatentsView: {len(patents)} patents returned"
                f" (total: {total})"
            ),
        )

    async def _search_inventor(
        self,
        query: str,
        max_results: int,
    ) -> Finding:
        """Search patents by inventor name."""
        fields = [
            "patent_id",
            "patent_title",
            "patent_date",
            "patent_abstract",
            "inventors.inventor_name_first",
            "inventors.inventor_name_last",
            "inventors.inventor_city",
            "inventors.inventor_state",
            "inventors.inventor_country",
            "assignees.assignee_organization",
            "assignees.assignee_individual_name_first",
            "assignees.assignee_individual_name_last",
        ]
        query_clause = self._build_inventor_query(query)
        data = await self._fetch_patents(query_clause, fields, max_results)
        finding = self._parse_patents(data)
        finding.notes = (
            f"PatentsView inventor search for '{query}': "
            f"{len(finding.entities)} entities found"
        )
        return finding

    async def _search_assignee(
        self,
        query: str,
        max_results: int,
    ) -> Finding:
        """Search patents by assignee/company name."""
        fields = [
            "patent_id",
            "patent_title",
            "patent_date",
            "patent_abstract",
            "inventors.inventor_name_first",
            "inventors.inventor_name_last",
            "assignees.assignee_organization",
        ]
        query_clause = {
            "_contains": {"assignees.assignee_organization": query},
        }
        data = await self._fetch_patents(query_clause, fields, max_results)
        finding = self._parse_patents(data)
        finding.notes = (
            f"PatentsView assignee search for '{query}': "
            f"{len(finding.entities)} entities found"
        )
        return finding

    async def _search_keyword(
        self,
        query: str,
        max_results: int,
    ) -> Finding:
        """Search patents by title/abstract keyword."""
        fields = [
            "patent_id",
            "patent_title",
            "patent_date",
            "patent_abstract",
            "inventors.inventor_name_first",
            "inventors.inventor_name_last",
            "assignees.assignee_organization",
        ]
        query_clause = {"_text_any": {"patent_title": query}}
        data = await self._fetch_patents(query_clause, fields, max_results)
        finding = self._parse_patents(data)
        finding.notes = (
            f"PatentsView keyword search for '{query}': "
            f"{len(finding.entities)} entities found"
        )
        return finding
