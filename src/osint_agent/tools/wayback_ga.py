"""Wayback Google Analytics adapter — hidden site network discovery.

Scrapes historical Google Analytics / GTM / AdSense tracking IDs from
Wayback Machine snapshots. Sites sharing the same tracking ID are likely
operated by the same entity — revealing astroturf networks, shell sites,
and coordinated media operations.

Uses the bellingcat/wayback-google-analytics library.
No API key or authentication required.
"""

import asyncio
import logging

import aiohttp

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

_SOURCE = lambda url=None: Source(tool="wayback_ga", source_url=url)


class WaybackGaAdapter(ToolAdapter):
    """Discover Google Analytics / GTM tracking IDs from Wayback Machine snapshots."""

    name = "wayback_ga"

    def __init__(self, timeout: int = 120):
        self.timeout = timeout

    def is_available(self) -> bool:
        try:
            import wayback_google_analytics  # noqa: F401
            return True
        except ImportError:
            return False

    async def run(
        self,
        url: str,
        start_date: str = "20100101000000",
        end_date: str | None = None,
        limit: int | None = 500,
        **kwargs,
    ) -> Finding:
        """Scrape analytics tracking IDs from a URL's Wayback Machine history.

        Args:
            url: Domain or URL to analyze (e.g., "example.com" or "https://example.com").
            start_date: Wayback timestamp start (default: 2010-01-01).
            end_date: Wayback timestamp end (default: now).
            limit: Max snapshots to check per URL (default: 500).
        """
        from wayback_google_analytics.scraper import get_analytics_codes

        # Normalize URL — the library expects bare domains or full URLs.
        if not url.startswith("http"):
            url = f"https://{url}"

        domain = _extract_domain(url)
        entities: list[Entity] = []
        relationships: list[Relationship] = []

        domain_ent = Entity(
            id=f"domain:{domain}",
            entity_type=EntityType.DOMAIN,
            label=domain,
            properties={"url": url},
            sources=[_SOURCE(url)],
        )
        entities.append(domain_ent)

        try:
            sem = asyncio.Semaphore(5)  # Rate-limit Wayback requests.
            async with aiohttp.ClientSession() as session:
                results = await asyncio.wait_for(
                    get_analytics_codes(
                        session,
                        [url],
                        start_date=start_date,
                        end_date=end_date,
                        limit=limit,
                        semaphore=sem,
                    ),
                    timeout=self.timeout,
                )
        except asyncio.TimeoutError:
            return Finding(
                entities=entities,
                notes=f"Wayback GA: timed out after {self.timeout}s for {domain}",
            )
        except Exception as exc:
            logger.warning("Wayback GA scrape failed for %s: %s", domain, exc)
            return Finding(
                entities=entities,
                notes=f"Wayback GA error: {exc}",
            )

        if not results:
            return Finding(
                entities=entities,
                notes=f"Wayback GA: no analytics codes found for {domain}",
            )

        # results is a list of dicts: [{url: {codes...}}, ...]
        # Merge into a single dict.
        merged: dict[str, dict] = {}
        for item in results:
            if isinstance(item, dict):
                merged.update(item)

        ua_codes: dict[str, dict] = {}  # code -> {first_seen, last_seen}
        ga_codes: dict[str, dict] = {}
        gtm_codes: dict[str, dict] = {}

        for site_url, site_data in merged.items():
            if not isinstance(site_data, dict):
                continue

            # Current codes.
            for key, store in [
                ("current_UA_code", ua_codes),
                ("current_GA_code", ga_codes),
                ("current_GTM_code", gtm_codes),
            ]:
                code = site_data.get(key)
                if code:
                    store.setdefault(code, {"first_seen": None, "last_seen": None})

            # Archived codes with timestamps.
            for key, store in [
                ("archived_UA_codes", ua_codes),
                ("archived_GA_codes", ga_codes),
                ("archived_GTM_codes", gtm_codes),
            ]:
                archived = site_data.get(key, {})
                if not isinstance(archived, dict):
                    continue
                for code, meta in archived.items():
                    if not code:
                        continue
                    existing = store.get(code, {"first_seen": None, "last_seen": None})
                    if isinstance(meta, dict):
                        fs = meta.get("first_seen")
                        ls = meta.get("last_seen")
                        if fs and (not existing["first_seen"] or fs < existing["first_seen"]):
                            existing["first_seen"] = fs
                        if ls and (not existing["last_seen"] or ls > existing["last_seen"]):
                            existing["last_seen"] = ls
                    store[code] = existing

        all_codes = {
            "UA": ua_codes,
            "GA": ga_codes,
            "GTM": gtm_codes,
        }

        total_codes = sum(len(v) for v in all_codes.values())
        if total_codes == 0:
            return Finding(
                entities=entities,
                notes=f"Wayback GA: no tracking codes found for {domain}",
            )

        # Create an entity for each unique tracking code.
        for code_type, code_dict in all_codes.items():
            for code, meta in sorted(code_dict.items()):
                code_ent = Entity(
                    id=f"document:analytics:{code}",
                    entity_type=EntityType.DOCUMENT,
                    label=f"{code_type}: {code}",
                    properties={
                        "tracking_code": code,
                        "code_type": code_type,
                        "first_seen": meta.get("first_seen"),
                        "last_seen": meta.get("last_seen"),
                    },
                    sources=[_SOURCE(f"https://web.archive.org/web/*/{url}")],
                )
                entities.append(code_ent)

                relationships.append(Relationship(
                    source_id=domain_ent.id,
                    target_id=code_ent.id,
                    relation_type=RelationType.CONNECTED_TO,
                    properties={"relationship": "uses_tracking_code"},
                    sources=[_SOURCE()],
                ))

        # Summary properties on the domain entity.
        domain_ent.properties["analytics_codes"] = {
            ct: sorted(cd.keys()) for ct, cd in all_codes.items() if cd
        }

        code_summary = []
        for ct, cd in all_codes.items():
            if cd:
                code_summary.append(f"{len(cd)} {ct}")

        return Finding(
            entities=entities,
            relationships=relationships,
            notes=(
                f"Wayback GA: {', '.join(code_summary)} tracking code(s) "
                f"found for {domain}"
            ),
        )


def _extract_domain(url: str) -> str:
    """Extract domain from a URL."""
    from urllib.parse import urlparse
    parsed = urlparse(url if "://" in url else f"https://{url}")
    return parsed.netloc or parsed.path.split("/")[0]
