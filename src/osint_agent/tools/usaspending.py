"""USASpending.gov tool adapter — federal contract and grant award data."""

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

BASE_URL = "https://api.usaspending.gov/api/v2/"

# Award type codes: A-D are contracts
CONTRACT_AWARD_TYPES = ["A", "B", "C", "D"]

# Standard fields to request from the spending_by_award endpoint
AWARD_FIELDS = [
    "Award ID",
    "Recipient Name",
    "Award Amount",
    "Awarding Agency",
    "Start Date",
    "End Date",
    "Description",
    "recipient_id",
]


class UsaSpendingAdapter(ToolAdapter):
    """Queries the USASpending.gov API for federal contract award data.

    Provides:
    - Recipient search: find contracts awarded to a company/organization
    - Keyword search: full-text search across award descriptions
    """

    name = "usaspending"

    def __init__(self, timeout: int = 30):
        self.timeout = timeout

    def is_available(self) -> bool:
        return True

    async def run(
        self,
        query: str,
        mode: str = "recipient",
        max_results: int = 25,
    ) -> Finding:
        """Search USASpending.gov for federal contract awards.

        Args:
            query: Company name, organization, or keyword to search.
            mode: "recipient" to search by recipient name,
                  "keyword" for full-text search across awards.
            max_results: Maximum results to return.
        """
        if mode == "keyword":
            return await self._search_keyword(query, max_results)
        return await self._search_recipient(query, max_results)

    async def _search_recipient(
        self, query: str, max_results: int,
    ) -> Finding:
        """Search for federal contract awards by recipient name."""
        body = {
            "filters": {
                "recipient_search_text": [query],
                "award_type_codes": CONTRACT_AWARD_TYPES,
            },
            "fields": AWARD_FIELDS,
            "limit": min(max_results, 100),
            "order": "desc",
            "sort": "Award Amount",
        }
        return await self._fetch_awards(body, query, "recipient")

    async def _search_keyword(
        self, query: str, max_results: int,
    ) -> Finding:
        """Full-text keyword search across federal contract awards."""
        body = {
            "filters": {
                "keywords": [query],
                "award_type_codes": CONTRACT_AWARD_TYPES,
            },
            "fields": AWARD_FIELDS,
            "limit": min(max_results, 100),
            "order": "desc",
            "sort": "Award Amount",
        }
        return await self._fetch_awards(body, query, "keyword")

    async def _fetch_awards(
        self, body: dict, query: str, mode: str,
    ) -> Finding:
        """Execute the spending_by_award search and parse results."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{BASE_URL}search/spending_by_award/",
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()

        results = data.get("results", [])
        entities = []
        relationships = []
        seen_recipients: dict[str, str] = {}  # normalized_name -> entity_id
        total_amount = 0.0

        for award in results:
            award_id = award.get("Award ID", "")
            recipient_name = award.get("Recipient Name", "")
            award_amount = award.get("Award Amount") or 0
            awarding_agency = award.get("Awarding Agency", "")
            start_date = award.get("Start Date", "")
            end_date = award.get("End Date", "")
            description = award.get("Description", "")

            total_amount += float(award_amount)

            # Create DOCUMENT entity for each award
            doc_id = f"document:usaspending:{award_id}"
            doc_label = description if description else award_id
            entities.append(Entity(
                id=doc_id,
                entity_type=EntityType.DOCUMENT,
                label=doc_label,
                properties={
                    "award_id": award_id,
                    "award_amount": award_amount,
                    "awarding_agency": awarding_agency,
                    "start_date": start_date,
                    "end_date": end_date,
                    "recipient_name": recipient_name,
                    "source_system": "usaspending",
                },
                sources=[Source(
                    tool=self.name,
                    source_url=(
                        f"https://www.usaspending.gov/award/{award_id}"
                    ),
                )],
            ))

            # Extract ORGANIZATION entity for each unique recipient
            if recipient_name:
                normalized = _normalize_name(recipient_name)
                if normalized not in seen_recipients:
                    org_id = f"org:usaspending:{normalized}"
                    seen_recipients[normalized] = org_id
                    entities.append(Entity(
                        id=org_id,
                        entity_type=EntityType.ORGANIZATION,
                        label=recipient_name,
                        properties={
                            "source_system": "usaspending",
                        },
                        sources=[Source(
                            tool=self.name,
                            source_url=(
                                "https://www.usaspending.gov"
                                f"/search/?searchText={recipient_name}"
                            ),
                        )],
                    ))

                # Create TRANSACTED_WITH relationship from org to document
                org_id = seen_recipients[normalized]
                relationships.append(Relationship(
                    source_id=org_id,
                    target_id=doc_id,
                    relation_type=RelationType.TRANSACTED_WITH,
                    properties={
                        "award_amount": award_amount,
                        "awarding_agency": awarding_agency,
                    },
                    sources=[Source(
                        tool=self.name,
                        source_url=(
                            f"https://www.usaspending.gov/award/{award_id}"
                        ),
                    )],
                ))

        return Finding(
            entities=entities,
            relationships=relationships,
            notes=(
                f"USASpending {mode} search for '{query}': "
                f"{len(results)} awards totaling "
                f"${total_amount:,.2f} across "
                f"{len(seen_recipients)} recipients"
            ),
        )


def _normalize_name(name: str) -> str:
    """Normalize a recipient name to a stable identifier fragment."""
    name = name.lower().strip()
    name = re.sub(r"[^a-z0-9]+", "_", name)
    return name.strip("_")
