"""ProPublica Nonprofit Explorer adapter — nonprofit financial data.

Queries ProPublica's Nonprofit Explorer API for tax return data
on 3M+ US nonprofits. Returns executive compensation, revenue,
expenses, and organizational details from Form 990 filings.
No API key or authentication required.
"""

import logging

import httpx

from osint_agent.models import (
    Entity,
    EntityType,
    Finding,
    Relationship,
    Source,
)
from osint_agent.tools.base import ToolAdapter

logger = logging.getLogger(__name__)

_BASE = "https://projects.propublica.org/nonprofits/api/v2"
_SOURCE = lambda: Source(tool="propublica_nonprofit")


class ProPublicaNonprofitAdapter(ToolAdapter):
    """Search ProPublica Nonprofit Explorer for nonprofit financial data."""

    name = "propublica_nonprofit"

    def __init__(self, timeout: int = 20):
        self.timeout = timeout

    def is_available(self) -> bool:
        return True  # Only needs httpx

    async def run(self, name: str, ein: str = "", **kwargs) -> Finding:
        """Search for a nonprofit by name or look up by EIN.

        Args:
            name: Organization name to search for.
            ein: Employer Identification Number (if known, skips search).
        """
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            if ein:
                return await self._lookup_ein(client, ein)
            return await self._search_name(client, name)

    async def _search_name(
        self,
        client: httpx.AsyncClient,
        name: str,
    ) -> Finding:
        """Search nonprofits by name."""
        try:
            resp = await client.get(
                f"{_BASE}/search.json",
                params={"q": name},
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("ProPublica search failed: %s", exc)
            return Finding(notes=f"ProPublica Nonprofit error: {exc}")

        data = resp.json()
        orgs = data.get("organizations", [])

        if not orgs:
            return Finding(notes=f"ProPublica: no nonprofits found for '{name}'")

        # Filter to results whose name is relevant to the query.
        # ProPublica's search is very fuzzy and returns unrelated orgs.
        relevant = [
            o for o in orgs
            if _is_relevant_result(name, o.get("name", ""))
        ]
        if not relevant:
            return Finding(
                notes=(
                    f"ProPublica: {len(orgs)} result(s) for '{name}' "
                    "but none matched closely enough"
                ),
            )
        orgs = relevant

        entities: list[Entity] = []
        relationships: list[Relationship] = []

        for org in orgs[:10]:
            ent = self._build_org_entity(org)
            entities.append(ent)

        # For the top result, fetch full details.
        top = orgs[0]
        top_ein = str(top.get("ein", ""))
        if top_ein:
            detail_ents, detail_rels = await self._fetch_details(
                client, top_ein, entities[0],
            )
            entities.extend(detail_ents)
            relationships.extend(detail_rels)

        return Finding(
            entities=entities,
            relationships=relationships,
            notes=(
                f"ProPublica Nonprofit: {len(orgs)} result(s) for '{name}'. "
                f"Top: {top.get('name', '?')} (EIN: {top_ein})"
            ),
        )

    async def _lookup_ein(
        self,
        client: httpx.AsyncClient,
        ein: str,
    ) -> Finding:
        """Look up a specific nonprofit by EIN."""
        ein_clean = ein.replace("-", "")
        try:
            resp = await client.get(f"{_BASE}/organizations/{ein_clean}.json")
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return Finding(notes=f"ProPublica: no nonprofit with EIN {ein}")
            return Finding(notes=f"ProPublica error: {exc.response.status_code}")
        except httpx.HTTPError as exc:
            return Finding(notes=f"ProPublica error: {exc}")

        data = resp.json()
        org_data = data.get("organization", {})
        filings = data.get("filings_with_data", []) or data.get("filings_without_data", [])

        ent = self._build_org_entity(org_data)
        entities = [ent]
        relationships: list[Relationship] = []

        # Process filings for financial data.
        detail_ents, detail_rels = self._process_filings(ent, filings)
        entities.extend(detail_ents)
        relationships.extend(detail_rels)

        return Finding(
            entities=entities,
            relationships=relationships,
            notes=(
                f"ProPublica Nonprofit: {org_data.get('name', '?')} "
                f"(EIN: {ein}) — {len(filings)} filing(s)"
            ),
        )

    async def _fetch_details(
        self,
        client: httpx.AsyncClient,
        ein: str,
        org_entity: Entity,
    ) -> tuple[list[Entity], list[Relationship]]:
        """Fetch full org details and filings by EIN."""
        try:
            resp = await client.get(f"{_BASE}/organizations/{ein}.json")
            resp.raise_for_status()
        except httpx.HTTPError:
            return [], []

        data = resp.json()
        filings = data.get("filings_with_data", [])
        return self._process_filings(org_entity, filings)

    def _process_filings(
        self,
        org_entity: Entity,
        filings: list[dict],
    ) -> tuple[list[Entity], list[Relationship]]:
        """Extract financial data and key personnel from filings."""
        entities: list[Entity] = []
        relationships: list[Relationship] = []

        if not filings:
            return entities, relationships

        # Use most recent filing for financial snapshot.
        latest = filings[0]
        org_entity.properties.update({
            k: v for k, v in {
                "tax_period": latest.get("tax_prd_yr"),
                "total_revenue": latest.get("totrevenue"),
                "total_expenses": latest.get("totfuncexpns"),
                "total_assets": latest.get("totassetsend"),
                "total_liabilities": latest.get("totliabend"),
            }.items() if v is not None
        })

        # Extract officers/key employees if available.
        for filing in filings[:3]:
            pdf_url = filing.get("pdf_url")
            if pdf_url:
                org_entity.properties.setdefault("filing_urls", [])
                org_entity.properties["filing_urls"].append(pdf_url)

        return entities, relationships

    def _build_org_entity(self, org: dict) -> Entity:
        """Build an organization entity from search/detail result."""
        ein = str(org.get("ein", ""))
        name = org.get("name", "Unknown Nonprofit")

        props = {}
        for key, api_key in [
            ("ein", "ein"),
            ("city", "city"),
            ("state", "state"),
            ("ntee_code", "ntee_code"),
            ("subsection_code", "subseccd"),
            ("classification_codes", "classification_codes"),
            ("ruling_date", "ruling_date"),
            ("tax_period", "tax_period"),
            ("income_amount", "income_amount"),
            ("revenue_amount", "revenue_amt"),
            ("asset_amount", "asset_amount"),
        ]:
            val = org.get(api_key)
            if val is not None and val != "":
                props[key] = val

        # NTEE code descriptions for context.
        ntee = org.get("ntee_code", "")
        if ntee:
            props["ntee_description"] = _ntee_category(ntee)

        return Entity(
            id=f"organization:nonprofit:{ein or name.lower().replace(' ', '_')}",
            entity_type=EntityType.ORGANIZATION,
            label=name,
            properties={
                **props,
                "url": (
                    f"https://projects.propublica.org"
                    f"/nonprofits/organizations/{ein}"
                    if ein else None
                ),
                "organization_type": "nonprofit",
            },
            sources=[_SOURCE()],
        )


def _is_relevant_result(query: str, org_name: str) -> bool:
    """Check if a ProPublica search result is relevant to the query.

    The ProPublica search API uses aggressive fuzzy matching that
    returns results like "Water Wind Wine Ministries" for a query
    of "WINRED".  This filter requires at least one of:
      1. Direct substring match (query in name or vice versa)
      2. Acronym match ("NRA" → "National Rifle Association")
      3. Token-level overlap (a query word appears in a name word)
    """
    q_lower = query.lower().strip()
    n_lower = org_name.lower().strip()

    # Direct substring match covers exact and contains cases.
    if q_lower in n_lower or n_lower in q_lower:
        return True

    q_tokens = q_lower.split()
    n_tokens = n_lower.split()

    # Acronym match: "nra" == first letters of "national rifle association".
    if len(q_tokens) == 1 and len(n_tokens) >= 2:
        initials = "".join(t[0] for t in n_tokens if t)
        if q_lower == initials:
            return True

    # Token-level: at least one query token (3+ chars) must appear
    # as a substring in some name token or vice versa.
    for qt in q_tokens:
        if len(qt) < 3:
            continue
        for nt in n_tokens:
            if qt in nt or nt in qt:
                return True

    return False


def _ntee_category(code: str) -> str:
    """Map NTEE code prefix to category description."""
    categories = {
        "A": "Arts, Culture & Humanities",
        "B": "Education",
        "C": "Environment",
        "D": "Animal-Related",
        "E": "Health Care",
        "F": "Mental Health & Crisis",
        "G": "Disease/Disorder/Medical",
        "H": "Medical Research",
        "I": "Crime & Legal-Related",
        "J": "Employment",
        "K": "Food, Agriculture & Nutrition",
        "L": "Housing & Shelter",
        "M": "Public Safety",
        "N": "Recreation & Sports",
        "O": "Youth Development",
        "P": "Human Services",
        "Q": "International Affairs",
        "R": "Civil Rights, Social Action & Advocacy",
        "S": "Community Improvement",
        "T": "Philanthropy & Voluntarism",
        "U": "Science & Technology",
        "V": "Social Science",
        "W": "Public & Societal Benefit",
        "X": "Religion-Related",
        "Y": "Mutual & Membership Benefit",
        "Z": "Unknown",
    }
    if code:
        return categories.get(code[0].upper(), "Unknown")
    return "Unknown"
