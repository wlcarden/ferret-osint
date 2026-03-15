"""CrossLinked adapter — LinkedIn employee enumeration via search engines.

Discovers employee names at an organization by scraping Google/Bing
for LinkedIn profile snippets. Never touches LinkedIn directly —
uses search engine dorks to find "site:linkedin.com/in/ Company" results.
No API key, no LinkedIn account required.
"""

import logging
import re

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

_SOURCE = lambda: Source(tool="crosslinked")


class CrossLinkedAdapter(ToolAdapter):
    """Enumerate employee names at a company via search engine LinkedIn dorks."""

    name = "crosslinked"

    def is_available(self) -> bool:
        try:
            import crosslinked  # noqa: F401
            return True
        except ImportError:
            return False

    async def run(self, company: str, **kwargs) -> Finding:
        """Search for LinkedIn profiles associated with a company.

        Args:
            company: Company or organization name.
        """
        import tempfile
        import csv
        import os

        # CrossLinked writes to a file. We use a temp file and parse it.
        with tempfile.NamedTemporaryFile(
            suffix=".csv", delete=False, mode="w",
        ) as f:
            outfile = f.name

        try:
            result = await self.run_subprocess(
                [
                    "crosslinked", company,
                    "-f", "{first} {last}",
                    "-o", outfile,
                    "-t", "15",
                ],
                timeout=60,
            )
        except Exception as exc:
            logger.warning("CrossLinked failed for %s: %s", company, exc)
            try:
                os.unlink(outfile)
            except OSError:
                pass
            return Finding(notes=f"CrossLinked error: {exc}")

        # Parse CSV output.
        names: list[dict] = []
        try:
            with open(outfile, "r", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    name = row.get("name", "").strip()
                    title = row.get("title", "").strip()
                    if name:
                        names.append({"name": name, "title": title})
        except Exception:
            pass
        finally:
            try:
                os.unlink(outfile)
            except OSError:
                pass

        if not names:
            # Try parsing stdout as fallback (some versions output there).
            names = _parse_stdout(result.stdout if result else "")

        if not names:
            return Finding(notes=f"CrossLinked: no employees found for {company}")

        entities: list[Entity] = []
        relationships: list[Relationship] = []

        org = Entity(
            id=f"organization:linkedin:{_slug(company)}",
            entity_type=EntityType.ORGANIZATION,
            label=company,
            sources=[_SOURCE()],
        )
        entities.append(org)

        seen: set[str] = set()
        for entry in names:
            name = entry["name"]
            slug = _slug(name)
            if slug in seen:
                continue
            seen.add(slug)

            person = Entity(
                id=f"person:linkedin:{slug}",
                entity_type=EntityType.PERSON,
                label=name,
                properties={
                    k: v for k, v in {
                        "title": entry.get("title") or None,
                        "source_platform": "LinkedIn (via search engine)",
                    }.items() if v
                },
                sources=[_SOURCE()],
            )
            entities.append(person)
            relationships.append(Relationship(
                source_id=person.id,
                target_id=org.id,
                relation_type=RelationType.WORKS_AT,
                sources=[_SOURCE()],
            ))

        return Finding(
            entities=entities,
            relationships=relationships,
            notes=f"CrossLinked: {len(seen)} employees found at {company}",
        )


def _slug(name: str) -> str:
    """Normalize a name to a stable ID slug."""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _parse_stdout(stdout: str) -> list[dict]:
    """Attempt to parse names from CrossLinked stdout."""
    names = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        # CrossLinked outputs "First Last" lines.
        if line and not line.startswith(("[", "#", "-", "=")):
            parts = line.split()
            if 1 < len(parts) <= 5 and all(p.isalpha() for p in parts):
                names.append({"name": line, "title": ""})
    return names
