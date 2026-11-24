"""ICE/HSI leadership tracker — identifies who holds government positions.

Scrapes ICE press releases and news pages to extract names attributed
to specific leadership roles (e.g., "Acting Special Agent in Charge")
at specific field offices, with dates. Useful for identifying the
individuals behind redacted government documents when you know the
office and approximate date.

Strategy:
  1. Search ice.gov/hsi/news for press releases mentioning the target
     office (e.g., "HSI Washington").
  2. Parse each release for attribution patterns like:
     "said [Name], [Title] of [Office]"
  3. Build a temporal map of name→role→date_range.

This adapter requires no API key — it scrapes public ice.gov pages.
"""

import logging
import re
from datetime import datetime

import httpx
from bs4 import BeautifulSoup

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

_BASE_URL = "https://www.ice.gov"

# Patterns that attribute a quote or action to a named person with a title.
# Captures: (Name, Title, Office)
_ATTRIBUTION_PATTERNS = [
    # "said John Smith, Acting Special Agent in Charge of HSI Washington"
    re.compile(
        r'said\s+'
        r'([A-Z][a-z]+(?:\s+[A-Z]\.?)?\s+[A-Z][a-z]+(?:-[A-Z][a-z]+)?)'
        r',\s+'
        r'((?:Acting\s+)?(?:Special\s+Agent\s+in\s+Charge|'
        r'Deputy\s+Special\s+Agent\s+in\s+Charge|'
        r'Field\s+Office\s+Director|'
        r'Executive\s+Associate\s+Director|'
        r'Assistant\s+Director|'
        r'Deputy\s+Director|'
        r'Director))'
        r'(?:\s+(?:of|for)\s+)?'
        r'([^."]*?)'
        r'(?:\.|")',
        re.IGNORECASE,
    ),
    # "HSI Washington Acting SAC John Smith said..."
    re.compile(
        r'((?:HSI|ICE|ERO)\s+[A-Za-z.,\s]+?)\s+'
        r'((?:Acting\s+)?(?:Special\s+Agent\s+in\s+Charge|SAC|'
        r'Field\s+Office\s+Director|FOD))\s+'
        r'([A-Z][a-z]+(?:\s+[A-Z]\.?)?\s+[A-Z][a-z]+(?:-[A-Z][a-z]+)?)',
        re.IGNORECASE,
    ),
]

# Date patterns in press release text
_DATE_PATTERN = re.compile(
    r'(?:January|February|March|April|May|June|July|August|'
    r'September|October|November|December)\s+\d{1,2},?\s+\d{4}',
)


