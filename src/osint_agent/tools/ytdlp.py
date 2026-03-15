"""yt-dlp adapter — YouTube/video platform metadata extraction.

Extracts channel info, video metadata, comments, and captions from
YouTube and 1000+ other sites without downloading video content.
No API key or authentication required.
"""

import json
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

_SOURCE = lambda: Source(tool="yt-dlp")


class YtDlpAdapter(ToolAdapter):
    """Extract metadata from YouTube channels, videos, and playlists."""

    name = "yt-dlp"

    def is_available(self) -> bool:
        try:
            import yt_dlp  # noqa: F401
            return True
        except ImportError:
            return False

    async def run(self, url: str, **kwargs) -> Finding:
        """Extract metadata from a video/channel URL.

        Args:
            url: YouTube (or other supported site) URL.
        """
        import asyncio
        import yt_dlp

        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "extract_flat": kwargs.get("flat", False),
            "ignoreerrors": True,
        }

        def _extract():
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)

        loop = asyncio.get_event_loop()
        try:
            info = await loop.run_in_executor(None, _extract)
        except Exception as exc:
            logger.warning("yt-dlp failed for %s: %s", url, exc)
            return Finding(notes=f"yt-dlp error: {exc}")

        if not info:
            return Finding(notes=f"yt-dlp returned no data for {url}")

        # Detect whether this is a channel/playlist or single video.
        entries = info.get("entries")
        if entries is not None:
            return self._build_channel_finding(info, url)
        return self._build_video_finding(info, url)

    def _build_video_finding(self, info: dict, url: str) -> Finding:
        entities: list[Entity] = []
        relationships: list[Relationship] = []

        vid_id = info.get("id", url)
        channel_id = info.get("channel_id") or info.get("uploader_id", "")
        channel_name = info.get("channel") or info.get("uploader", "")

        # Video entity (as document).
        props = {}
        for key in (
            "title", "description", "upload_date", "duration",
            "view_count", "like_count", "comment_count",
            "categories", "tags", "webpage_url", "thumbnail",
        ):
            val = info.get(key)
            if val not in (None, "", [], {}):
                props[key] = val

        video = Entity(
            id=f"document:video:{vid_id}",
            entity_type=EntityType.DOCUMENT,
            label=info.get("title", url)[:120],
            properties=props,
            sources=[_SOURCE()],
        )
        entities.append(video)

        # Channel entity (as account).
        if channel_name:
            channel = Entity(
                id=f"account:youtube:{channel_id or channel_name}",
                entity_type=EntityType.ACCOUNT,
                label=channel_name,
                properties={
                    k: v for k, v in {
                        "platform": "YouTube",
                        "url": info.get("channel_url"),
                        "subscriber_count": info.get("channel_follower_count"),
                    }.items() if v is not None
                },
                sources=[_SOURCE()],
            )
            entities.append(channel)
            relationships.append(Relationship(
                source_id=channel.id,
                target_id=video.id,
                relation_type=RelationType.OWNS,
                sources=[_SOURCE()],
            ))

        notes_parts = [f"Video: {info.get('title', '?')}"]
        if info.get("view_count"):
            notes_parts.append(f"{info['view_count']:,} views")
        if channel_name:
            notes_parts.append(f"by {channel_name}")

        return Finding(
            entities=entities,
            relationships=relationships,
            notes=" | ".join(notes_parts),
        )

    def _build_channel_finding(self, info: dict, url: str) -> Finding:
        entities: list[Entity] = []
        relationships: list[Relationship] = []

        channel_id = info.get("id", "")
        channel_name = info.get("channel") or info.get("title", url)

        channel = Entity(
            id=f"account:youtube:{channel_id or channel_name}",
            entity_type=EntityType.ACCOUNT,
            label=channel_name,
            properties={
                k: v for k, v in {
                    "platform": "YouTube",
                    "url": info.get("webpage_url") or url,
                    "description": (info.get("description") or "")[:500] or None,
                    "subscriber_count": info.get("channel_follower_count"),
                }.items() if v is not None
            },
            sources=[_SOURCE()],
        )
        entities.append(channel)

        # Include first N video entries as documents.
        entries = info.get("entries") or []
        video_count = 0
        for entry in entries[:20]:
            if not entry:
                continue
            vid_id = entry.get("id", "")
            title = entry.get("title", "")
            if not vid_id or not title:
                continue
            video = Entity(
                id=f"document:video:{vid_id}",
                entity_type=EntityType.DOCUMENT,
                label=title[:120],
                properties={
                    k: v for k, v in {
                        "title": title,
                        "url": entry.get("url") or entry.get("webpage_url"),
                        "duration": entry.get("duration"),
                        "view_count": entry.get("view_count"),
                    }.items() if v is not None
                },
                sources=[_SOURCE()],
            )
            entities.append(video)
            relationships.append(Relationship(
                source_id=channel.id,
                target_id=video.id,
                relation_type=RelationType.OWNS,
                sources=[_SOURCE()],
            ))
            video_count += 1

        return Finding(
            entities=entities,
            relationships=relationships,
            notes=f"Channel: {channel_name} | {video_count} videos extracted",
        )
