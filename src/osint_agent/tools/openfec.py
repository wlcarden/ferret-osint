"""OpenFEC tool adapter — federal campaign finance data."""

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

BASE_URL = "https://api.open.fec.gov/v1"


class OpenFECAdapter(ToolAdapter):
    """Queries the OpenFEC API for campaign finance data.

    Provides:
    - Individual contributor search (who donated to whom)
    - Committee/PAC lookup
    - Candidate lookup
    """

    name = "openfec"
    required_env_key = "OPENFEC_API_KEY"

    def __init__(self, timeout: int = 30):
        self.timeout = timeout
        self.api_key = os.getenv("OPENFEC_API_KEY", "")

    async def run(
        self,
        query: str,
        mode: str = "contributors",
        max_results: int = 20,
        employer: str | None = None,
        occupation: str | None = None,
    ) -> Finding:
        """Search OpenFEC.

        Args:
            query: Person name, committee name, or candidate name.
            mode: "contributors" to find individual donations,
                  "committees" to search PACs/committees,
                  "candidates" to search candidates.
            max_results: Maximum results to return.
            employer: Filter contributors by employer name.
            occupation: Filter contributors by occupation.
        """
        if mode == "committees":
            return await self._search_committees(query, max_results)
        elif mode == "candidates":
            return await self._search_candidates(query, max_results)
        return await self._search_contributors(
            query, max_results, employer=employer, occupation=occupation,
        )

    async def _search_contributors(
        self,
        name: str,
        max_results: int,
        employer: str | None = None,
        occupation: str | None = None,
    ) -> Finding:
        """Search for individual campaign contributions by contributor name.

        This is the primary investigative use — "who has this person donated to?"
        """
        entities = []
        relationships = []

        params = {
            "api_key": self.api_key,
            "contributor_name": name,
            "sort": "-contribution_receipt_date",
            "per_page": min(max_results, 100),
            "is_individual": "true",
        }
        if employer:
            params["contributor_employer"] = employer
        if occupation:
            params["contributor_occupation"] = occupation

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(
                f"{BASE_URL}/schedules/schedule_a/",
                params=params,
            )
            resp.raise_for_status()
            data = resp.json()

        results = data.get("results", [])

        # Create entity for the contributor
        person_id = f"person:fec:{name.lower().replace(' ', '_')}"
        seen_committees = set()

        if results:
            first = results[0]
            person = Entity(
                id=person_id,
                entity_type=EntityType.PERSON,
                label=first.get("contributor_name", name),
                properties={
                    "employer": first.get("contributor_employer", ""),
                    "occupation": first.get("contributor_occupation", ""),
                    "state": first.get("contributor_state", ""),
                    "city": first.get("contributor_city", ""),
                    "zip": first.get("contributor_zip", ""),
                    "source_system": "openfec",
                },
                sources=[Source(tool=self.name, source_url=f"{BASE_URL}/schedules/schedule_a/")],
            )
            entities.append(person)

        total_donated = 0.0
        for contribution in results:
            amount = contribution.get("contribution_receipt_amount", 0) or 0
            total_donated += amount
            committee_id_raw = contribution.get("committee_id", "")
            committee_name = contribution.get("committee", {}).get("name", committee_id_raw)
            receipt_date = contribution.get("contribution_receipt_date", "")

            if committee_id_raw and committee_id_raw not in seen_committees:
                seen_committees.add(committee_id_raw)
                committee_entity_id = f"org:fec:{committee_id_raw}"
                entities.append(Entity(
                    id=committee_entity_id,
                    entity_type=EntityType.ORGANIZATION,
                    label=committee_name,
                    properties={
                        "fec_committee_id": committee_id_raw,
                        "source_system": "openfec",
                    },
                    sources=[Source(
                        tool=self.name,
                        source_url=f"https://www.fec.gov/data/committee/{committee_id_raw}/",
                    )],
                ))

            # Aggregate donations to each committee
            if committee_id_raw:
                # Sum all contributions to this committee
                committee_amounts = [
                    c.get("contribution_receipt_amount", 0) or 0
                    for c in results
                    if c.get("committee_id") == committee_id_raw
                ]
                if committee_id_raw not in {r.target_id.split(":")[-1] for r in relationships}:
                    relationships.append(Relationship(
                        source_id=person_id,
                        target_id=f"org:fec:{committee_id_raw}",
                        relation_type=RelationType.DONATED_TO,
                        properties={
                            "total_amount": sum(committee_amounts),
                            "contribution_count": len(committee_amounts),
                            "most_recent_date": receipt_date,
                        },
                        sources=[Source(
                            tool=self.name,
                            source_url=f"https://www.fec.gov/data/receipts/individual-contributions/?contributor_name={name}",
                        )],
                    ))

        return Finding(
            entities=entities,
            relationships=relationships,
            notes=f"OpenFEC: '{name}' made {len(results)} contributions totaling ${total_donated:,.2f} to {len(seen_committees)} committees",
        )

    async def _search_committees(self, query: str, max_results: int) -> Finding:
        """Search for PACs/committees by name."""
        entities = []

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(
                f"{BASE_URL}/committees/",
                params={
                    "api_key": self.api_key,
                    "q": query,
                    "per_page": min(max_results, 20),
                },
            )
            resp.raise_for_status()
            data = resp.json()

        for committee in data.get("results", []):
            cid = committee.get("committee_id", "")
            entities.append(Entity(
                id=f"org:fec:{cid}",
                entity_type=EntityType.ORGANIZATION,
                label=committee.get("name", ""),
                properties={
                    "fec_committee_id": cid,
                    "committee_type": committee.get("committee_type", ""),
                    "designation": committee.get("designation", ""),
                    "party": committee.get("party", ""),
                    "state": committee.get("state", ""),
                    "treasurer_name": committee.get("treasurer_name", ""),
                    "source_system": "openfec",
                },
                sources=[Source(
                    tool=self.name,
                    source_url=f"https://www.fec.gov/data/committee/{cid}/",
                )],
            ))

        return Finding(
            entities=entities,
            notes=f"OpenFEC committee search for '{query}': {len(entities)} results",
        )

    async def _search_candidates(self, query: str, max_results: int) -> Finding:
        """Search for candidates by name."""
        entities = []

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(
                f"{BASE_URL}/candidates/search/",
                params={
                    "api_key": self.api_key,
                    "q": query,
                    "per_page": min(max_results, 20),
                },
            )
            resp.raise_for_status()
            data = resp.json()

        for candidate in data.get("results", []):
            cand_id = candidate.get("candidate_id", "")
            entities.append(Entity(
                id=f"person:fec:candidate:{cand_id}",
                entity_type=EntityType.PERSON,
                label=candidate.get("name", ""),
                properties={
                    "fec_candidate_id": cand_id,
                    "office": candidate.get("office_full", ""),
                    "party": candidate.get("party_full", ""),
                    "state": candidate.get("state", ""),
                    "district": candidate.get("district", ""),
                    "active_through": candidate.get("active_through"),
                    "source_system": "openfec",
                },
                sources=[Source(
                    tool=self.name,
                    source_url=f"https://www.fec.gov/data/candidate/{cand_id}/",
                )],
            ))

        return Finding(
            entities=entities,
            notes=f"OpenFEC candidate search for '{query}': {len(entities)} results",
        )
