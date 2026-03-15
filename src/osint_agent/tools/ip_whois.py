"""IP WHOIS adapter — IP address to ASN/organization lookup.

Queries WHOIS data for IP addresses to find the owning organization,
ASN, network block, and geographic location. Complements domain WHOIS
with infrastructure-level ownership information.
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

_SOURCE = lambda: Source(tool="ip_whois")


class IpWhoisAdapter(ToolAdapter):
    """Look up ASN and organization info for an IP address."""

    name = "ip_whois"

    def is_available(self) -> bool:
        try:
            import ipwhois  # noqa: F401
            return True
        except ImportError:
            return False

    async def run(self, ip: str, **kwargs) -> Finding:
        """Query WHOIS for an IP address.

        Args:
            ip: IPv4 or IPv6 address.
        """
        import asyncio
        from ipwhois import IPWhois
        from ipwhois.exceptions import (
            IPDefinedError,
            ASNRegistryError,
            WhoisLookupError,
        )

        def _lookup():
            obj = IPWhois(ip)
            return obj.lookup_rdap(asn_methods=["whois", "dns", "http"])

        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(None, _lookup)
        except (IPDefinedError, ASNRegistryError, WhoisLookupError) as exc:
            return Finding(notes=f"IP WHOIS error for {ip}: {exc}")
        except Exception as exc:
            logger.warning("IP WHOIS failed for %s: %s", ip, exc)
            return Finding(notes=f"IP WHOIS error: {exc}")

        if not result:
            return Finding(notes=f"No WHOIS data for {ip}")

        entities: list[Entity] = []
        relationships: list[Relationship] = []

        # IP entity (modeled as domain for infrastructure).
        asn = result.get("asn")
        asn_desc = result.get("asn_description", "")
        asn_country = result.get("asn_country_code", "")
        network = result.get("network", {}) or {}

        ip_ent = Entity(
            id=f"domain:ip:{ip}",
            entity_type=EntityType.DOMAIN,
            label=ip,
            properties={
                k: v for k, v in {
                    "ip_address": ip,
                    "asn": asn,
                    "asn_description": asn_desc,
                    "asn_country": asn_country,
                    "network_cidr": result.get("asn_cidr"),
                    "network_name": network.get("name"),
                    "network_range": f"{network.get('start_address', '')} - {network.get('end_address', '')}",
                }.items() if v
            },
            sources=[_SOURCE()],
        )
        entities.append(ip_ent)

        # Organization entity from RDAP objects.
        objects = result.get("objects", {}) or {}
        for handle, obj in objects.items():
            contact = obj.get("contact", {}) or {}
            name = contact.get("name")
            if not name:
                continue

            org = Entity(
                id=f"organization:{handle.lower()}",
                entity_type=EntityType.ORGANIZATION,
                label=name,
                properties={
                    k: v for k, v in {
                        "handle": handle,
                        "kind": contact.get("kind"),
                        "address": _extract_address(contact),
                        "phone": _extract_phone(contact),
                        "email": _extract_email(contact),
                        "role": ", ".join(obj.get("roles", [])),
                    }.items() if v
                },
                sources=[_SOURCE()],
            )
            entities.append(org)
            relationships.append(Relationship(
                source_id=org.id,
                target_id=ip_ent.id,
                relation_type=RelationType.OWNS,
                properties={"roles": obj.get("roles", [])},
                sources=[_SOURCE()],
            ))

        notes = f"IP {ip}"
        if asn_desc:
            notes += f" | {asn_desc}"
        if asn:
            notes += f" | AS{asn}"

        return Finding(
            entities=entities,
            relationships=relationships,
            notes=notes,
        )


def _extract_address(contact: dict) -> str | None:
    """Pull first address string from RDAP contact."""
    for addr in contact.get("address", []) or []:
        val = addr.get("value")
        if val:
            return val.replace("\n", ", ")
    return None


def _extract_phone(contact: dict) -> str | None:
    """Pull first phone from RDAP contact."""
    for ph in contact.get("phone", []) or []:
        val = ph.get("value")
        if val:
            return val
    return None


def _extract_email(contact: dict) -> str | None:
    """Pull first email from RDAP contact."""
    for em in contact.get("email", []) or []:
        val = em.get("value")
        if val:
            return val
    return None
