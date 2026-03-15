"""Gravatar tool adapter — email to profile/identity bridge.

Uses Gravatar's public MD5 endpoint to look up profiles associated
with an email address. Returns display name, preferred username,
location, and linked social accounts — bridging the gap between
an email address and online identities.

Gravatar profiles are opt-in, so coverage is partial. But when a
profile exists, it often contains high-value identity links: GitHub,
Twitter, personal site URLs, and self-reported locations.

No rate limits documented. No auth required.
"""

import hashlib
import logging

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

_BASE_URL = "https://gravatar.com"


class GravatarAdapter(ToolAdapter):
    """Look up Gravatar profiles by email address.

    Bridges email → identity by extracting display name,
    preferred username, location, and linked URLs from
    Gravatar profiles.
    """

    name = "gravatar"

    def __init__(self, timeout: int = 15):
        self.timeout = timeout

    def is_available(self) -> bool:
        return True  # Only needs httpx

    async def run(self, email: str) -> Finding:
        """Look up a Gravatar profile for an email address.

        Args:
            email: Email address to look up.
        """
        email_hash = hashlib.md5(
            email.strip().lower().encode(),
        ).hexdigest()

        url = f"{_BASE_URL}/{email_hash}.json"

        async with httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=True,
        ) as client:
            try:
                resp = await client.get(url)
                if resp.status_code == 404:
                    return Finding(
                        notes=f"Gravatar: no profile found for '{email}'",
                    )
                resp.raise_for_status()
                data = resp.json()
            except httpx.HTTPStatusError as e:
                return Finding(
                    notes=f"Gravatar: HTTP {e.response.status_code} for '{email}'",
                )
            except Exception as e:
                return Finding(notes=f"Gravatar: error for '{email}': {e}")

        return self._build_finding(email, email_hash, data)

    def _build_finding(
        self, email: str, email_hash: str, data: dict,
    ) -> Finding:
        """Build Finding from Gravatar API response."""
        entries = data.get("entry", [])
        if not entries:
            return Finding(
                notes=f"Gravatar: empty profile for '{email}'",
            )

        profile = entries[0]
        entities: list[Entity] = []
        relationships: list[Relationship] = []

        profile_url = profile.get("profileUrl", f"{_BASE_URL}/{email_hash}")
        source = Source(tool=self.name, source_url=profile_url)

        # Extract profile fields
        display_name = profile.get("displayName", "")
        preferred_username = profile.get("preferredUsername", "")
        location = profile.get("currentLocation", "")
        about_me = profile.get("aboutMe", "")

        # Name fields (structured)
        name_obj = profile.get("name", {})
        full_name = ""
        if name_obj:
            parts = []
            if name_obj.get("givenName"):
                parts.append(name_obj["givenName"])
            if name_obj.get("familyName"):
                parts.append(name_obj["familyName"])
            full_name = " ".join(parts)

        # Create person entity if we have a name
        person_label = full_name or display_name or preferred_username
        if person_label:
            person_id = f"person:gravatar:{email_hash}"
            person_props = {}
            if location:
                person_props["location"] = location
            if about_me:
                person_props["bio"] = about_me[:500]
            if full_name and display_name and full_name != display_name:
                person_props["display_name"] = display_name

            entities.append(Entity(
                id=person_id,
                entity_type=EntityType.PERSON,
                label=person_label,
                properties=person_props,
                sources=[source],
            ))

            # Link person to email
            email_id = f"email:{email}"
            entities.append(Entity(
                id=email_id,
                entity_type=EntityType.EMAIL,
                label=email,
                sources=[source],
            ))
            relationships.append(Relationship(
                source_id=person_id,
                target_id=email_id,
                relation_type=RelationType.HAS_EMAIL,
                sources=[source],
            ))

        # Create username entity if available
        if preferred_username:
            username_id = f"username:gravatar:{preferred_username}"
            entities.append(Entity(
                id=username_id,
                entity_type=EntityType.USERNAME,
                label=preferred_username,
                properties={"source": "gravatar"},
                sources=[source],
            ))
            if person_label:
                relationships.append(Relationship(
                    source_id=person_id,
                    target_id=username_id,
                    relation_type=RelationType.HAS_USERNAME,
                    sources=[source],
                ))

        # Extract linked URLs (social accounts, websites)
        urls = profile.get("urls", [])
        for url_entry in urls:
            url_value = url_entry.get("value", "")
            url_title = url_entry.get("title", "")
            if not url_value:
                continue

            # Try to identify the platform from the URL
            platform = _identify_platform(url_value) or url_title or "website"
            acct_username = _extract_username_from_url(url_value)

            acct_id = f"account:gravatar_link:{_slugify(url_value)}"
            acct_props = {
                "platform": platform,
                "url": url_value,
            }
            if acct_username:
                acct_props["username"] = acct_username

            entities.append(Entity(
                id=acct_id,
                entity_type=EntityType.ACCOUNT,
                label=f"{acct_username or url_title or platform} ({platform})",
                properties=acct_props,
                sources=[source],
            ))

            if person_label:
                relationships.append(Relationship(
                    source_id=person_id,
                    target_id=acct_id,
                    relation_type=RelationType.HAS_ACCOUNT,
                    sources=[source],
                ))

        # Photos (avatar URL)
        photos = profile.get("photos", [])
        avatar_url = photos[0].get("value", "") if photos else ""

        # Build notes
        notes_lines = [f"Gravatar profile for '{email}':"]
        if person_label:
            notes_lines.append(f"  Name: {person_label}")
        if preferred_username:
            notes_lines.append(f"  Username: {preferred_username}")
        if location:
            notes_lines.append(f"  Location: {location}")
        if about_me:
            notes_lines.append(f"  Bio: {about_me[:200]}")
        if urls:
            notes_lines.append(f"  Linked accounts: {len(urls)}")
            for u in urls:
                notes_lines.append(f"    {u.get('title', '')}: {u.get('value', '')}")
        if avatar_url:
            notes_lines.append(f"  Avatar: {avatar_url}")

        return Finding(
            entities=entities,
            relationships=relationships,
            notes="\n".join(notes_lines),
        )


