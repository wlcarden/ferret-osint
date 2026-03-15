"""Wayback Machine tool adapter — archived web page retrieval."""

import asyncio
import logging

from osint_agent.models import (
    Entity,
    EntityType,
    Finding,
    Source,
)
from osint_agent.tools.base import ToolAdapter

logger = logging.getLogger(__name__)

# Retry settings for 429 rate-limit responses
MAX_RETRIES = 3
INITIAL_BACKOFF = 2  # seconds


class WaybackAdapter(ToolAdapter):
    """Wraps waybackpy for Internet Archive / Wayback Machine access.

    Provides:
    - Check if a URL has been archived
    - Get the most recent archived snapshot
    - Get the oldest archived snapshot
    - List all available snapshots for a URL (via CDX API)

    Includes exponential backoff for 429 rate-limit responses from
    the Internet Archive API.
    """

    name = "wayback"

    def is_available(self) -> bool:
        try:
            import waybackpy
            return True
        except ImportError:
            return False

    async def run(self, url: str, mode: str = "snapshots") -> Finding:
        """Query Wayback Machine for a URL.

        Args:
            url: The URL to look up.
            mode: "newest" for most recent snapshot,
                  "oldest" for first archived snapshot,
                  "snapshots" for CDX index of all snapshots.
        """
        import waybackpy

        user_agent = "OSINT-Agent/0.1"

        if mode == "newest":
            return await self._get_snapshot_with_retry(
                url, user_agent, newest=True,
            )
        elif mode == "oldest":
            return await self._get_snapshot_with_retry(
                url, user_agent, newest=False,
            )
        return await self._get_cdx_with_retry(url, user_agent)

    async def _get_snapshot_with_retry(
        self, url: str, user_agent: str, newest: bool = True,
    ) -> Finding:
        """Get a single snapshot with retry on 429."""
        for attempt in range(MAX_RETRIES):
            try:
                return self._get_snapshot(url, user_agent, newest)
            except Exception as e:
                if _is_rate_limit(e) and attempt < MAX_RETRIES - 1:
                    wait = INITIAL_BACKOFF * (2 ** attempt)
                    logger.info(
                        "Wayback 429: retrying in %ds (attempt %d/%d)",
                        wait, attempt + 1, MAX_RETRIES,
                    )
                    await asyncio.sleep(wait)
                    continue
                raise
        # unreachable, but satisfies type checker
        return Finding(notes=f"Wayback: retries exhausted for '{url}'")

    async def _get_cdx_with_retry(
        self, url: str, user_agent: str,
    ) -> Finding:
        """Get CDX snapshots with retry on 429."""
        for attempt in range(MAX_RETRIES):
            try:
                return self._get_cdx_snapshots(url, user_agent)
            except Exception as e:
                if _is_rate_limit(e) and attempt < MAX_RETRIES - 1:
                    wait = INITIAL_BACKOFF * (2 ** attempt)
                    logger.info(
                        "Wayback CDX 429: retrying in %ds (attempt %d/%d)",
                        wait, attempt + 1, MAX_RETRIES,
                    )
                    await asyncio.sleep(wait)
                    continue
                raise
        return Finding(notes=f"Wayback CDX: retries exhausted for '{url}'")

    def _get_snapshot(self, url: str, user_agent: str, newest: bool = True) -> Finding:
        """Get a single snapshot (newest or oldest)."""
        import waybackpy

        try:
            availability = waybackpy.WaybackMachineAvailabilityAPI(url, user_agent)
            if newest:
                snapshot = availability.newest()
            else:
                snapshot = availability.oldest()
        except Exception as e:
            if _is_rate_limit(e):
                raise  # let retry wrapper handle it
            return Finding(
                notes=(
                    f"Wayback Machine: no "
                    f"{'newest' if newest else 'oldest'} snapshot "
                    f"for '{url}': {e}"
                ),
            )

        archive_url = snapshot.archive_url
        timestamp = str(snapshot.timestamp()) if hasattr(snapshot, "timestamp") else ""

        entity = Entity(
            id=f"document:wayback:{'newest' if newest else 'oldest'}:{url}",
            entity_type=EntityType.DOCUMENT,
            label=f"Archived: {url} ({'newest' if newest else 'oldest'})",
            properties={
                "original_url": url,
                "archive_url": archive_url,
                "timestamp": timestamp,
                "snapshot_type": "newest" if newest else "oldest",
                "source_system": "wayback",
            },
            sources=[Source(tool=self.name, source_url=archive_url)],
        )

        return Finding(
            entities=[entity],
            notes=f"Wayback Machine {'newest' if newest else 'oldest'} snapshot for '{url}': {archive_url}",
        )

    def _get_cdx_snapshots(self, url: str, user_agent: str) -> Finding:
        """Get all CDX snapshots for a URL."""
        import waybackpy

        try:
            cdx = waybackpy.WaybackMachineCDXServerAPI(url, user_agent)
            snapshots = list(cdx.snapshots())
        except Exception as e:
            if _is_rate_limit(e):
                raise  # let retry wrapper handle it
            return Finding(notes=f"Wayback CDX: no snapshots for '{url}': {e}")

        entities = []
        for i, snap in enumerate(snapshots[:50]):  # Cap at 50 to avoid flooding
            archive_url = snap.archive_url
            timestamp = snap.datetime_timestamp.isoformat() if hasattr(snap, "datetime_timestamp") else str(snap.timestamp)
            status = getattr(snap, "statuscode", "")
            mimetype = getattr(snap, "mimetype", "")

            entities.append(Entity(
                id=f"document:wayback:{i}:{url}",
                entity_type=EntityType.DOCUMENT,
                label=f"Snapshot {i}: {url} ({timestamp})",
                properties={
                    "original_url": url,
                    "archive_url": archive_url,
                    "timestamp": timestamp,
                    "status_code": status,
                    "mimetype": mimetype,
                    "source_system": "wayback",
                },
                sources=[Source(tool=self.name, source_url=archive_url)],
            ))

        return Finding(
            entities=entities,
            notes=f"Wayback CDX: {len(snapshots)} total snapshots for '{url}' (showing {len(entities)})",
        )


def _is_rate_limit(exc: Exception) -> bool:
    """Detect a 429 rate-limit error from waybackpy or urllib."""
    msg = str(exc).lower()
    if "429" in msg or "too many requests" in msg:
        return True
    # waybackpy wraps urllib errors; check nested cause
    cause = getattr(exc, "__cause__", None) or getattr(exc, "__context__", None)
    if cause:
        cause_msg = str(cause).lower()
        if "429" in cause_msg or "too many requests" in cause_msg:
            return True
    return False
