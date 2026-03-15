"""SEC EDGAR tool adapter — corporate filings, officers, insider transactions."""

import os

from osint_agent.models import (
    Entity,
    EntityType,
    Finding,
    Relationship,
    RelationType,
    Source,
)
from osint_agent.tools.base import ToolAdapter


class EdgarAdapter(ToolAdapter):
    """Wraps edgartools for SEC EDGAR data access.

    Provides:
    - Company lookup by name or ticker
    - Officer/director identification from filings
    - Insider transaction data (Form 4)
    - Company metadata (address, industry, filer status)
    """

    name = "edgar"

    def __init__(self):
        self._identity_set = False

    def is_available(self) -> bool:
        try:
            import edgar
            return True
        except ImportError:
            return False

    def _ensure_identity(self):
        if not self._identity_set:
            from edgar import set_identity
            user_agent = os.getenv(
                "SEC_EDGAR_USER_AGENT",
                "OSINTAgent osint-agent@localhost",
            )
            set_identity(user_agent)
            self._identity_set = True

    async def run(self, query: str, mode: str = "company") -> Finding:
        """Query SEC EDGAR.

        Args:
            query: Company name, ticker symbol, or CIK number.
            mode: "company" for basic info + recent filings,
                  "insiders" for insider transaction data (Form 4),
                  "search" for company name search.
        """
        self._ensure_identity()

        if mode == "search":
            return self._search_companies(query)
        elif mode == "insiders":
            return self._get_insider_transactions(query)
        else:
            return self._get_company_info(query)

    def _search_companies(self, query: str) -> Finding:
        """Search for companies by name."""
        from edgar import find

        results = find(query)
        entities = []

        for i, row in enumerate(results):
            if i >= 20:
                break
            if isinstance(row, dict):
                ticker = row.get("ticker", "")
                name = row.get("name", "")
            else:
                ticker = getattr(row, "ticker", "") or ""
                name = getattr(row, "name", "") or ""
            if not name:
                continue

            org_id = f"org:sec:{ticker or name}"
            entities.append(Entity(
                id=org_id,
                entity_type=EntityType.ORGANIZATION,
                label=name,
                properties={
                    "ticker": ticker,
                    "source_system": "sec_edgar",
                },
                sources=[Source(
                    tool=self.name,
                    source_url=f"https://www.sec.gov/cgi-bin/browse-edgar?company={query}&CIK=&type=&dateb=&owner=include&count=40&search_text=&action=getcompany",
                )],
            ))

        return Finding(
            entities=entities,
            notes=f"EDGAR search for '{query}' returned {len(entities)} companies",
        )

    def _get_company_info(self, query: str) -> Finding:
        """Get company details, metadata, and recent filing summary."""
        from edgar import Company

        try:
            company = Company(query)
        except Exception as e:
            return Finding(notes=f"EDGAR lookup failed for '{query}': {e}")

        entities = []
        relationships = []

        cik = str(company.cik)
        org_id = f"org:sec:{cik}"
        tickers = company.tickers if hasattr(company, "tickers") else []

        org = Entity(
            id=org_id,
            entity_type=EntityType.ORGANIZATION,
            label=company.name,
            properties={
                "cik": cik,
                "tickers": tickers,
                "sic": getattr(company, "sic", None),
                "industry": getattr(company, "industry", None),
                "filer_category": getattr(company, "filer_category", None),
                "fiscal_year_end": getattr(company, "fiscal_year_end", None),
                "source_system": "sec_edgar",
            },
            sources=[Source(
                tool=self.name,
                source_url=f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}",
            )],
        )
        entities.append(org)

        # Extract address as a separate entity
        addr_str = self._extract_address(company)
        if addr_str:
            addr_id = f"address:sec:{cik}"
            entities.append(Entity(
                id=addr_id,
                entity_type=EntityType.ADDRESS,
                label=addr_str,
                properties={"address_type": "business", "source_system": "sec_edgar"},
                sources=[Source(tool=self.name)],
            ))
            relationships.append(Relationship(
                source_id=org_id,
                target_id=addr_id,
                relation_type=RelationType.HAS_ADDRESS,
                sources=[Source(tool=self.name)],
            ))

        return Finding(
            entities=entities,
            relationships=relationships,
            notes=f"EDGAR company info for '{company.name}' (CIK: {cik})",
        )

    def _get_insider_transactions(self, query: str) -> Finding:
        """Get insider transactions (Form 4 filings) for a company."""
        from edgar import Company

        try:
            company = Company(query)
        except Exception as e:
            return Finding(notes=f"EDGAR lookup failed for '{query}': {e}")

        cik = str(company.cik)
        org_id = f"org:sec:{cik}"
        entities = []
        relationships = []

        org = Entity(
            id=org_id,
            entity_type=EntityType.ORGANIZATION,
            label=company.name,
            properties={"cik": cik, "source_system": "sec_edgar"},
            sources=[Source(tool=self.name)],
        )
        entities.append(org)

        # Get recent Form 4 filings (insider transactions)
        try:
            filings = company.get_filings(form="4")
            recent = filings.latest(20)
        except Exception:
            return Finding(
                entities=entities,
                notes=f"Could not retrieve Form 4 filings for '{company.name}'",
            )

        seen_filers = set()
        for filing in recent:
            filing_date = str(getattr(filing, "filing_date", ""))
            accession = getattr(filing, "accession_no", "")

            # Extract the reporting owner (insider) from index headers
            ent_name = ""
            ent_cik = ""
            try:
                headers = filing.index_headers
                ro = getattr(headers, "reporting_owner", None)
                if ro:
                    owner_data = getattr(ro, "owner_data", None)
                    if owner_data:
                        ent_name = getattr(owner_data, "conformed_name", "")
                        ent_cik = str(getattr(owner_data, "cik", "")).lstrip("0")
            except Exception:
                pass

            if ent_cik and ent_cik != cik and ent_cik not in seen_filers:
                seen_filers.add(ent_cik)
                person_id = f"person:sec:{ent_cik}"
                entities.append(Entity(
                    id=person_id,
                    entity_type=EntityType.PERSON,
                    label=ent_name,
                    properties={"cik": ent_cik, "source_system": "sec_edgar"},
                    sources=[Source(
                        tool=self.name,
                        source_url=f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={ent_cik}",
                    )],
                ))
                relationships.append(Relationship(
                    source_id=person_id,
                    target_id=org_id,
                    relation_type=RelationType.OFFICER_OF,
                    properties={"evidence": "form_4_filing", "filing_date": filing_date},
                    sources=[Source(
                        tool=self.name,
                        source_url=f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}",
                    )],
                ))

            # Also create a document entity for the filing itself
            if accession:
                doc_id = f"document:sec:{accession}"
                entities.append(Entity(
                    id=doc_id,
                    entity_type=EntityType.DOCUMENT,
                    label=f"Form 4 — {company.name} ({filing_date})",
                    properties={
                        "form_type": "4",
                        "filing_date": filing_date,
                        "accession_no": accession,
                        "source_system": "sec_edgar",
                    },
                    sources=[Source(tool=self.name)],
                ))
                relationships.append(Relationship(
                    source_id=org_id,
                    target_id=doc_id,
                    relation_type=RelationType.FILED,
                    properties={"form_type": "4"},
                    sources=[Source(tool=self.name)],
                ))

        return Finding(
            entities=entities,
            relationships=relationships,
            notes=f"EDGAR Form 4: found {len(seen_filers)} insiders for '{company.name}'",
        )

    def _extract_address(self, company) -> str | None:
        """Try to extract a business address string from company data."""
        try:
            data = company.data
            addresses = getattr(data, "addresses", None)
            if addresses:
                biz = addresses.get("business", {})
                parts = [
                    biz.get("street1", ""),
                    biz.get("street2", ""),
                    biz.get("city", ""),
                    biz.get("stateOrCountry", ""),
                    biz.get("zipCode", ""),
                ]
                addr = ", ".join(p for p in parts if p)
                return addr if addr else None
        except Exception:
            pass
        return None
