"""DuckDuckGo web search tool adapter — general-purpose web and news search."""

import hashlib

from osint_agent.models import (
    Entity,
    EntityType,
    Finding,
    Source,
)
from osint_agent.tools.base import ToolAdapter


def _url_hash(url: str) -> str:
    """Return first 12 hex chars of the MD5 hash of a URL."""
    return hashlib.md5(url.encode()).hexdigest()[:12]


class DdgSearchAdapter(ToolAdapter):
    """Searches the web via DuckDuckGo using the ddgs package.

    Provides:
    - General web search (text mode)
    - News search (news mode)

    Useful as a broad-spectrum complement to specialized OSINT tools.
    """

    name = "ddg_search"

    def is_available(self) -> bool:
        try:
            from ddgs import DDGS  # noqa: F401
            return True
        except ImportError:
            return False

    async def run(
        self,
        query: str,
        mode: str = "text",
        max_results: int = 20,
    ) -> Finding:
        """Search DuckDuckGo.

        Args:
            query: Search query string.
            mode: "text" for web search, "news" for news search.
            max_results: Maximum number of results to return.
        """
        from ddgs import DDGS

        ddgs = DDGS()

        if mode == "news":
            return self._parse_news(query, ddgs.news(query, max_results=max_results))
        return self._parse_text(query, ddgs.text(query, max_results=max_results))

    def _parse_text(self, query: str, results: list[dict]) -> Finding:
        """Parse web search results into entities."""
        entities = []

        for item in results:
            url = item.get("href", "")
            title = item.get("title", "Untitled")
            body = item.get("body", "")

            entities.append(Entity(
                id=f"document:ddg:{_url_hash(url)}",
                entity_type=EntityType.DOCUMENT,
                label=title,
                properties={
                    "url": url,
                    "snippet": body[:500],
                    "search_query": query,
                    "result_type": "web",
                },
                sources=[Source(tool=self.name, source_url=url)],
            ))

        return Finding(
            entities=entities,
            notes=f"DuckDuckGo text search for '{query}': {len(results)} results",
        )

    def _parse_news(self, query: str, results: list[dict]) -> Finding:
        """Parse news search results into entities."""
        entities = []

        for item in results:
            url = item.get("url", "")
            title = item.get("title", "Untitled")
            body = item.get("body", "")
            date = item.get("date", "")
            source = item.get("source", "")

            entities.append(Entity(
                id=f"document:ddg:{_url_hash(url)}",
                entity_type=EntityType.DOCUMENT,
                label=title,
                properties={
                    "url": url,
                    "snippet": body[:500],
                    "search_query": query,
                    "result_type": "news",
                    "date": date,
                    "source": source,
                },
                sources=[Source(tool=self.name, source_url=url)],
            ))

        return Finding(
            entities=entities,
            notes=f"DuckDuckGo news search for '{query}': {len(results)} results",
        )
