"""crt.sh adapter — Certificate Transparency subdomain discovery.

Queries the crt.sh database (Sectigo's CT log aggregator) to find
SSL/TLS certificates issued for a domain and its subdomains.
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

_SOURCE = lambda: Source(tool="crtsh")


class CrtshAdapter(ToolAdapter):
    """Discover subdomains via Certificate Transparency logs."""

    name = "crtsh"

    def is_available(self) -> bool:
        try:
            import pycrtsh  # noqa: F401
            return True
        except ImportError:
            return False

    async def run(self, domain: str, **kwargs) -> Finding:
        """Query crt.sh for certificates matching a domain.

        Args:
            domain: Base domain to search (e.g., "example.com").
        """
        import asyncio
        from pycrtsh import Crtsh

        def _query():
            c = Crtsh()
            return c.search(domain)

        loop = asyncio.get_event_loop()
        try:
            results = await loop.run_in_executor(None, _query)
        except Exception as exc:
            logger.warning("crt.sh query failed for %s: %s", domain, exc)
            return Finding(notes=f"crt.sh error: {exc}")

        if not results:
            return Finding(notes=f"No certificates found for {domain}")

        entities: list[Entity] = []
        relationships: list[Relationship] = []
        seen_domains: set[str] = set()

        # Base domain entity.
        base = Entity(
            id=f"domain:{domain}",
            entity_type=EntityType.DOMAIN,
            label=domain,
            sources=[_SOURCE()],
        )
        entities.append(base)
        seen_domains.add(domain)

        for cert in results:
            name = cert.get("name", "")
            if not name:
                continue

            # crt.sh returns wildcard entries like "*.example.com".
            name = name.lstrip("*.")
            if not name or name in seen_domains:
                continue
            seen_domains.add(name)

            sub = Entity(
                id=f"domain:{name}",
                entity_type=EntityType.DOMAIN,
                label=name,
                properties={
                    k: v for k, v in {
                        "issuer": cert.get("issuer"),
                        "not_before": cert.get("not_before"),
                        "not_after": cert.get("not_after"),
                    }.items() if v is not None
                },
                sources=[_SOURCE()],
            )
            entities.append(sub)
            relationships.append(Relationship(
                source_id=base.id,
                target_id=sub.id,
                relation_type=RelationType.CONNECTED_TO,
                properties={"via": "certificate_transparency"},
                sources=[_SOURCE()],
            ))

        return Finding(
            entities=entities,
            relationships=relationships,
            notes=(
                f"crt.sh: {len(seen_domains) - 1} subdomains "
                f"discovered for {domain}"
            ),
        )