def _identify_platform(url: str) -> str:
    """Identify the platform from a URL."""
    url_lower = url.lower()
    platforms = {
        "github.com": "GitHub",
        "twitter.com": "Twitter",
        "x.com": "Twitter",
        "linkedin.com": "LinkedIn",
        "facebook.com": "Facebook",
        "instagram.com": "Instagram",
        "mastodon": "Mastodon",
        "reddit.com": "Reddit",
        "youtube.com": "YouTube",
        "twitch.tv": "Twitch",
        "gitlab.com": "GitLab",
        "bitbucket.org": "Bitbucket",
        "stackoverflow.com": "StackOverflow",
        "medium.com": "Medium",
        "wordpress.com": "WordPress",
        "tumblr.com": "Tumblr",
        "flickr.com": "Flickr",
        "pinterest.com": "Pinterest",
        "tiktok.com": "TikTok",
        "discord.gg": "Discord",
        "telegram.me": "Telegram",
        "t.me": "Telegram",
    }
    for domain, platform in platforms.items():
        if domain in url_lower:
            return platform
    return ""


def _extract_username_from_url(url: str) -> str:
    """Try to extract a username from a profile URL."""
    import re

    # Common patterns: https://platform.com/username
    match = re.search(
        r"(?:github|twitter|x|instagram|reddit|gitlab|medium|tiktok)"
        r"\.com/([A-Za-z0-9_.-]+)",
        url, re.IGNORECASE,
    )
    if match:
        username = match.group(1)
        # Filter out non-user paths
        if username.lower() not in {"about", "help", "settings", "login", "signup"}:
            return username
    return ""


def _slugify(url: str) -> str:
    """Create a stable slug from a URL for entity IDs."""
    import re

    slug = url.lower()
    slug = re.sub(r"https?://", "", slug)
    slug = re.sub(r"[^a-z0-9]", "_", slug)
    slug = re.sub(r"_+", "_", slug).strip("_")
    return slug[:80]
