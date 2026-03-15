"""SBIR.gov tool adapter — Small Business Innovation Research award data."""

import asyncio
import re

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

BASE_URL = "https://api.www.sbir.gov/public/api/awards"

# Max rows we'll request from the API to avoid overloading
MAX_API_ROWS = 500


class SbirAdapter(ToolAdapter):
    """Queries the SBIR.gov API for SBIR/STTR award data.

    Provides:
    - Firm search: find awards by company name
    - PI search: find awards by Principal Investigator name

    The public API supports: firm, ri (researcher/PI), agency, year,
    rows, start, format.  No general keyword/full-text param exists,
    so keyword mode falls back to firm search.
    """

    name = "sbir"

    def __init__(self, timeout: int = 30):
        self.timeout = timeout

    def is_available(self) -> bool:
        return True

    async def run(
        self,
        query: str,
        mode: str = "firm",
        max_results: int = 50,
    ) -> Finding:
        """Search SBIR.gov for SBIR/STTR awards.

        Args:
            query: Company name, PI name, or keyword to search.
            mode: "firm" to search by company name,
                  "pi" to search by Principal Investigator,
                  "keyword" for full-text search.
            max_results: Maximum results to return.
        """
        params = {"rows": min(max_results, MAX_API_ROWS)}

        if mode == "pi":
            params["ri"] = query
        else:
            # keyword mode falls back to firm search (API has no
            # general full-text param)
            params["firm"] = query

        return await self._fetch_awards(params, query, mode)

    async def _fetch_awards(
        self, params: dict, query: str, mode: str,
    ) -> Finding:
        """Execute the SBIR API search and parse results."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for attempt in range(3):
                resp = await client.get(BASE_URL, params=params)
                if resp.status_code in (429, 503):
                    wait = 2 ** attempt
                    await asyncio.sleep(wait)
                    continue
                if resp.status_code in (403, 429, 503):
                    return Finding(
                        entities=[],
                        relationships=[],
                        notes=(
                            f"SBIR API returned {resp.status_code} for "
                            f"'{query}' — API may be under maintenance."
                        ),
                    )
                resp.raise_for_status()
                break
            else:
                # Exhausted retries (429/503)
                return Finding(
                    entities=[],
                    relationships=[],
                    notes=(
                        f"SBIR API returned {resp.status_code} after "
                        f"3 retries for '{query}' — API may be under "
                        f"maintenance or rate-limited."
                    ),
                )
            data = resp.json()

        # API returns a JSON array directly
        results = data if isinstance(data, list) else []

        entities: list[Entity] = []
        relationships: list[Relationship] = []
        seen_orgs: dict[str, str] = {}  # normalized_name -> entity_id
        seen_pis: dict[str, str] = {}   # normalized_name -> entity_id
        total_amount = 0.0

        for award in results:
            tracking_number = award.get("Agency Tracking Number", "")
            proposal_title = award.get("Proposal Title", "")
            company = award.get("Company", "")
            pi_name = award.get("PI", "")
            amount_str = award.get("Amount", "$0")
            agency = award.get("Agency", "")
            branch = award.get("Branch", "")
            program = award.get("Program", "")
            phase = award.get("Phase", "")
            contract = award.get("Contract", "")
            award_year = award.get("Award Year", "")
            pi_title = award.get("PI Title", "")
            pi_phone = award.get("PI Phone", "")
            abstract = award.get("Abstract", "")
            address = award.get("Address", "")
            city = award.get("City", "")
            state = award.get("State", "")
            zipcode = award.get("Zip", "")
            duns = award.get("DUNS", "")
            hubzone = award.get("HUBZone Owned", "")
            woman_owned = award.get("Woman Owned", "")
            disadvantaged = award.get(
                "Socially and Economically Disadvantaged", "",
            )

            amount = _parse_amount(amount_str)
            total_amount += amount

            # DOCUMENT entity for each award
            cleaned_tracking = _clean_tracking_number(tracking_number)
            doc_id = f"document:sbir:{cleaned_tracking}"
            doc_label = proposal_title if proposal_title else cleaned_tracking

            truncated_abstract = abstract[:500] if abstract else ""

            entities.append(Entity(
                id=doc_id,
                entity_type=EntityType.DOCUMENT,
                label=doc_label,
                properties={
                    "agency": agency,
                    "branch": branch,
                    "program": program,
                    "phase": phase,
                    "contract_number": contract,
                    "amount": amount,
                    "award_year": award_year,
                    "pi_name": pi_name,
                    "pi_title": pi_title,
                    "abstract": truncated_abstract,
                },
                sources=[Source(
                    tool=self.name,
                    source_url="https://www.sbir.gov/",
                )],
            ))

            # ORGANIZATION entity (deduplicated)
            if company:
                normalized_company = _normalize_name(company)
                if normalized_company not in seen_orgs:
                    org_id = f"org:sbir:{normalized_company}"
                    seen_orgs[normalized_company] = org_id
                    entities.append(Entity(
                        id=org_id,
                        entity_type=EntityType.ORGANIZATION,
                        label=company,
                        properties={
                            "address": address,
                            "city": city,
                            "state": state,
                            "zip": zipcode,
                            "duns": duns,
                            "hubzone": hubzone,
                            "woman_owned": woman_owned,
                            "disadvantaged": disadvantaged,
                        },
                        sources=[Source(
                            tool=self.name,
                            source_url="https://www.sbir.gov/",
                        )],
                    ))

                org_id = seen_orgs[normalized_company]
                relationships.append(Relationship(
                    source_id=org_id,
                    target_id=doc_id,
                    relation_type=RelationType.FILED,
                    properties={
                        "amount": amount,
                        "agency": agency,
                        "program": program,
                    },
                    sources=[Source(
                        tool=self.name,
                        source_url="https://www.sbir.gov/",
                    )],
                ))

            # PERSON entity for the PI (deduplicated)
            if pi_name:
                normalized_pi = _normalize_name(pi_name)
                if normalized_pi not in seen_pis:
                    person_id = f"person:sbir:{normalized_pi}"
                    seen_pis[normalized_pi] = person_id
                    entities.append(Entity(
                        id=person_id,
                        entity_type=EntityType.PERSON,
                        label=pi_name,
                        properties={
                            "title": pi_title,
                            "phone": pi_phone,
                        },
                        sources=[Source(
                            tool=self.name,
                            source_url="https://www.sbir.gov/",
                        )],
                    ))

                person_id = seen_pis[normalized_pi]
                relationships.append(Relationship(
                    source_id=person_id,
                    target_id=doc_id,
                    relation_type=RelationType.FILED,
                    properties={
                        "role": "Principal Investigator",
                    },
                    sources=[Source(
                        tool=self.name,
                        source_url="https://www.sbir.gov/",
                    )],
                ))

        return Finding(
            entities=entities,
            relationships=relationships,
            notes=(
                f"SBIR {mode} search for '{query}': "
                f"{len(results)} awards totaling "
                f"${total_amount:,.2f} across "
                f"{len(seen_orgs)} companies, "
                f"{len(seen_pis)} PIs"
            ),
        )


def _normalize_name(name: str) -> str:
    """Normalize a name to a stable identifier fragment."""
    name = name.lower().strip()
    name = re.sub(r"[^a-z0-9]+", "_", name)
    return name.strip("_")


def _parse_amount(amount_str: str) -> float:
    """Parse a dollar amount string like '$69,731.00' to a float."""
    if not amount_str:
        return 0.0
    cleaned = re.sub(r"[^\d.]", "", amount_str)
    if not cleaned:
        return 0.0
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def _clean_tracking_number(tracking: str) -> str:
    """Clean a tracking number for use as an entity ID fragment."""
    tracking = tracking.strip()
    tracking = re.sub(r"[^a-zA-Z0-9\-]", "_", tracking)
    return tracking.strip("_") if tracking else "unknown"
