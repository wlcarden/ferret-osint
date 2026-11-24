"""CourtListener/RECAP tool adapter — federal court records."""

import logging
import os
from pathlib import Path

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

logger = logging.getLogger(__name__)

BASE_URL = "https://www.courtlistener.com/api/rest/v4"
STORAGE_URL = "https://storage.courtlistener.com"


class CourtListenerAdapter(ToolAdapter):
    """Queries the CourtListener REST API for federal court records.

    Provides:
    - Docket search by party name, case name, or keyword
    - Docket lookup by case number (e.g. "3:26-mc-80074")
    - Document listing for a specific docket (via RECAP)
    - Document download (PDF) from RECAP archive
    - Opinion full-text search
    - Party extraction from docket data
    """

    name = "courtlistener"
    required_env_key = "COURTLISTENER_API_KEY"

    def __init__(self, timeout: int = 30):
        self.timeout = timeout
        self.api_key = os.getenv("COURTLISTENER_API_KEY", "")

    def _headers(self) -> dict:
        return {"Authorization": f"Token {self.api_key}"}

    async def run(self, query: str, mode: str = "dockets", max_results: int = 20) -> Finding:
        """Search CourtListener.

        Args:
            query: Search term (person name, company name, case keyword).
            mode: "dockets" for case search, "opinions" for opinion text search.
            max_results: Maximum results to return.
        """
        if mode == "opinions":
            return await self._search_opinions(query, max_results)
        return await self._search_dockets(query, max_results)

    async def _search_dockets(self, query: str, max_results: int) -> Finding:
        """Search dockets via the v4 search endpoint."""
        entities = []
        relationships = []

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(
                f"{BASE_URL}/search/",
                headers=self._headers(),
                params={
                    "q": query,
                    "type": "r",  # RECAP docket search
                    "page_size": min(max_results, 20),
                },
            )
            resp.raise_for_status()
            data = resp.json()

        results = data.get("results", [])
        for docket in results:
            docket_id = docket.get("docket_id", docket.get("id", ""))
            case_name = docket.get("caseName", docket.get("case_name", "Unknown Case"))
            court = docket.get("court", "")
            date_filed = docket.get("dateFiled", docket.get("date_filed", ""))
            docket_number = docket.get("docketNumber", docket.get("docket_number", ""))
            cause = docket.get("cause", "")

            doc_entity_id = f"document:cl:{docket_id}"
            entities.append(Entity(
                id=doc_entity_id,
                entity_type=EntityType.DOCUMENT,
                label=case_name,
                properties={
                    "court": court,
                    "date_filed": date_filed,
                    "docket_number": docket_number,
                    "cause": cause,
                    "source_system": "courtlistener",
                },
                sources=[Source(
                    tool=self.name,
                    source_url=f"https://www.courtlistener.com/docket/{docket_id}/",
                    raw_data=docket,
                )],
            ))

        return Finding(
            entities=entities,
            relationships=relationships,
            notes=f"CourtListener docket search for '{query}': {len(results)} results",
        )

    async def _search_opinions(self, query: str, max_results: int) -> Finding:
        """Full-text search of court opinions."""
        entities = []

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(
                f"{BASE_URL}/search/",
                headers=self._headers(),
                params={
                    "q": query,
                    "type": "o",  # opinions
                    "order_by": "score desc",
                    "page_size": min(max_results, 20),
                },
            )
            resp.raise_for_status()
            data = resp.json()

        results = data.get("results", [])
        for opinion in results:
            op_id = opinion.get("id", "")
            case_name = opinion.get("caseName", opinion.get("case_name", "Unknown"))
            court = opinion.get("court", "")
            date_filed = opinion.get("dateFiled", opinion.get("date_filed", ""))
            snippet = opinion.get("snippet", "")

            entities.append(Entity(
                id=f"document:cl:opinion:{op_id}",
                entity_type=EntityType.DOCUMENT,
                label=case_name,
                properties={
                    "document_type": "opinion",
                    "court": court,
                    "date_filed": date_filed,
                    "snippet": snippet[:500],
                    "source_system": "courtlistener",
                },
                sources=[Source(
                    tool=self.name,
                    source_url=f"https://www.courtlistener.com/opinion/{op_id}/",
                    raw_data=opinion,
                )],
            ))

        return Finding(
            entities=entities,
            notes=f"CourtListener opinion search for '{query}': {len(results)} results",
        )

    async def search_party(self, name: str, max_results: int = 20) -> Finding:
        """Search for a person/organization as a party in court cases.

        This is the primary investigative use — "has this person been in court?"
        Creates PARTY_TO relationships between the person and case documents.
        """
        entities = []
        relationships = []

        # Search dockets where this name appears as a party
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(
                f"{BASE_URL}/search/",
                headers=self._headers(),
                params={
                    "q": f'"{name}"',
                    "type": "r",
                    "page_size": min(max_results, 20),
                },
            )
            resp.raise_for_status()
            data = resp.json()

        # Create a person/org entity for the search target
        person_id = f"person:cl_search:{name.lower().replace(' ', '_')}"
        person = Entity(
            id=person_id,
            entity_type=EntityType.PERSON,
            label=name,
            sources=[Source(tool=self.name)],
        )
        entities.append(person)

        results = data.get("results", [])
        for docket in results:
            docket_id = docket.get("docket_id", docket.get("id", ""))
            case_name = docket.get("caseName", docket.get("case_name", "Unknown"))
            court = docket.get("court", "")
            date_filed = docket.get("dateFiled", docket.get("date_filed", ""))

            doc_id = f"document:cl:{docket_id}"
            entities.append(Entity(
                id=doc_id,
                entity_type=EntityType.DOCUMENT,
                label=case_name,
                properties={
                    "court": court,
                    "date_filed": date_filed,
                    "docket_number": docket.get("docketNumber", docket.get("docket_number", "")),
                    "source_system": "courtlistener",
                },
                sources=[Source(
                    tool=self.name,
                    source_url=f"https://www.courtlistener.com/docket/{docket_id}/",
                )],
            ))

            relationships.append(Relationship(
                source_id=person_id,
                target_id=doc_id,
                relation_type=RelationType.PARTY_TO,
                properties={"case_name": case_name, "date_filed": date_filed},
                sources=[Source(
                    tool=self.name,
                    source_url=f"https://www.courtlistener.com/docket/{docket_id}/",
                )],
            ))

        return Finding(
            entities=entities,
            relationships=relationships,
            notes=f"CourtListener party search for '{name}': {len(results)} cases found",
        )

    async def lookup_docket(
        self, docket_number: str, court: str | None = None,
    ) -> Finding:
        """Look up a docket by its case number.

        Args:
            docket_number: Case number (e.g. "3:26-mc-80074").
            court: Optional CourtListener court ID (e.g. "cand" for N.D. Cal.).
        """
        entities = []

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            params: dict = {"docket_number": docket_number}
            if court:
                params["court"] = court
            resp = await client.get(
                f"{BASE_URL}/dockets/",
                headers=self._headers(),
                params=params,
            )
            resp.raise_for_status()
            data = resp.json()

        results = data.get("results", [])
        for docket in results:
            docket_id = docket.get("id", "")
            case_name = docket.get("case_name", "Unknown Case")
            docket_court = docket.get("court_id", "")
            date_filed = docket.get("date_filed", "")
            docket_num = docket.get("docket_number", "")
            cause = docket.get("cause", "")
            assigned_to = docket.get("assigned_to_str", "")
            date_terminated = docket.get("date_terminated", "")
            pacer_case_id = docket.get("pacer_case_id", "")
            nature_of_suit = docket.get("nature_of_suit", "")

            entities.append(Entity(
                id=f"document:cl:{docket_id}",
                entity_type=EntityType.DOCUMENT,
                label=case_name,
                properties={
                    "court": docket_court,
                    "date_filed": date_filed,
                    "date_terminated": date_terminated,
                    "docket_number": docket_num,
                    "docket_id": docket_id,
                    "cause": cause,
                    "nature_of_suit": nature_of_suit,
                    "assigned_to": assigned_to,
                    "pacer_case_id": pacer_case_id,
                    "source_system": "courtlistener",
                },
                sources=[Source(
                    tool=self.name,
                    source_url=docket.get("absolute_url", ""),
                    raw_data=docket,
                )],
            ))

        return Finding(
            entities=entities,
            notes=f"CourtListener docket lookup for '{docket_number}': {len(results)} match(es)",
        )

    async def list_documents(self, docket_id: int) -> Finding:
        """List all RECAP documents available for a docket.

        Uses the search endpoint filtered by docket_id, which returns
        recap_documents nested in results. The /docket-entries/ endpoint
        requires a paid membership, but search is free-tier accessible.

        Args:
            docket_id: CourtListener internal docket ID (from lookup_docket).
        """
        entities = []

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(
                f"{BASE_URL}/search/",
                headers=self._headers(),
                params={
                    "q": f"docket_id:{docket_id}",
                    "type": "r",
                },
            )
            resp.raise_for_status()
            data = resp.json()

        results = data.get("results", [])
        if not results:
            return Finding(notes=f"No RECAP results for docket {docket_id}")

        # The search result contains recap_documents as a nested list
        docket_result = results[0]
        case_name = docket_result.get("caseName", "Unknown Case")
        recap_docs = docket_result.get("recap_documents", [])

        for doc in recap_docs:
            doc_id = doc.get("id", "")
            entry_number = doc.get("entry_number")
            attachment_number = doc.get("attachment_number")
            description = doc.get("description", "")
            short_desc = doc.get("short_description", "")
            is_available = doc.get("is_available", False)
            page_count = doc.get("page_count")
            filepath = doc.get("filepath_local")
            date_filed = doc.get("entry_date_filed", "")
            pacer_doc_id = doc.get("pacer_doc_id", "")

            # Build a readable label like "Doc 1 Att 2: Exhibit A, Summons"
            label_parts = []
            if entry_number is not None:
                label_parts.append(f"Doc {entry_number}")
            if attachment_number is not None:
                label_parts.append(f"Att {attachment_number}")
            label_parts.append(short_desc or description[:80] or "untitled")
            label = " — ".join(label_parts)

            download_url = ""
            if is_available and filepath:
                download_url = f"{STORAGE_URL}/{filepath}"

            entities.append(Entity(
                id=f"document:cl:recap:{doc_id}",
                entity_type=EntityType.DOCUMENT,
                label=label,
                properties={
                    "case_name": case_name,
                    "entry_number": entry_number,
                    "attachment_number": attachment_number,
                    "description": description,
                    "short_description": short_desc,
                    "is_available": is_available,
                    "page_count": page_count,
                    "date_filed": date_filed,
                    "download_url": download_url,
                    "pacer_doc_id": pacer_doc_id,
                    "filepath": filepath,
                    "source_system": "courtlistener_recap",
                },
                sources=[Source(
                    tool=self.name,
                    source_url=doc.get("absolute_url", ""),
                    raw_data=doc,
                )],
            ))

        available = sum(1 for e in entities if e.properties.get("is_available"))
        return Finding(
            entities=entities,
            notes=(
                f"RECAP documents for docket {docket_id} ({case_name}): "
                f"{len(entities)} entries, {available} downloadable"
            ),
        )

    async def fetch_document(
        self,
        docket_id: int,
        doc_number: int,
        attachment_number: int | None = None,
        output_dir: str = "data/court_docs",
    ) -> Finding:
        """Download a specific document PDF from the RECAP archive.

        First lists documents for the docket, finds the matching entry,
        then downloads the PDF if available.

        Args:
            docket_id: CourtListener internal docket ID.
            doc_number: The docket entry number (e.g. 1 for Document 1).
            attachment_number: Attachment number within the entry (e.g. 2
                for Exhibit A). None for the main document.
            output_dir: Directory to save the PDF.
        """
        # Get the document listing
        listing = await self.list_documents(docket_id)
        if listing.error:
            return listing

        # Find the matching document
        target = None
        for entity in listing.entities:
            props = entity.properties
            if props.get("entry_number") == doc_number:
                if attachment_number is None and props.get("attachment_number") is None:
                    target = entity
                    break
                elif props.get("attachment_number") == attachment_number:
                    target = entity
                    break

        if target is None:
            att_desc = f" attachment {attachment_number}" if attachment_number else ""
            return Finding(
                notes=(
                    f"Document {doc_number}{att_desc} not found in "
                    f"RECAP archive for docket {docket_id}"
                ),
            )

        download_url = target.properties.get("download_url", "")
        if not download_url:
            return Finding(
                entities=[target],
                notes=(
                    f"Document {doc_number} found but not available for "
                    f"download in RECAP (may need PACER purchase first)"
                ),
            )

        # Download the PDF
        out_path = Path(output_dir)
        out_path.mkdir(parents=True, exist_ok=True)

        att_suffix = f".{attachment_number}" if attachment_number else ""
        filename = f"docket_{docket_id}_doc_{doc_number}{att_suffix}.pdf"
        filepath = out_path / filename

        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            resp = await client.get(download_url, headers=self._headers())
            resp.raise_for_status()
            filepath.write_bytes(resp.content)

        logger.info("Downloaded %s (%d bytes)", filepath, len(resp.content))

        return Finding(
            entities=[target],
            notes=(
                f"Downloaded: {filepath} "
                f"({len(resp.content):,} bytes, "
                f"{target.properties.get('page_count', '?')} pages) — "
                f"{target.properties.get('short_description', target.label)}"
            ),
        )

    @staticmethod
    def extract_text(pdf_path: str) -> str:
        """Extract text from a downloaded court document PDF.

        Uses pdfplumber (install with: uv add pdfplumber, or
        pip install 'ferret-osint[pdf]'). Falls back gracefully
        if pdfplumber is not installed.

        Args:
            pdf_path: Path to the PDF file.

        Returns:
            Extracted text, or an error message if extraction fails.
        """
        try:
            import pdfplumber
        except ImportError:
            return (
                "pdfplumber not installed. "
                "Install with: uv add pdfplumber, or pip install 'ferret-osint[pdf]'"
            )

        path = Path(pdf_path)
        if not path.exists():
            return f"File not found: {pdf_path}"

        pages_text: list[str] = []
        with pdfplumber.open(path) as pdf:
            for i, page in enumerate(pdf.pages, 1):
                text = page.extract_text() or ""
                if text.strip():
                    pages_text.append(f"--- Page {i} ---\n{text}")

        if not pages_text:
            return "No text extracted (PDF may be image-only or encrypted)"

        return "\n\n".join(pages_text)
