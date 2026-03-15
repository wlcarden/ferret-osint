"""theHarvester tool adapter — email and subdomain harvesting."""

import json
import shutil
import tempfile
from pathlib import Path

from osint_agent.models import (
    Entity,
    EntityType,
    Finding,
    Relationship,
    RelationType,
    Source,
)
from osint_agent.tools.base import ToolAdapter


class TheHarvesterAdapter(ToolAdapter):
    """Wraps theHarvester CLI for email and subdomain discovery.

    Searches across 30+ public sources to find emails, subdomains,
    IPs, and URLs associated with a domain.
    """

    name = "theharvester"

    def __init__(self, timeout: int = 120):
        self.timeout = timeout

    def is_available(self) -> bool:
        return shutil.which("theHarvester") is not None

    async def run(
        self,
        domain: str,
        sources: str = "baidu,brave,certspotter,crtsh,dnsdumpster,duckduckgo,hackertarget,rapiddns,urlscan",
        limit: int = 200,
    ) -> Finding:
        """Harvest emails, subdomains, and IPs for a domain.

        Args:
            domain: Target domain (e.g., "example.com").
            sources: Comma-separated list of data sources.
            limit: Maximum results per source.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            output_file = Path(tmpdir) / "results"
            cmd = [
                "theHarvester",
                "-d", domain,
                "-b", sources,
                "-l", str(limit),
                "-f", str(output_file),
            ]

            result = await self.run_subprocess(cmd, timeout=self.timeout)

            json_path = Path(f"{output_file}.json")
            if not json_path.exists():
                return Finding(notes=f"theHarvester produced no JSON output for '{domain}'. stderr: {result.stderr[:500]}")

            raw = json.loads(json_path.read_text())

        return self._parse_results(domain, raw)

    def _parse_results(self, domain: str, raw: dict) -> Finding:
        """Parse theHarvester JSON output into entities and relationships."""
        entities = []
        relationships = []

        # Domain entity
        domain_id = f"domain:{domain}"
        domain_entity = Entity(
            id=domain_id,
            entity_type=EntityType.DOMAIN,
            label=domain,
            sources=[Source(tool=self.name)],
        )
        entities.append(domain_entity)

        # Emails
        emails = raw.get("emails", []) or []
        for email in emails:
            if not email:
                continue
            email_id = f"email:{email.lower()}"
            entities.append(Entity(
                id=email_id,
                entity_type=EntityType.EMAIL,
                label=email,
                sources=[Source(tool=self.name)],
            ))
            relationships.append(Relationship(
                source_id=domain_id,
                target_id=email_id,
                relation_type=RelationType.HAS_EMAIL,
                sources=[Source(tool=self.name)],
            ))

        # Hosts/subdomains
        hosts = raw.get("hosts", []) or []
        for host in hosts:
            if not host:
                continue
            # Hosts may come as "subdomain:ip" or just "subdomain"
            host_str = host.split(":")[0] if ":" in host else host
            if host_str == domain:
                continue
            sub_id = f"domain:{host_str}"
            entities.append(Entity(
                id=sub_id,
                entity_type=EntityType.DOMAIN,
                label=host_str,
                properties={"parent_domain": domain},
                sources=[Source(tool=self.name)],
            ))
            relationships.append(Relationship(
                source_id=domain_id,
                target_id=sub_id,
                relation_type=RelationType.OWNS,
                properties={"relationship": "subdomain"},
                sources=[Source(tool=self.name)],
            ))

        # IPs
        ips = raw.get("ips", []) or []

        return Finding(
            entities=entities,
            relationships=relationships,
            notes=f"theHarvester for '{domain}': {len(emails)} emails, {len(hosts)} hosts, {len(ips)} IPs",
        )
