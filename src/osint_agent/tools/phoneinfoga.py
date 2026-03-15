"""PhoneInfoga tool adapter — phone number intelligence."""

import json

from osint_agent.models import (
    Entity,
    EntityType,
    Finding,
    Source,
)
from osint_agent.tools.base import ToolAdapter


class PhoneInfogaAdapter(ToolAdapter):
    """Wraps the PhoneInfoga CLI for phone number scanning.

    Extracts carrier info, location data, line type, and
    performs Google/Numverify lookups on phone numbers.
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
            ["phoneinfoga", "scan", "-n", phone_number, "--output", "json"],
            timeout=60,
        )

        if result.returncode != 0:
            return Finding(notes=f"PhoneInfoga failed on '{phone_number}': {result.stderr[:500]}")

        try:
            data = self.parse_json_output(result.stdout)
        except (json.JSONDecodeError, ValueError):
            return Finding(notes=f"PhoneInfoga returned unparseable output for '{phone_number}'")

        return self._parse_results(phone_number, data)

    def _parse_results(self, phone_number: str, data: dict | list) -> Finding:
        """Parse PhoneInfoga JSON output into entities."""
        if isinstance(data, list):
            data = data[0] if data else {}

        phone_id = f"phone:{phone_number}"
        properties = {
            "raw_number": phone_number,
            "source_system": "phoneinfoga",
        }

        # PhoneInfoga output structure varies by version, extract what's available
        if isinstance(data, dict):
            properties.update({
                k: v for k, v in {
                    "valid": data.get("valid"),
                    "carrier": data.get("carrier"),
                    "country": data.get("country"),
                    "country_code": data.get("countryCode"),
                    "international_format": data.get("international_format") or data.get("formatInternational"),
                    "local_format": data.get("local_format") or data.get("formatNational"),
                    "line_type": data.get("line_type") or data.get("lineType"),
                    "location": data.get("location"),
                }.items()
                if v is not None
            })

        phone = Entity(
            id=phone_id,
            entity_type=EntityType.PHONE,
            label=properties.get("international_format", phone_number),
            properties=properties,
            sources=[Source(tool=self.name, raw_data=data if isinstance(data, dict) else None)],
        )

        return Finding(
            entities=[phone],
            notes=f"PhoneInfoga scan of '{phone_number}': carrier={properties.get('carrier', 'unknown')}, country={properties.get('country', 'unknown')}",
        )
