"""People search adapter — generates search URLs and attempts scraping across
multiple free people search aggregators.

People search sites (TruePeopleSearch, FastPeopleSearch, That's Them, etc.)
aggregate public records: addresses, phone numbers, relatives, associates,
age/DOB. This is high-value data for PI/journalist work, especially on
thin-footprint targets who lack institutional records.

Strategy (following SpiderFoot patterns):
1. Prefer structured data (JSON-LD, schema.org) over HTML scraping
2. Spokeo embeds schema.org/Person JSON-LD — primary extraction path
3. For Cloudflare-blocked sites, return URLs for manual lookup
4. Use curl_cffi for TLS fingerprint impersonation (Chrome)
"""

import asyncio
import json
import random
import re
from dataclasses import dataclass, field
from urllib.parse import quote, quote_plus

from osint_agent.models import (
    Entity,
    EntityType,
    Finding,
    Relationship,
    RelationType,
    Source,
)
from osint_agent.tools.base import ToolAdapter


# US state name → abbreviation mapping for URL construction
_STATE_ABBREVS = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT",
    "delaware": "DE", "florida": "FL", "georgia": "GA", "hawaii": "HI",
    "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME",
    "maryland": "MD", "massachusetts": "MA", "michigan": "MI",
    "minnesota": "MN", "mississippi": "MS", "missouri": "MO",
    "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM",
    "new york": "NY", "north carolina": "NC", "north dakota": "ND",
    "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD",
    "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC",
}

# Reverse: abbreviation → full name
_ABBREV_TO_STATE = {v: k for k, v in _STATE_ABBREVS.items()}


# Chrome impersonation targets for curl_cffi rotation
_IMPERSONATE_TARGETS = [
    "chrome131",
    "chrome127",
    "chrome124",
    "chrome120",
    "chrome116",
]


@dataclass
class _SiteResult:
    """Result from attempting to scrape a single people search site."""

    site_name: str
    url: str
    status: str  # "scraped", "blocked", "error"
    records: list[dict] | None = None  # Parsed person records if scraped
    error: str | None = None


def _normalize_state(state: str) -> tuple[str, str]:
    """Return (full_name, abbreviation) for a state input.

    Accepts either full name or abbreviation.
    Returns ("", "") if unrecognized.
    """
    state = state.strip()
    upper = state.upper()
    lower = state.lower()

    if upper in _ABBREV_TO_STATE:
        return (_ABBREV_TO_STATE[upper], upper)
    if lower in _STATE_ABBREVS:
        return (lower, _STATE_ABBREVS[lower])
    return ("", "")


