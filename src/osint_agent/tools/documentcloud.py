"""DocumentCloud adapter — FOIA and public document search.

Searches DocumentCloud.org's corpus of 10M+ documents uploaded by
journalists and researchers — FOIA responses, court filings, leaked
memos, government reports, and investigative source material.

Uses the python-documentcloud library (MuckRock).
Free account optional (public search works without auth, limited to 25/page).
Authenticated users get 100 results/page.
"""

import logging
import os

from osint_agent.models import (
    Entity,
    EntityType,
    Finding,
    Source,
)
from osint_agent.tools.base import ToolAdapter

logger = logging.getLogger(__name__)

_SOURCE = lambda url=None: Source(tool="documentcloud", source_url=url)


class DocumentCloudAdapter(ToolAdapter):
    """Search DocumentCloud for FOIA documents, court filings, and public records."""

    name = "documentcloud"

    def is_available(self) -> bool:
        try:
            import documentcloud  # noqa: F401
            return True
        except ImportError:
            return False

    async def run(
        self,
        query: str,
        per_page: int = 25,
        **kwargs,
    ) -> Finding:
        """Search DocumentCloud for documents matching a query.

        Args:
            query: Search terms (supports DocumentCloud query syntax).
            per_page: Results per page (25 without auth, up to 100 with auth).
        """
        import asyncio
        import documentcloud

        username = os.environ.get("DOCUMENTCLOUD_USERNAME", "")
        password = os.environ.get("DOCUMENTCLOUD_PASSWORD", "")

        def _search():
            if username and password:
                client = documentcloud.DocumentCloud(username, password)
            else:
                # Public search — no auth, limited results.
                client = documentcloud.DocumentCloud()
            return client.documents.search(query, per_page=per_page)

        loop = asyncio.get_event_loop()
        try:
            results = await loop.run_in_executor(None, _search)
        except ValueError:
            # "No tokens found" — expected when no credentials provided.
            # Fall back to direct API search via httpx.
            return await self._search_public_api(query, per_page)
        except Exception as exc:
            logger.warning("DocumentCloud search failed: %s", exc)
            return await self._search_public_api(query, per_page)

        if not results:
            return Finding(notes=f"DocumentCloud: no documents found for '{query}'")

        entities: list[Entity] = []
        for doc in results[:per_page]:
            ent = self._build_doc_entity(doc)
            entities.append(ent)

        return Finding(
            entities=entities,
            notes=(
                f"DocumentCloud: {len(entities)} document(s) for '{query}'"
            ),
        )

    async def _search_public_api(
        self,
        query: str,
        per_page: int = 25,
    ) -> Finding:
        """Fallback: search DocumentCloud's public API directly via httpx."""
        import httpx

        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(
                    "https://api.www.documentcloud.org/api/documents/search/",
                    params={"q": query, "per_page": min(per_page, 25)},
                )
                resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("DocumentCloud public API failed: %s", exc)
            return Finding(notes=f"DocumentCloud error: {exc}")

        data = resp.json()
        results = data.get("results", [])

        if not results:
            return Finding(notes=f"DocumentCloud: no documents found for '{query}'")

        entities: list[Entity] = []
        for doc in results:
            ent = self._build_doc_entity_from_api(doc)
            entities.append(ent)

        total = data.get("count", len(results))
        return Finding(
            entities=entities,
            notes=(
                f"DocumentCloud: {len(entities)} of {total} document(s) for '{query}'"
            ),
        )

    def _build_doc_entity(self, doc) -> Entity:
        """Build entity from python-documentcloud Document object."""
        props = {}
        for attr in ("source", "description", "created_at", "updated_at",
                      "page_count", "language", "organization"):
            val = getattr(doc, attr, None)
            if val is not None:
                props[attr] = str(val) if not isinstance(val, (str, int, float)) else val

        contributor = getattr(doc, "contributor", None)
        if contributor:
            props["contributor"] = str(contributor)

        canonical_url = getattr(doc, "canonical_url", None) or f"https://www.documentcloud.org/documents/{doc.id}"

        return Entity(
            id=f"document:documentcloud:{doc.id}",
            entity_type=EntityType.DOCUMENT,
            label=doc.title or f"Document {doc.id}",
            properties={
                **props,
                "url": canonical_url,
                "pdf_url": getattr(doc, "pdf_url", None),
                "document_source": "documentcloud",
            },
            sources=[_SOURCE(canonical_url)],
        )

    def _build_doc_entity_from_api(self, doc: dict) -> Entity:
        """Build entity from raw API JSON response."""
        doc_id = doc.get("id", "")
        title = doc.get("title", f"Document {doc_id}")
        canonical_url = doc.get("canonical_url", f"https://www.documentcloud.org/documents/{doc_id}")

        props = {}
        for key in ("source", "description", "created_at", "updated_at",
                     "page_count", "language"):
            val = doc.get(key)
            if val is not None and val != "":
                props[key] = val

        org = doc.get("organization")
        if isinstance(org, dict):
            props["organization"] = org.get("name", str(org.get("id", "")))
        elif org is not None:
            props["organization"] = str(org)

        contributor = doc.get("user")
        if isinstance(contributor, dict):
            props["contributor"] = contributor.get("name", "")
        elif contributor is not None:
            props["contributor"] = str(contributor)

        return Entity(
            id=f"document:documentcloud:{doc_id}",
            entity_type=EntityType.DOCUMENT,
            label=title,
            properties={
                **props,
                "url": canonical_url,
                "pdf_url": doc.get("pdf_url"),
                "document_source": "documentcloud",
            },
            sources=[_SOURCE(canonical_url)],
        )
