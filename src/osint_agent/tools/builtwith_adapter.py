"""BuiltWith adapter — website technology fingerprinting.

Identifies what technologies a website is built with: CMS, frameworks,
analytics, hosting, CDN, JavaScript libraries, and more. Useful for
fingerprinting an organization's tech stack and finding related sites
using the same infrastructure.
No API key or authentication required (uses local detection).
"""

import logging

from osint_agent.models import (
    Entity,
    EntityType,
    Finding,
    Source,
)
from osint_agent.tools.base import ToolAdapter

logger = logging.getLogger(__name__)

_SOURCE = lambda: Source(tool="builtwith")


class BuiltWithAdapter(ToolAdapter):
    """Fingerprint website technologies."""

    name = "builtwith"

    def is_available(self) -> bool:
        try:
            import builtwith  # noqa: F401
            return True
        except ImportError:
            return False

    async def run(self, domain: str, **kwargs) -> Finding:
        """Detect technologies used by a website.

        Args:
            domain: Domain or URL to fingerprint.
        """
        import asyncio

        import builtwith

        url = domain if domain.startswith("http") else f"https://{domain}"

        def _detect():
            return builtwith.parse(url)

        loop = asyncio.get_event_loop()
        try:
            techs = await loop.run_in_executor(None, _detect)
        except Exception as exc:
            logger.warning("BuiltWith failed for %s: %s", domain, exc)
            return Finding(notes=f"BuiltWith error: {exc}")

        if not techs:
            return Finding(notes=f"No technologies detected for {domain}")

        # Flatten domain from URL.
        clean_domain = domain.split("//")[-1].split("/")[0]

        properties = {}
        total_techs = 0
        for category, items in sorted(techs.items()):
            properties[category] = items
            total_techs += len(items)

        entity = Entity(
            id=f"domain:{clean_domain}",
            entity_type=EntityType.DOMAIN,
            label=clean_domain,
            properties=properties,
            sources=[_SOURCE()],
        )

        return Finding(
            entities=[entity],
            notes=(
                f"BuiltWith: {total_techs} technologies detected "
                f"across {len(techs)} categories for {clean_domain}"
            ),
        )