def _build_search_urls(
    first: str,
    last: str,
    state_full: str,
    state_abbrev: str,
    city: str,
) -> list[tuple[str, str]]:
    """Build search URLs for all supported people search sites.

    Returns list of (site_name, url) tuples.
    """
    first_lower = first.lower()
    last_lower = last.lower()
    first_cap = first.capitalize()
    last_cap = last.capitalize()
    state_title = state_full.title() if state_full else ""

    # Location string for query-param sites
    location = ""
    if city and state_abbrev:
        location = f"{city}, {state_abbrev}"
    elif state_full:
        location = state_title

    urls = []

    # TruePeopleSearch
    if state_abbrev:
        urls.append((
            "TruePeopleSearch",
            f"https://www.truepeoplesearch.com/results"
            f"?name={quote_plus(f'{first_cap} {last_cap}')}"
            f"&citystatezip={quote_plus(location or state_title)}",
        ))
    else:
        urls.append((
            "TruePeopleSearch",
            f"https://www.truepeoplesearch.com/results"
            f"?name={quote_plus(f'{first_cap} {last_cap}')}",
        ))

    # FastPeopleSearch: /name/first-last_state
    if state_full:
        urls.append((
            "FastPeopleSearch",
            f"https://www.fastpeoplesearch.com/name"
            f"/{first_lower}-{last_lower}_{state_full.replace(' ', '-')}",
        ))
    else:
        urls.append((
            "FastPeopleSearch",
            f"https://www.fastpeoplesearch.com/name"
            f"/{first_lower}-{last_lower}",
        ))

    # That's Them: /name/First-Last/State
    if state_title:
        urls.append((
            "ThatsThem",
            f"https://thatsthem.com/name"
            f"/{first_cap}-{last_cap}/{state_title.replace(' ', '-')}",
        ))
    else:
        urls.append((
            "ThatsThem",
            f"https://thatsthem.com/name/{first_cap}-{last_cap}",
        ))

    # Spokeo
    if state_abbrev:
        urls.append((
            "Spokeo",
            f"https://www.spokeo.com/{first_cap}-{last_cap}"
            f"/{state_abbrev}",
        ))
    else:
        urls.append((
            "Spokeo",
            f"https://www.spokeo.com/{first_cap}-{last_cap}",
        ))

    # CyberBackgroundChecks: /people/first/last/state_abbrev
    if state_abbrev:
        urls.append((
            "CyberBackgroundChecks",
            f"https://www.cyberbackgroundchecks.com/people"
            f"/{first_lower}/{last_lower}/{state_abbrev.lower()}",
        ))
    else:
        urls.append((
            "CyberBackgroundChecks",
            f"https://www.cyberbackgroundchecks.com/people"
            f"/{first_lower}/{last_lower}",
        ))

    # Radaris
    urls.append((
        "Radaris",
        f"https://radaris.com/p/{first_cap}/{last_cap}/",
    ))

    return urls


def _parse_name(name: str) -> tuple[str, str]:
    """Split a full name into (first, last).

    Handles "First Last", "Last, First", and multi-word names.
    """
    name = name.strip()
    if "," in name:
        parts = [p.strip() for p in name.split(",", 1)]
        return (parts[1], parts[0])  # Last, First → (First, Last)

    parts = name.split()
    if len(parts) == 1:
        return (parts[0], "")
    if len(parts) == 2:
        return (parts[0], parts[1])
    # 3+ parts: first = first token, last = last token
    return (parts[0], parts[-1])


