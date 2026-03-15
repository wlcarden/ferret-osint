"""DNS enumeration adapter — raw DNS record lookups.

Queries authoritative DNS records (MX, TXT, NS, SOA, A, AAAA, CNAME)
for a domain. Useful for discovering mail servers, SPF/DKIM/DMARC
policies, name servers, and infrastructure connections.
No API key or authentication required.
"""

import logging

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

_SOURCE = lambda: Source(tool="dns_enum")

# Record types to query and their OSINT value.
_RECORD_TYPES = ["A", "AAAA", "MX", "NS", "TXT", "SOA", "CNAME"]


class DnsEnumAdapter(ToolAdapter):
    """Enumerate DNS records for a domain."""

    name = "dns_enum"

    def is_available(self) -> bool:
        try:
            import dns.resolver  # noqa: F401
            return True
        except ImportError:
            return False

    async def run(self, domain: str, **kwargs) -> Finding:
        """Query multiple DNS record types for a domain.

        Args:
            domain: Domain name to enumerate.
        """
        import asyncio

        import dns.exception
        import dns.resolver

        entities: list[Entity] = []
        relationships: list[Relationship] = []
        all_records: dict[str, list[str]] = {}

        base = Entity(
            id=f"domain:{domain}",
            entity_type=EntityType.DOMAIN,
            label=domain,
            sources=[_SOURCE()],
        )
        entities.append(base)

        loop = asyncio.get_event_loop()

        for rtype in _RECORD_TYPES:
            try:
                answers = await loop.run_in_executor(
                    None,
                    lambda rt=rtype: dns.resolver.resolve(domain, rt),
                )
                records = [rdata.to_text().rstrip(".") for rdata in answers]
                all_records[rtype] = records
            except (
                dns.resolver.NoAnswer,
                dns.resolver.NXDOMAIN,
                dns.resolver.NoNameservers,
                dns.exception.Timeout,
            ):
                continue
            except Exception as exc:
                logger.debug("DNS %s query for %s failed: %s", rtype, domain, exc)
                continue

        # Build entities from discovered records.
        seen: set[str] = set()

        # MX records → email infrastructure.
        for mx in all_records.get("MX", []):
            # MX records have priority prefix: "10 mail.example.com"
            parts = mx.split(None, 1)
            host = parts[-1] if parts else mx
            if host in seen:
                continue
            seen.add(host)
            mx_ent = Entity(
                id=f"domain:{host}",
                entity_type=EntityType.DOMAIN,
                label=host,
                properties={
                    "role": "mail_server",
                    "mx_priority": parts[0] if len(parts) > 1 else None,
                },
                sources=[_SOURCE()],
            )
            entities.append(mx_ent)
            relationships.append(Relationship(
                source_id=base.id,
                target_id=mx_ent.id,
                relation_type=RelationType.CONNECTED_TO,
                properties={"via": "MX_record"},
                sources=[_SOURCE()],
            ))

        # NS records → name server infrastructure.
        for ns in all_records.get("NS", []):
            if ns in seen:
                continue
            seen.add(ns)
            ns_ent = Entity(
                id=f"domain:{ns}",
                entity_type=EntityType.DOMAIN,
                label=ns,
                properties={"role": "nameserver"},
                sources=[_SOURCE()],
            )
            entities.append(ns_ent)
            relationships.append(Relationship(
                source_id=base.id,
                target_id=ns_ent.id,
                relation_type=RelationType.CONNECTED_TO,
                properties={"via": "NS_record"},
                sources=[_SOURCE()],
            ))

        # Store all records as properties on the base domain.
        base.properties = {
            f"dns_{rtype.lower()}": records
            for rtype, records in all_records.items()
            if records
        }

        # Extract interesting TXT data.
        txt_records = all_records.get("TXT", [])
        spf = [t for t in txt_records if "v=spf1" in t]
        _dmarc_domain = f"_dmarc.{domain}"
        if spf:
            base.properties["spf_policy"] = spf[0]

        notes_parts = []
        for rtype, records in sorted(all_records.items()):
            notes_parts.append(f"{rtype}: {len(records)}")

        return Finding(
            entities=entities,
            relationships=relationships,
            notes=f"DNS for {domain} — " + ", ".join(notes_parts),
        )
