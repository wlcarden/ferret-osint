"""Common Crawl tool adapter — web archive index search."""

import hashlib
import re

import httpx

from osint_agent.models import (
    Entity,
    EntityType,
    Finding,
    Source,
)
from osint_agent.tools.base import ToolAdapter

# Common Crawl CDX index API (latest index by default)
CDX_BASE = "https://index.commoncrawl.org"

# Max pages we'll fetch to avoid overloading
MAX_PAGES = 3


class CommonCrawlAdapter(ToolAdapter):
    """Searches the Common Crawl CDX index for archived URLs.

    Common Crawl stores petabytes of web data from broad crawls.
    The CDX index lets us find what URLs were captured matching a
    domain or URL pattern — useful as a complement to Wayback Machine.

    Provides:
    - Domain search: find all captured URLs for a domain
    - URL search: find captures of a specific URL
    """

    name = "commoncrawl"

    def __init__(self, timeout: int = 30):
        self.timeout = timeout

    def is_available(self) -> bool:
        return True

    async def run(
        self,
        query: str,
        max_results: int = 50,
    ) -> Finding:
        """Search Common Crawl CDX index.

        Args:
            query: Domain (e.g. "example.com") or full URL pattern.
                   Domain queries automatically get "*.domain/*" matching.
            max_results: Maximum results to return.
        """
        # If it looks like a bare domain, use wildcard matching
        url_pattern = query
        if not query.startswith("http") and "/" not in query:
            url_pattern = f"*.{query}/*"

        return await self._search_index(url_pattern, query, max_results)

    async def _search_index(
        self, url_pattern: str, original_query: str, max_results: int,
    ) -> Finding:
        """Query the Common Crawl CDX index."""
        # First, get the latest index info
        index_info = await self._get_latest_index()
        if not index_info:
            return Finding(
                entities=[],
                relationships=[],
                notes="Common Crawl: could not determine latest index.",
            )

        index_name = index_info["id"]
        cdx_url = index_info.get("cdx-api", f"{CDX_BASE}/{index_name}-index")

        params = {
            "url": url_pattern,
            "output": "json",
            "limit": min(max_results, 200),
        }

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(cdx_url, params=params)
            if resp.status_code == 404:
                return Finding(
                    entities=[],
                    relationships=[],
                    notes=(
                        f"Common Crawl: no captures found for "
                        f"'{original_query}' in index {index_name}."
                    ),
                )
            if resp.status_code in (403, 429, 503):
                return Finding(
                    entities=[],
                    relationships=[],
                    notes=(
                        f"Common Crawl CDX returned {resp.status_code} "
                        f"for '{original_query}' — service may be "
                        f"temporarily unavailable."
                    ),
                )
            resp.raise_for_status()

        # Response is NDJSON (one JSON object per line)
        lines = resp.text.strip().split("\n")
        results = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                import json
                results.append(json.loads(line))
            except (ValueError, KeyError):
                continue

        entities: list[Entity] = []
        seen_urls: set[str] = set()

        for record in results[:max_results]:
            url = record.get("url", "")
            timestamp = record.get("timestamp", "")
            status = record.get("status", "")
            mime = record.get("mime", "")
            digest = record.get("digest", "")
            length = record.get("length", "")

            if not url or url in seen_urls:
                continue
            seen_urls.add(url)

            url_hash = hashlib.md5(
                f"{url}:{timestamp}".encode(),
            ).hexdigest()[:12]
            doc_id = f"document:commoncrawl:{url_hash}"

            # Format timestamp: 20231215143022 -> 2023-12-15T14:30:22
            formatted_ts = _format_timestamp(timestamp)

            entities.append(Entity(
                id=doc_id,
                entity_type=EntityType.DOCUMENT,
                label=f"CC: {url}",
                properties={
                    "url": url,
                    "timestamp": formatted_ts,
                    "raw_timestamp": timestamp,
                    "status_code": status,
                    "mime_type": mime,
                    "content_digest": digest,
                    "content_length": length,
                    "source_system": "commoncrawl",
                    "index": index_name,
                },
                sources=[Source(
                    tool=self.name,
                    source_url=(
                        f"https://index.commoncrawl.org/{index_name}"
                        f"-index?url={url}&output=json"
                    ),
                )],
            ))

        return Finding(
            entities=entities,
            relationships=[],
            notes=(
                f"Common Crawl search for '{original_query}': "
                f"{len(results)} records found, "
                f"{len(entities)} unique URLs "
                f"(index: {index_name})"
            ),
        )

    async def _get_latest_index(self) -> dict | None:
        """Fetch the list of available indexes and return the latest."""
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(f"{CDX_BASE}/collinfo.json")
                resp.raise_for_status()
                collections = resp.json()
        except Exception:
            return None

        if not collections:
            return None

        # Collections are returned newest-first
        # e.g. {"id": "CC-MAIN-2024-10", "cdx-api": "...", ...}
        return collections[0]


def _format_timestamp(ts: str) -> str:
    """Format Common Crawl timestamp '20231215143022' to ISO-ish."""
    if not ts or len(ts) < 14:
        return ts
    return (
        f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}T"
        f"{ts[8:10]}:{ts[10:12]}:{ts[12:14]}"
    )