class PeopleSearchAdapter(ToolAdapter):
    """Generates people search URLs and attempts scraping.

    Supports searching by name with optional state/city filtering.
    Returns search URLs for 6+ people search aggregators as sources
    on a PERSON entity, attempting to scrape each one.
    """

    name = "peoplesearch"

    def __init__(self, timeout: int = 15):
        self.timeout = timeout

    def is_available(self) -> bool:
        try:
            from curl_cffi import requests  # noqa: F401
            return True
        except ImportError:
            return False

    async def run(
        self,
        query: str,
        state: str = "",
        city: str = "",
    ) -> Finding:
        """Search people search aggregators for a person.

        Args:
            query: Person name (e.g. "Thomas Jacob" or "Jacob, Thomas").
            state: State name or abbreviation (e.g. "Virginia" or "VA").
            city: Optional city for narrower results.

        Returns:
            Finding with a PERSON entity containing search URLs as sources,
            plus any scraped data as additional entities.
        """
        first, last = _parse_name(query)
        if not first or not last:
            return Finding(
                notes=f"People search requires first and last name, got: '{query}'",
            )

        state_full, state_abbrev = _normalize_state(state) if state else ("", "")

        urls = _build_search_urls(first, last, state_full, state_abbrev, city)

        # Attempt scraping each site
        results = await self._scrape_sites(urls)

        return self._build_finding(first, last, state_full, state_abbrev, results)

    async def _scrape_sites(
        self,
        urls: list[tuple[str, str]],
    ) -> list[_SiteResult]:
        """Attempt to scrape each people search URL.

        Uses curl_cffi with Chrome TLS impersonation. Falls back to
        returning the URL if blocked.
        """
        from curl_cffi import requests

        results = []
        for i, (site_name, url) in enumerate(urls):
            # Throttle requests per theHarvester/SpiderFoot patterns
            if i > 0:
                await asyncio.sleep(random.uniform(0.5, 2.0))
            try:
                resp = requests.get(
                    url,
                    impersonate=random.choice(_IMPERSONATE_TARGETS),
                    timeout=self.timeout,
                )
                if resp.status_code == 200:
                    # Check for Cloudflare challenge indicators
                    if _is_challenge_page(resp.text):
                        results.append(_SiteResult(
                            site_name=site_name,
                            url=url,
                            status="blocked",
                            error="Cloudflare challenge/CAPTCHA",
                        ))
                    else:
                        records = _try_parse(site_name, resp.text)
                        results.append(_SiteResult(
                            site_name=site_name,
                            url=url,
                            status="scraped" if records else "no_results",
                            records=records,
                        ))
                elif resp.status_code == 403:
                    results.append(_SiteResult(
                        site_name=site_name,
                        url=url,
                        status="blocked",
                        error=f"HTTP {resp.status_code}",
                    ))
                else:
                    results.append(_SiteResult(
                        site_name=site_name,
                        url=url,
                        status="error",
                        error=f"HTTP {resp.status_code}",
                    ))
            except Exception as e:
                results.append(_SiteResult(
                    site_name=site_name,
                    url=url,
                    status="error",
                    error=str(e),
                ))

        return results

    def _build_finding(
        self,
        first: str,
        last: str,
        state_full: str,
        state_abbrev: str,
        results: list[_SiteResult],
    ) -> Finding:
        """Build a Finding from scrape results."""
        entities = []
        relationships = []

        # Create person entity with all search URLs as sources
        person_id = f"person:peoplesearch:{first.lower()}_{last.lower()}"
        props = {
            "first_name": first,
            "last_name": last,
            "source_system": "peoplesearch",
        }
        if state_full:
            props["state"] = state_abbrev
            props["state_full"] = state_full

        sources = []
        scraped_sites = []
        blocked_sites = []

        for result in results:
            source = Source(
                tool=self.name,
                source_url=result.url,
                confidence=1.0 if result.status == "scraped" else 0.5,
                raw_data={
                    "site": result.site_name,
                    "status": result.status,
                    "error": result.error,
                },
            )
            sources.append(source)

            if result.status == "scraped" and result.records:
                scraped_sites.append(result.site_name)
                # Add scraped data as additional entities
                for record in result.records:
                    self._add_record_entities(
                        record,
                        person_id,
                        result.site_name,
                        result.url,
                        entities,
                        relationships,
                    )
            elif result.status == "blocked":
                blocked_sites.append(result.site_name)

        person = Entity(
            id=person_id,
            entity_type=EntityType.PERSON,
            label=f"{first} {last}",
            properties=props,
            sources=sources,
        )
        entities.insert(0, person)

        # Build notes
        notes_parts = [
            f"People search for '{first} {last}'",
        ]
        if state_full:
            notes_parts[0] += f" in {state_full.title()}"
        notes_parts.append(f"{len(results)} sites queried")
        if scraped_sites:
            notes_parts.append(f"scraped: {', '.join(scraped_sites)}")
        if blocked_sites:
            notes_parts.append(
                f"blocked (check manually): {', '.join(blocked_sites)}"
            )

        return Finding(
            entities=entities,
            relationships=relationships,
            notes=" | ".join(notes_parts),
        )

    def _add_record_entities(
        self,
        record: dict,
        person_id: str,
        site_name: str,
        source_url: str,
        entities: list[Entity],
        relationships: list[Relationship],
    ):
        """Add entities from a scraped person record.

        Handles both simple records (name, address, phone) and rich
        Spokeo JSON-LD records (aliases, multiple addresses, relatives,
        profile URLs).
        """
        source = Source(tool=self.name, source_url=source_url)

        # Create a sub-person entity for this specific result
        # (multiple Thomas Jacobs may appear — each is a distinct record)
        rec_name = record.get("name", "")
        if rec_name:
            rec_id = f"person:peoplesearch:{rec_name.lower().replace(' ', '_')}_{hash(str(record)) % 10**8}"
            props = {
                "source_site": site_name,
                "source_system": "peoplesearch",
            }
            if record.get("profile_url"):
                props["profile_url"] = record["profile_url"]
            if record.get("aliases"):
                props["aliases"] = record["aliases"]
            if record.get("age"):
                props["age"] = record["age"]

            entities.append(Entity(
                id=rec_id,
                entity_type=EntityType.PERSON,
                label=rec_name,
                properties=props,
                sources=[source],
            ))
        else:
            rec_id = person_id

        # Addresses — handle both single and multiple
        all_addresses = record.get("all_addresses", [])
        if not all_addresses and record.get("address"):
            all_addresses = [record["address"]]

        for addr_str in all_addresses:
            addr_id = f"address:peoplesearch:{hash(addr_str) % 10**8}"
            entities.append(Entity(
                id=addr_id,
                entity_type=EntityType.ADDRESS,
                label=addr_str,
                properties={
                    "source_site": site_name,
                    "source_system": "peoplesearch",
                },
                sources=[source],
            ))
            relationships.append(Relationship(
                source_id=rec_id,
                target_id=addr_id,
                relation_type=RelationType.HAS_ADDRESS,
                sources=[source],
            ))

        # Phone
        if record.get("phone"):
            phone_id = f"phone:peoplesearch:{record['phone'].replace('-', '').replace(' ', '')}"
            entities.append(Entity(
                id=phone_id,
                entity_type=EntityType.PHONE,
                label=record["phone"],
                properties={
                    "source_site": site_name,
                    "source_system": "peoplesearch",
                },
                sources=[source],
            ))
            relationships.append(Relationship(
                source_id=rec_id,
                target_id=phone_id,
                relation_type=RelationType.HAS_PHONE,
                sources=[source],
            ))

        # Relatives/associates
        for relative in record.get("relatives", []):
            rel_id = f"person:peoplesearch:{relative.lower().replace(' ', '_')}"
            entities.append(Entity(
                id=rel_id,
                entity_type=EntityType.PERSON,
                label=relative,
                properties={
                    "relationship": "relative_or_associate",
                    "source_site": site_name,
                    "source_system": "peoplesearch",
                },
                sources=[source],
            ))
            relationships.append(Relationship(
                source_id=rec_id,
                target_id=rel_id,
                relation_type=RelationType.CONNECTED_TO,
                properties={"connection_type": "relative_or_associate"},
                sources=[source],
            ))


