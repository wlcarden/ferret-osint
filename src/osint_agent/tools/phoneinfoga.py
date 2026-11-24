"""PhoneInfoga tool adapter — phone number intelligence.

Targets the PhoneInfoga v2 CLI, which prints plain text (the ``--output json``
flag existed only in v1 and was removed in v2 in favour of ``phoneinfoga serve``).
The ``local`` scanner yields number formatting and country; the ``googlesearch``
scanner yields prebuilt dork URLs. Carrier and line type require the numverify
scanner plugin plus ``NUMVERIFY_API_KEY``.
"""

import re

from osint_agent.models import (
    Entity,
    EntityType,
    Finding,
    Source,
)
from osint_agent.tools.base import ToolAdapter

# "Results for <scanner>" section headers in v2 text output.
_SECTION_RE = re.compile(r"^Results for (\w+)\s*$")

# "Key: value" lines inside a scanner section.
_KV_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9 _-]*):\s*(.+?)\s*$")

# Map PhoneInfoga's text labels (any scanner) onto entity property names.
_FIELD_MAP = {
    "raw local": "raw_local",
    "local": "local_format",
    "e164": "e164",
    "international": "international_format",
    "country": "country",
    "carrier": "carrier",
    "line type": "line_type",
    "valid": "valid",
}


class PhoneInfogaAdapter(ToolAdapter):
    """Wraps the PhoneInfoga v2 CLI for phone number scanning.

    Extracts number formatting, country, and prebuilt search dork URLs.
    Carrier/line-type enrichment requires the numverify plugin.
    """

    name = "phoneinfoga"
    required_binary = "phoneinfoga"
    install_hint = "see scripts/bootstrap.sh or github.com/sundowndev/phoneinfoga"

    async def run(self, phone_number: str) -> Finding:
        """Scan a phone number for available information.

        Args:
            phone_number: Phone number in E.164 format (e.g., +14155551234).
        """
        result = await self.run_subprocess(
            ["phoneinfoga", "scan", "-n", phone_number],
            timeout=60,
        )

        if result.returncode != 0:
            return Finding(
                notes=f"PhoneInfoga failed on '{phone_number}': {result.stderr[:500]}",
            )

        return self._parse_results(phone_number, result.stdout)

    def _parse_results(self, phone_number: str, stdout: str) -> Finding:
        """Parse PhoneInfoga v2 plain-text output into a PHONE entity."""
        sections = _split_sections(stdout)

        properties: dict = {
            "raw_number": phone_number,
            "source_system": "phoneinfoga",
        }
        # Merge fields across every scanner section. `local` always runs and
        # supplies formatting/country; optional plugins (numverify) add carrier
        # and line type, so a single-section read would drop them.
        for scanner in sorted(sections):
            for label, value in sections[scanner].items():
                key = _FIELD_MAP.get(label.lower())
                if key and value:
                    properties[key] = value

        dorks = _extract_dork_urls(stdout)
        if dorks:
            properties["search_dorks"] = dorks

        scanners = sorted(sections)
        if scanners:
            properties["scanners"] = scanners

        phone = Entity(
            id=f"phone:{phone_number}",
            entity_type=EntityType.PHONE,
            label=properties.get("e164")
            or properties.get("international_format")
            or phone_number,
            properties=properties,
            sources=[Source(tool=self.name, raw_data={"stdout": stdout[:4000]})],
        )

        return Finding(
            entities=[phone],
            notes=(
                f"PhoneInfoga scan of '{phone_number}':"
                f" country={properties.get('country', 'unknown')},"
                f" carrier={properties.get('carrier', 'unknown (needs numverify plugin)')},"
                f" {len(dorks)} search dork(s)"
            ),
        )


def _split_sections(stdout: str) -> dict[str, dict[str, str]]:
    """Split v2 text output into {scanner_name: {label: value}}."""
    sections: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None

    for line in stdout.splitlines():
        header = _SECTION_RE.match(line)
        if header:
            current = sections.setdefault(header.group(1).lower(), {})
            continue
        if current is None:
            continue
        kv = _KV_RE.match(line)
        # Dork URLs are captured separately; skip them here.
        if kv and kv.group(1).strip().lower() != "url":
            current[kv.group(1).strip()] = kv.group(2).strip()

    return sections


def _extract_dork_urls(stdout: str) -> list[str]:
    """Collect the prebuilt search URLs emitted by the googlesearch scanner."""
    urls = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("URL:"):
            url = stripped[4:].strip()
            if url:
                urls.append(url)
    return urls
