"""Maigret tool adapter — username search across 2500+ sites."""

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
from osint_agent.tools.maigret_filters import is_false_positive

# Maigret extracted_ids fields that represent actual cross-referenceable
# identifiers (user IDs, alternative usernames). Everything else (bio,
# follower_count, is_verified, created_at, etc.) is profile metadata.
_XREF_ID_TYPES = frozenset({
    "uid", "id", "username", "nickname",
    "disqus_username", "imgur_username", "gravatar_username",
    "wikimapia_uid",
})


class MaigretAdapter(ToolAdapter):
    """Wraps the maigret CLI to search for usernames across platforms."""

    name = "maigret"

    def __init__(self, timeout: int = 60, top_sites: int | None = None):
        self.timeout = timeout
        self.top_sites = top_sites

    def is_available(self) -> bool:
        return shutil.which("maigret") is not None

    async def run(self, username: str) -> Finding:
        """Search for a username across platforms.

        Returns a Finding containing:
        - A USERNAME entity for the search target
        - An ACCOUNT entity for each platform where the username was found
        - HAS_ACCOUNT relationships linking username to each account
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            cmd = [
                "maigret",
                username,
                "--json", "simple",
                "--timeout", str(self.timeout),
                "--no-progressbar",
                "--no-color",
                "--folderoutput", tmpdir,
            ]
            if self.top_sites:
                cmd.extend(["--top-sites", str(self.top_sites)])

            result = await self.run_subprocess(cmd, timeout=self.timeout + 30)

            report_path = Path(tmpdir) / f"report_{username}_simple.json"
            if not report_path.exists():
                return Finding(notes=f"Maigret produced no output for '{username}'. stderr: {result.stderr[:500]}")

            raw = json.loads(report_path.read_text())

        return self._parse_results(username, raw)

    def _parse_results(self, username: str, raw: dict) -> Finding:
        """Parse maigret JSON output into entities and relationships."""
        entities = []
        relationships = []

        username_entity = Entity(
            id=f"username:{username}",
            entity_type=EntityType.USERNAME,
            label=username,
            sources=[Source(tool=self.name)],
        )
        entities.append(username_entity)

        claimed_count = 0
        filtered_count = 0
        for site_name, data in raw.items():
            status = data.get("status", {})
            if status.get("status") != "Claimed":
                continue

            http_status = data.get("http_status")
            fp_reason = is_false_positive(site_name, http_status)
            if fp_reason:
                filtered_count += 1
                continue

            claimed_count += 1
            url = data.get("url_user", "")
            tags = status.get("tags", [])
            extracted_ids = status.get("ids", {})

            account_id = f"account:{site_name.lower()}:{username}"
            account = Entity(
                id=account_id,
                entity_type=EntityType.ACCOUNT,
                label=f"{username} on {site_name}",
                properties={
                    "platform": site_name,
                    "url": url,
                    "tags": tags,
                    "extracted_ids": extracted_ids,
                    "http_status": data.get("http_status"),
                    "rank": data.get("rank"),
                },
                sources=[Source(
                    tool=self.name,
                    source_url=url,
                    raw_data={"site_name": site_name, "status": status},
                )],
            )
            entities.append(account)

            rel = Relationship(
                source_id=username_entity.id,
                target_id=account_id,
                relation_type=RelationType.HAS_ACCOUNT,
                properties={"platform": site_name},
                sources=[Source(tool=self.name, source_url=url)],
            )
            relationships.append(rel)

            # Create cross-reference entities for actual identifiers.
            # Maigret's extracted_ids include profile metadata (follower_count,
            # is_verified, type, etc.) that are not usable for cross-referencing.
            for id_type, id_value in extracted_ids.items():
                if not id_value or id_type not in _XREF_ID_TYPES:
                    continue
                xref_id = f"username:{id_type}:{id_value}"
                xref = Entity(
                    id=xref_id,
                    entity_type=EntityType.USERNAME,
                    label=f"{id_type}:{id_value}",
                    properties={"id_type": id_type},
                    sources=[Source(tool=self.name, source_url=url)],
                )
                entities.append(xref)
                relationships.append(Relationship(
                    source_id=account_id,
                    target_id=xref_id,
                    relation_type=RelationType.ALSO_KNOWN_AS,
                    sources=[Source(tool=self.name, source_url=url)],
                ))

        notes = f"Maigret found {claimed_count} accounts for username '{username}'"
        if filtered_count:
            notes += f" ({filtered_count} false positives filtered)"

        return Finding(
            entities=entities,
            relationships=relationships,
            notes=notes,
        )