def _is_challenge_page(html: str) -> bool:
    """Detect Cloudflare challenge pages and CAPTCHAs."""
    lower = html.lower()
    indicators = [
        "just a moment",
        "captcha challenge",
        "security challenge",
        "cf-browser-verification",
        "checking your browser",
        "ray id",
        "_cf_chl_opt",
    ]
    return any(indicator in lower for indicator in indicators)


def _try_parse(site_name: str, html: str) -> list[dict] | None:
    """Attempt to parse person records from a people search site's HTML.

    Strategy: prefer structured data (JSON-LD) over HTML scraping.
    Spokeo embeds schema.org/Person JSON-LD — this is the primary path.
    HTML parsers are fallbacks for sites without structured data.

    Returns a list of dicts with keys: name, address, phone, age, relatives,
    aliases, profile_url. Returns None if parsing fails or no results found.
    """
    parsers = {
        "Spokeo": _parse_spokeo_jsonld,
        "TruePeopleSearch": _parse_html_generic,
        "FastPeopleSearch": _parse_html_generic,
        "ThatsThem": _parse_html_generic,
    }
    parser = parsers.get(site_name)
    if not parser:
        return None
    try:
        return parser(html)
    except Exception:
        return None


def _parse_spokeo_jsonld(html: str) -> list[dict] | None:
    """Parse Spokeo results using embedded schema.org JSON-LD.

    Spokeo embeds Person objects with homeLocation, relatedTo,
    additionalName as JSON-LD. This is deterministic and robust —
    schema.org markup is stable across layout changes because it
    serves SEO.
    """
    records = []

    # Find all JSON-LD script blocks
    ld_blocks = re.findall(
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
        html,
        re.DOTALL,
    )

    for block in ld_blocks:
        block = block.strip()
        try:
            data = json.loads(block)
        except (json.JSONDecodeError, ValueError):
            continue

        # Handle both single objects and arrays
        items = data if isinstance(data, list) else [data]

        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("@type") != "Person":
                continue

            record = _spokeo_person_to_record(item)
            if record:
                records.append(record)

    return records if records else None


