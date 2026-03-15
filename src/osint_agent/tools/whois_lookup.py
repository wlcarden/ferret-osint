"""WHOIS lookup tool adapter — domain registration and ownership data."""

import asyncio
import re
from datetime import datetime

from osint_agent.models import (
    Entity,
    EntityType,
    Finding,
    Relationship,
    RelationType,
    Source,
)
from osint_agent.tools.base import ToolAdapter

_PRIVACY_INDICATORS = [
    "privacy",
    "redacted",
    "whoisguard",
    "domains by proxy",
    "contact privacy",
    "withheld",
    "data protected",
    "not disclosed",
    "identity protect",
]


def _is_privacy_redacted(value: str) -> bool:
    """Check if a string contains common privacy/redaction service indicators."""
    if not value:
        return True
    lower = value.lower()
    return any(indicator in lower for indicator in _PRIVACY_INDICATORS)


def _normalize_date(value) -> str | None:
    """Normalize a whois date field to ISO format string.

    WHOIS dates can be a single datetime, a list of datetimes, or None.
    Takes the first element if a list.
    """
    if value is None:
        return None
    if isinstance(value, list):
        value = value[0] if value else None
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _normalize_name(name: str) -> str:
    """Normalize a name for use in entity IDs (lowercase, stripped, collapsed whitespace)."""
    return re.sub(r"\s+", "_", name.strip().lower())


class WhoisAdapter(ToolAdapter):
    """Wraps the python-whois library for domain WHOIS lookups.

    Extracts domain registration details, registrant information,
    nameservers, and builds entity relationships for organizations
    and persons found in registration data.
    """

    name = "whois"

    def is_available(self) -> bool:
        """Check if the python-whois package is importable."""
        try:
            import whois  # noqa: F401
            return True
        except ImportError:
            return False

    async def run(self, domain: str) -> Finding:
        """Perform a WHOIS lookup on a domain.

        Args:
            domain: Target domain (e.g., "example.com").
        """
        import whois

        loop = asyncio.get_event_loop()
        try:
            w = await loop.run_in_executor(None, whois.whois, domain)
        except Exception as e:
            return Finding(
                notes=f"WHOIS lookup failed for '{domain}': {e}",
            )

        return self._parse_results(domain, w)

    def _parse_results(self, domain: str, w) -> Finding:
        """Parse a WhoisEntry object into entities and relationships."""
        entities = []
        relationships = []

        source = Source(tool=self.name, source_url=f"whois://{domain}")
        domain_id = f"domain:{domain}"

        # Build domain properties from available WHOIS fields
        props: dict = {}
        if getattr(w, "registrar", None):
            props["registrar"] = w.registrar
        if getattr(w, "creation_date", None):
            props["creation_date"] = _normalize_date(w.creation_date)
        if getattr(w, "expiration_date", None):
            props["expiration_date"] = _normalize_date(w.expiration_date)
        if getattr(w, "updated_date", None):
            props["updated_date"] = _normalize_date(w.updated_date)
        if getattr(w, "name_servers", None):
            ns = w.name_servers
            if isinstance(ns, list):
                props["name_servers"] = [s.lower() for s in ns if s]
            elif ns:
                props["name_servers"] = [ns.lower()]
        if getattr(w, "dnssec", None):
            props["dnssec"] = w.dnssec
        if getattr(w, "status", None):
            status = w.status
            if isinstance(status, list):
                props["status"] = status
            elif status:
                props["status"] = [status]
        if getattr(w, "registrant_country", None):
            props["registrant_country"] = w.registrant_country
        if getattr(w, "registrant_state", None):
            props["registrant_state"] = w.registrant_state
        if getattr(w, "registrant_city", None):
            props["registrant_city"] = w.registrant_city

        domain_entity = Entity(
            id=domain_id,
            entity_type=EntityType.DOMAIN,
            label=domain,
            properties=props,
            sources=[source],
        )
        entities.append(domain_entity)

        # Registrant organization
        registrant_org = getattr(w, "registrant_org", None) or getattr(w, "org", None)
        if registrant_org and not _is_privacy_redacted(registrant_org):
            org_id = f"org:whois:{_normalize_name(registrant_org)}"
            entities.append(Entity(
                id=org_id,
                entity_type=EntityType.ORGANIZATION,
                label=registrant_org,
                sources=[source],
            ))
            relationships.append(Relationship(
                source_id=org_id,
                target_id=domain_id,
                relation_type=RelationType.OWNS,
                sources=[source],
            ))

        # Registrant name
        registrant_name = getattr(w, "registrant_name", None) or getattr(w, "name", None)
        if registrant_name and not _is_privacy_redacted(registrant_name):
            person_id = f"person:whois:{_normalize_name(registrant_name)}"
            entities.append(Entity(
                id=person_id,
                entity_type=EntityType.PERSON,
                label=registrant_name,
                sources=[source],
            ))
            relationships.append(Relationship(
                source_id=person_id,
                target_id=domain_id,
                relation_type=RelationType.OWNS,
                sources=[source],
            ))

        # Emails
        emails_raw = getattr(w, "emails", None)
        if emails_raw:
            if isinstance(emails_raw, str):
                emails_raw = [emails_raw]
            for email in emails_raw:
                if not email:
                    continue
                email_lower = email.lower()
                email_id = f"email:{email_lower}"
                entities.append(Entity(
                    id=email_id,
                    entity_type=EntityType.EMAIL,
                    label=email_lower,
                    sources=[source],
                ))
                relationships.append(Relationship(
                    source_id=domain_id,
                    target_id=email_id,
                    relation_type=RelationType.HAS_EMAIL,
                    sources=[source],
                ))

        # Summary counts
        email_count = len([
            e for e in entities if e.entity_type == EntityType.EMAIL
        ])
        ns_count = len(props.get("name_servers", []))
        notes = (
            f"WHOIS for '{domain}': registrar={props.get('registrar', 'N/A')}, "
            f"{email_count} emails, {ns_count} nameservers"
        )

        return Finding(
            entities=entities,
            relationships=relationships,
            notes=notes,
        )
