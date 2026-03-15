"""Steam Community tool adapter — public profile lookup via XML endpoint.

Uses Steam's deprecated-but-functional XML profile endpoint to look up
user profiles by vanity URL. No API key required.

Endpoint: https://steamcommunity.com/id/{vanity_url}/?xml=1

Returns: persona name, real name (if set), location, account creation date,
Steam ID, avatar, and profile visibility status.
"""

import logging
import xml.etree.ElementTree as ET

import httpx

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

_BASE_URL = "https://steamcommunity.com/id"
_USER_AGENT = "OSINT-Agent/0.1"
_MAX_RETRIES = 2
_BACKOFF = 3


class SteamAdapter(ToolAdapter):
    """Look up Steam Community profiles by vanity URL.

    No API key needed — uses the public XML profile endpoint.
    Only works for users who have set a custom vanity URL.
    """

    name = "steam"

    def __init__(self, timeout: int = 15):
        self.timeout = timeout

    def is_available(self) -> bool:
        return True  # Only needs httpx

    async def run(self, username: str) -> Finding:
        """Look up a Steam profile by vanity URL / custom ID.

        Args:
            username: Steam vanity URL (the custom part of steamcommunity.com/id/{this}).
        """
        username = username.strip().lower()
        url = f"{_BASE_URL}/{username}/?xml=1"
        profile_url = f"{_BASE_URL}/{username}"

        async with httpx.AsyncClient(
            timeout=self.timeout,
            headers={"User-Agent": _USER_AGENT},
            follow_redirects=True,
        ) as client:
            for attempt in range(_MAX_RETRIES):
                try:
                    resp = await client.get(url)
                    if resp.status_code in (404, 500):
                        return Finding(
                            notes=f"Steam: no profile found for vanity URL '{username}'",
                        )
                    if resp.status_code == 429:
                        import asyncio
                        await asyncio.sleep(_BACKOFF * (2 ** attempt))
                        continue
                    resp.raise_for_status()
                    break
                except httpx.HTTPStatusError as e:
                    if attempt < _MAX_RETRIES - 1:
                        continue
                    return Finding(
                        notes=f"Steam: HTTP {e.response.status_code} for '{username}'",
                    )
            else:
                return Finding(notes=f"Steam: retries exhausted for '{username}'")

        return self._parse_xml(resp.text, username, profile_url)

    def _parse_xml(self, xml_text: str, username: str, profile_url: str) -> Finding:
        """Parse Steam XML profile response."""
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as e:
            return Finding(notes=f"Steam: XML parse error for '{username}': {e}")

        # Check for error response
        error = root.find("error")
        if error is not None:
            return Finding(
                notes=f"Steam: {error.text} (vanity URL: '{username}')",
            )

        source = Source(tool=self.name, source_url=profile_url)
        entities: list[Entity] = []
        relationships: list[Relationship] = []

        # Extract profile fields
        steam_id64 = _xml_text(root, "steamID64", "")
        persona_name = _xml_text(root, "steamID", "")  # steamID = display name in XML
        real_name = _xml_text(root, "realname", "")
        location = _xml_text(root, "location", "")
        member_since = _xml_text(root, "memberSince", "")
        avatar_full = _xml_text(root, "avatarFull", "")
        summary = _xml_text(root, "summary", "")
        visibility = _xml_text(root, "privacyState", "")
        vac_banned = _xml_text(root, "vacBanned", "0")
        online_state = _xml_text(root, "onlineState", "")

        # Custom URL
        custom_url = _xml_text(root, "customURL", username)

        # Account entity
        account_props = {
            "platform": "Steam",
            "username": custom_url or username,
            "url": profile_url,
            "steam_id64": steam_id64,
            "persona_name": persona_name,
            "visibility": visibility,
            "member_since": member_since,
            "online_state": online_state,
        }
        if real_name:
            account_props["real_name"] = real_name
        if location:
            account_props["location"] = location
        if vac_banned == "1":
            account_props["vac_banned"] = True
        if avatar_full:
            account_props["avatar_url"] = avatar_full
        if summary and summary != "No information given.":
            # Strip CDATA/HTML tags from summary
            import re
            clean_summary = re.sub(r"<[^>]+>", "", summary).strip()
            if clean_summary and clean_summary != "No information given.":
                account_props["bio"] = clean_summary[:500]

        account_entity = Entity(
            id=f"account:steam:{custom_url or username}",
            entity_type=EntityType.ACCOUNT,
            label=f"{persona_name or username} on Steam",
            properties=account_props,
            sources=[source],
        )
        entities.append(account_entity)

        # If real name is set, create a person entity
        if real_name:
            person_id = f"person:steam:{custom_url or username}"
            person_props = {}
            if location:
                person_props["location"] = location
            entities.append(Entity(
                id=person_id,
                entity_type=EntityType.PERSON,
                label=real_name,
                properties=person_props,
                sources=[source],
            ))
            relationships.append(Relationship(
                source_id=person_id,
                target_id=account_entity.id,
                relation_type=RelationType.HAS_ACCOUNT,
                sources=[source],
            ))

        # Build notes
        notes_lines = [f"Steam profile: {profile_url}"]
        notes_lines.append(f"  Persona: {persona_name}")
        if real_name:
            notes_lines.append(f"  Real name: {real_name}")
        if location:
            notes_lines.append(f"  Location: {location}")
        notes_lines.append(f"  SteamID64: {steam_id64}")
        notes_lines.append(f"  Member since: {member_since}")
        notes_lines.append(f"  Visibility: {visibility}")
        if vac_banned == "1":
            notes_lines.append("  VAC BANNED")

        return Finding(
            entities=entities,
            relationships=relationships,
            notes="\n".join(notes_lines),
        )


def _xml_text(root: ET.Element, tag: str, default: str = "") -> str:
    """Safely extract text from an XML element."""
    elem = root.find(tag)
    if elem is not None and elem.text:
        return elem.text.strip()
    return default