class IceLeadershipAdapter(ToolAdapter):
    """Scrapes ICE/HSI press releases to identify field office leadership.

    No API key required — uses public ice.gov pages.
    """

    name = "ice_leadership"

    def __init__(self, timeout: int = 30):
        self.timeout = timeout

    def is_available(self) -> bool:
        return True  # Only needs httpx + beautifulsoup4

    async def run(
        self,
        office: str,
        role: str = "Special Agent in Charge",
        max_pages: int = 3,
    ) -> Finding:
        """Search for leadership of a specific ICE/HSI field office.

        Args:
            office: Office name (e.g., "HSI Washington" or "ERO Miami").
            role: Role to search for (default: "Special Agent in Charge").
            max_pages: Number of search result pages to check.
        """
        entities: list[Entity] = []
        relationships: list[Relationship] = []
        attributions: list[dict] = []

        async with httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (research tool)",
            },
        ) as client:
            # Search ice.gov for press releases mentioning this office
            urls = await self._search_releases(client, office, max_pages)
            logger.info("Found %d press release URLs for '%s'", len(urls), office)

            for url in urls[:20]:  # Cap to avoid excessive scraping
                page_attributions = await self._parse_release(client, url, office)
                attributions.extend(page_attributions)

        if not attributions:
            return Finding(
                notes=(
                    f"No leadership attributions found for '{office}' "
                    f"in ICE press releases"
                ),
            )

        # Deduplicate and build entities
        seen_names: dict[str, dict] = {}  # name -> best attribution
        for attr in attributions:
            name = attr["name"]
            if name not in seen_names:
                seen_names[name] = attr
            else:
                # Keep the one with the most recent date
                existing = seen_names[name]
                if attr.get("date", "") > existing.get("date", ""):
                    seen_names[name] = attr

        for name, attr in seen_names.items():
            person_id = f"person:ice_leadership:{name.lower().replace(' ', '_')}"
            entities.append(Entity(
                id=person_id,
                entity_type=EntityType.PERSON,
                label=name,
                properties={
                    "title": attr["title"],
                    "office": attr.get("office_match", office),
                    "agency": "ICE/HSI",
                    "date_observed": attr.get("date", ""),
                    "source_url": attr["url"],
                },
                sources=[Source(
                    tool=self.name,
                    source_url=attr["url"],
                    confidence=0.85,
                )],
            ))

            # Create government office entity for this field office
            office_id = f"government_office:ice:{office.lower().replace(' ', '_')}"
            if not any(e.id == office_id for e in entities):
                entities.append(Entity(
                    id=office_id,
                    entity_type=EntityType.GOVERNMENT_OFFICE,
                    label=office,
                    properties={"agency": "ICE/HSI", "type": "field_office"},
                    sources=[Source(tool=self.name)],
                ))

            relationships.append(Relationship(
                source_id=person_id,
                target_id=office_id,
                relation_type=RelationType.WORKS_AT,
                properties={
                    "role": attr["title"],
                    "date_observed": attr.get("date", ""),
                },
                sources=[Source(
                    tool=self.name,
                    source_url=attr["url"],
                )],
            ))

        # Build summary
        summary_lines = [
            f"ICE leadership search for '{office}':",
        ]
        for name, attr in sorted(
            seen_names.items(),
            key=lambda x: x[1].get("date", ""),
            reverse=True,
        ):
            summary_lines.append(
                f"  {name} — {attr['title']} "
                f"(observed {attr.get('date', 'undated')})"
            )

        return Finding(
            entities=entities,
            relationships=relationships,
            notes="\n".join(summary_lines),
        )

    async def _search_releases(
        self,
        client: httpx.AsyncClient,
        office: str,
        max_pages: int,
    ) -> list[str]:
        """Find press release URLs mentioning the target office.

        The ice.gov site is JS-rendered, so we use DuckDuckGo to find
        press releases indexed by search engines.
        """
        from ddgs import DDGS

        urls: list[str] = []
        queries = [
            f'"{office}" "acting special agent in charge" 2026',
            f'"{office}" "special agent in charge" said 2026',
            f'"{office}" "special agent in charge" said 2025',
        ]

        for query in queries[:max_pages]:
            try:
                with DDGS() as ddgs:
                    results = list(ddgs.text(query, max_results=10))
                for result in results:
                    url = result.get("href", "")
                    if url and url not in urls:
                        urls.append(url)
            except Exception as exc:
                logger.warning("DuckDuckGo search failed: %s", exc)

        return urls

    async def _parse_release(
        self,
        client: httpx.AsyncClient,
        url: str,
        office: str,
    ) -> list[dict]:
        """Parse a press release page for leadership attributions."""
        try:
            resp = await client.get(url)
            if resp.status_code != 200:
                return []
        except httpx.HTTPError:
            return []

        soup = BeautifulSoup(resp.text, "lxml")

        # Get the main article text
        article = soup.find("article") or soup.find("div", class_="field--type-text-long")
        if not article:
            # Fall back to body text
            text = soup.get_text(separator=" ")
        else:
            text = article.get_text(separator=" ")

        # Clean up whitespace
        text = re.sub(r"\s+", " ", text)

        # Try to find a date in the page
        page_date = ""
        date_match = _DATE_PATTERN.search(text)
        if date_match:
            try:
                parsed = datetime.strptime(
                    date_match.group().replace(",", ""),
                    "%B %d %Y",
                )
                page_date = parsed.strftime("%Y-%m-%d")
            except ValueError:
                pass

        # Also check meta tags for date
        if not page_date:
            meta_date = soup.find("meta", {"property": "article:published_time"})
            if meta_date and meta_date.get("content"):
                page_date = meta_date["content"][:10]

        attributions: list[dict] = []

        for pattern in _ATTRIBUTION_PATTERNS:
            for match in pattern.finditer(text):
                groups = match.groups()
                if len(groups) >= 3:
                    # Determine which group is name vs office based on pattern
                    # Pattern 1: (Name, Title, Office)
                    # Pattern 2: (Office, Title, Name)
                    agency_prefixes = ("HSI", "ICE", "ERO")
                    if groups[0][0].isupper() and not groups[0].upper().startswith(agency_prefixes):
                        name, title, office_match = groups[0], groups[1], groups[2]
                    else:
                        office_match, title, name = groups[0], groups[1], groups[2]

                    # Filter: only include if it's related to our target office
                    office_lower = office.lower()
                    combined = f"{office_match} {title}".lower()
                    if (
                        office_lower in combined
                        or office_lower.replace("hsi ", "") in combined
                        or office_lower in url.lower()
                    ):
                        attributions.append({
                            "name": name.strip(),
                            "title": title.strip(),
                            "office_match": office_match.strip(),
                            "date": page_date,
                            "url": url,
                        })

        return attributions