def _spokeo_person_to_record(person: dict) -> dict | None:
    """Convert a schema.org Person JSON-LD object to our record format."""
    record = {}

    name = person.get("name", "")
    if not name:
        return None
    record["name"] = name

    # Profile URL
    if person.get("url"):
        record["profile_url"] = person["url"]

    # Aliases / additional names
    aliases = person.get("additionalName", [])
    if aliases:
        record["aliases"] = aliases if isinstance(aliases, list) else [aliases]

    # Addresses from homeLocation
    addresses = []
    for loc in person.get("homeLocation", []):
        addr = loc.get("address", {})
        if not addr:
            continue
        parts = [
            addr.get("streetAddress", ""),
            addr.get("addressLocality", ""),
            addr.get("addressRegion", ""),
            addr.get("postalCode", ""),
        ]
        addr_str = ", ".join(p for p in parts if p)
        if addr_str:
            addresses.append(addr_str)

    if addresses:
        record["address"] = addresses[0]  # Primary
        record["all_addresses"] = addresses

    # Relatives from relatedTo
    relatives = []
    for rel in person.get("relatedTo", []):
        rel_name = rel.get("name", "")
        if rel_name:
            relatives.append(rel_name)
    if relatives:
        record["relatives"] = relatives

    return record


def _parse_html_generic(html: str) -> list[dict] | None:
    """Generic HTML parser for people search results.

    Looks for common patterns: name in headings/links, addresses
    with street patterns, phone numbers, age indicators.
    This is a best-effort fallback — structured data is preferred.
    """
    records = []

    # Look for card/record containers
    card_patterns = [
        re.compile(r'<div[^>]*class="[^"]*(?:card-summary|people-record|card-block)[^"]*"[^>]*>(.*?)</div>\s*</div>', re.DOTALL),
        re.compile(r'<article[^>]*>(.*?)</article>', re.DOTALL),
    ]

    cards = []
    for pattern in card_patterns:
        cards = pattern.findall(html)
        if cards:
            break

    for card in cards[:10]:
        record = {}

        # Name: look in headings or prominent links
        name_match = re.search(
            r'<(?:h[1-4]|a)[^>]*>([A-Z][a-z]+ (?:[A-Z]\. )?[A-Z][a-z]+)</(?:h[1-4]|a)>',
            card,
        )
        if name_match:
            record["name"] = name_match.group(1).strip()

        # Address: street pattern
        addr_match = re.search(
            r'(\d+\s+[A-Z][^<,]{5,50}(?:St|Ave|Dr|Rd|Blvd|Ln|Ct|Way|Cir|Pl|Ter)[^<]{0,50})',
            card,
            re.I,
        )
        if addr_match:
            record["address"] = addr_match.group(1).strip()

        # Phone
        phone_match = re.search(r'\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}', card)
        if phone_match:
            record["phone"] = phone_match.group(0)

        # Age
        age_match = re.search(r'(?:Age|age)\s*:?\s*(\d{1,3})', card)
        if age_match:
            record["age"] = age_match.group(1)

        if record.get("name") or record.get("address"):
            records.append(record)

    return records if records else None
