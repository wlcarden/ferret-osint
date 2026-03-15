"""Reddit tool adapter — public profile and post history analysis.

Uses Reddit's public JSON API (no auth required) to pull:
  - Account metadata (age, karma)
  - Post history (titles, subreddits, timestamps, scores)
  - Comment history (subreddits, timestamps, content snippets)

Generates intelligence from the raw data:
  - Subreddit clustering (shows interests, affiliations, communities)
  - Temporal analysis (posting patterns → timezone inference)
  - Content extraction (location mentions, self-identified info)

Rate limits: ~10 QPM unauthenticated. We throttle to ~3 RPM to be safe.
User-Agent must be descriptive or Reddit returns 429.
"""

import asyncio
import logging
import re
from collections import Counter
from datetime import UTC, datetime

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

_USER_AGENT = "Linux:OSINTAgent:v0.1 (research tool)"
_BASE_URL = "https://www.reddit.com"
_REQUEST_DELAY = 2.0  # seconds between requests (safe for 10 QPM)
_MAX_PAGES = 4  # max pagination depth (25 items/page = 100 items)
_MAX_RETRIES = 3
_INITIAL_BACKOFF = 5  # seconds

# Subreddit categories for clustering analysis
_POLITICAL_SUBREDDITS = {
    # Right-wing / far-right
    "conservative", "republican", "the_donald", "askthe_donald",
    "conservativesonly", "walkaway", "louderwithcrowder", "benshapiro",
    "tucker_carlson", "timpool", "conspiracy", "anarcho_capitalism",
    "shitstatistssay", "goldandblack", "progun", "firearms",
    "socialjusticeinaction", "tumblrinaction", "kotakuinaction",
    "mensrights", "jordanpeterson", "intellectualdarkweb",
    # Left-wing
    "politics", "political_revolution", "sandersforpresident",
    "latestagecapitalism", "antiwork", "socialism", "anarchism",
    "breadtube", "communism", "completeanarchy", "chapotraphouse",
    # Neutral / news
    "news", "worldnews", "neutralpolitics", "politicaldiscussion",
    "moderatepolitics",
}

# Patterns that might indicate location self-disclosure
_LOCATION_PATTERNS = [
    r"(?:I(?:'m| am) (?:from|in|based in|living in))\s+"
    r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*(?:,\s*[A-Z]{2})?)",
    r"(?:here in|moved to|live in|born in)\s+"
    r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*(?:,\s*[A-Z]{2})?)",
]


class RedditAdapter(ToolAdapter):
    """Pulls Reddit user profile, post history, and comment analysis.

    No external dependencies beyond httpx (already in the project).
    Uses Reddit's public .json API endpoints.
    """

    name = "reddit"

    def __init__(self, timeout: int = 30):
        self.timeout = timeout

    def is_available(self) -> bool:
        return True  # Only needs httpx, which is a core dependency

    async def run(self, username: str, max_pages: int = _MAX_PAGES) -> Finding:
        """Fetch and analyze a Reddit user's public profile.

        Args:
            username: Reddit username (without u/ prefix).
            max_pages: Max pages to fetch for posts/comments (25 items/page).
        """
        username = username.lstrip("u/").lstrip("/")

        async with httpx.AsyncClient(
            timeout=self.timeout,
            headers={"User-Agent": _USER_AGENT},
            follow_redirects=True,
        ) as client:
            # Fetch profile, posts, and comments
            about = await self._fetch_about(client, username)
            if about is None:
                return Finding(
                    notes=f"Reddit: user '{username}' not found or suspended",
                )

            await asyncio.sleep(_REQUEST_DELAY)
            posts = await self._fetch_listing(
                client, username, "submitted", max_pages,
            )

            await asyncio.sleep(_REQUEST_DELAY)
            comments = await self._fetch_listing(
                client, username, "comments", max_pages,
            )

        return self._build_finding(username, about, posts, comments)

    async def _fetch_about(
        self, client: httpx.AsyncClient, username: str,
    ) -> dict | None:
        """Fetch user about/profile data."""
        url = f"{_BASE_URL}/user/{username}/about.json"
        for attempt in range(_MAX_RETRIES):
            try:
                resp = await client.get(url)
                if resp.status_code == 404:
                    return None
                if resp.status_code == 403:
                    # Suspended or private
                    return None
                if resp.status_code == 429:
                    wait = _INITIAL_BACKOFF * (2 ** attempt)
                    logger.info("Reddit 429, retry in %ds", wait)
                    await asyncio.sleep(wait)
                    continue
                resp.raise_for_status()
                data = resp.json()
                return data.get("data", {})
            except httpx.HTTPStatusError:
                if attempt < _MAX_RETRIES - 1:
                    await asyncio.sleep(_INITIAL_BACKOFF)
                    continue
                return None
        return None

    async def _fetch_listing(
        self,
        client: httpx.AsyncClient,
        username: str,
        listing_type: str,
        max_pages: int,
    ) -> list[dict]:
        """Fetch paginated listing (posts or comments)."""
        items: list[dict] = []
        after: str | None = None

        for page in range(max_pages):
            url = f"{_BASE_URL}/user/{username}/{listing_type}.json"
            params = {"limit": 25, "raw_json": 1}
            if after:
                params["after"] = after

            for attempt in range(_MAX_RETRIES):
                try:
                    resp = await client.get(url, params=params)
                    if resp.status_code == 429:
                        wait = _INITIAL_BACKOFF * (2 ** attempt)
                        logger.info("Reddit 429, retry in %ds", wait)
                        await asyncio.sleep(wait)
                        continue
                    resp.raise_for_status()
                    break
                except httpx.HTTPStatusError:
                    if attempt < _MAX_RETRIES - 1:
                        await asyncio.sleep(_INITIAL_BACKOFF)
                        continue
                    return items
            else:
                # All retries exhausted
                return items

            data = resp.json().get("data", {})
            children = data.get("children", [])
            if not children:
                break

            for child in children:
                items.append(child.get("data", {}))

            after = data.get("after")
            if not after:
                break

            if page < max_pages - 1:
                await asyncio.sleep(_REQUEST_DELAY)

        return items

    def _build_finding(
        self,
        username: str,
        about: dict,
        posts: list[dict],
        comments: list[dict],
    ) -> Finding:
        """Build Finding from raw API data with analysis."""
        entities: list[Entity] = []
        relationships: list[Relationship] = []

        profile_url = f"https://reddit.com/user/{username}"
        source = Source(tool=self.name, source_url=profile_url)

        # Account entity
        created_utc = about.get("created_utc", 0)
        created_date = (
            datetime.fromtimestamp(created_utc, tz=UTC).isoformat()
            if created_utc
            else ""
        )

        # Analyze post/comment data
        subreddit_analysis = self._analyze_subreddits(posts, comments)
        temporal_analysis = self._analyze_temporal(posts, comments)
        location_mentions = self._extract_locations(posts, comments)

        account_props = {
            "platform": "Reddit",
            "username": username,
            "url": profile_url,
            "created": created_date,
            "link_karma": about.get("link_karma", 0),
            "comment_karma": about.get("comment_karma", 0),
            "total_karma": about.get("total_karma", 0),
            "is_gold": about.get("is_gold", False),
            "is_mod": about.get("is_mod", False),
            "verified": about.get("verified", False),
            "post_count": len(posts),
            "comment_count": len(comments),
        }

        # Add analysis to properties
        if subreddit_analysis["top_subreddits"]:
            account_props["top_subreddits"] = ", ".join(
                f"{sub}({count})"
                for sub, count in subreddit_analysis["top_subreddits"][:15]
            )
        if subreddit_analysis["political_subreddits"]:
            account_props["political_subreddits"] = ", ".join(
                f"{sub}({count})"
                for sub, count in subreddit_analysis["political_subreddits"]
            )
        if temporal_analysis["peak_hours"]:
            account_props["peak_posting_hours_utc"] = ", ".join(
                str(h) for h in temporal_analysis["peak_hours"]
            )
        if temporal_analysis["estimated_timezone"]:
            account_props["estimated_timezone"] = temporal_analysis["estimated_timezone"]
        if location_mentions:
            account_props["location_mentions"] = ", ".join(location_mentions[:10])

        account_entity = Entity(
            id=f"account:reddit:{username}",
            entity_type=EntityType.ACCOUNT,
            label=f"{username} on Reddit",
            properties=account_props,
            sources=[source],
        )
        entities.append(account_entity)

        # Create entities for notable subreddits (active communities)
        for sub_name, count in subreddit_analysis["top_subreddits"][:10]:
            sub_id = f"organization:subreddit:{sub_name}"
            entities.append(Entity(
                id=sub_id,
                entity_type=EntityType.ORGANIZATION,
                label=f"r/{sub_name}",
                properties={
                    "platform": "Reddit",
                    "url": f"https://reddit.com/r/{sub_name}",
                    "type": "subreddit",
                },
                sources=[source],
            ))
            relationships.append(Relationship(
                source_id=account_entity.id,
                target_id=sub_id,
                relation_type=RelationType.AFFILIATED_WITH,
                properties={
                    "activity_count": count,
                    "type": "subreddit_participation",
                },
                sources=[source],
            ))

        # Build notes summary
        notes_lines = [
            f"Reddit profile: u/{username}",
            f"  Account created: {created_date}",
            f"  Karma: {about.get('total_karma', 0):,} "
            f"(link: {about.get('link_karma', 0):,}, "
            f"comment: {about.get('comment_karma', 0):,})",
            f"  Posts fetched: {len(posts)}, Comments fetched: {len(comments)}",
        ]

        if subreddit_analysis["top_subreddits"]:
            notes_lines.append("  Top subreddits: " + ", ".join(
                f"r/{s}({c})" for s, c in subreddit_analysis["top_subreddits"][:10]
            ))

        if subreddit_analysis["political_subreddits"]:
            notes_lines.append("  Political subs: " + ", ".join(
                f"r/{s}({c})" for s, c in subreddit_analysis["political_subreddits"]
            ))

        if temporal_analysis["estimated_timezone"]:
            notes_lines.append(
                f"  Estimated timezone: {temporal_analysis['estimated_timezone']} "
                f"(peak hours UTC: {temporal_analysis['peak_hours']})",
            )

        if location_mentions:
            notes_lines.append(
                f"  Location mentions: {', '.join(location_mentions[:5])}",
            )

        return Finding(
            entities=entities,
            relationships=relationships,
            notes="\n".join(notes_lines),
        )

    def _analyze_subreddits(
        self, posts: list[dict], comments: list[dict],
    ) -> dict:
        """Cluster activity by subreddit."""
        counter: Counter = Counter()

        for post in posts:
            sub = post.get("subreddit", "").lower()
            if sub:
                counter[sub] += 1

        for comment in comments:
            sub = comment.get("subreddit", "").lower()
            if sub:
                counter[sub] += 1

        top = counter.most_common(30)
        political = [
            (sub, count) for sub, count in top
            if sub in _POLITICAL_SUBREDDITS
        ]

        return {
            "top_subreddits": top,
            "political_subreddits": political,
            "unique_subreddits": len(counter),
        }

    def _analyze_temporal(
        self, posts: list[dict], comments: list[dict],
    ) -> dict:
        """Analyze posting times for timezone inference."""
        hours_utc: list[int] = []

        for item in posts + comments:
            created = item.get("created_utc", 0)
            if created:
                dt = datetime.fromtimestamp(created, tz=UTC)
                hours_utc.append(dt.hour)

        if not hours_utc:
            return {"peak_hours": [], "estimated_timezone": ""}

        hour_counts = Counter(hours_utc)
        # Top 5 most active hours
        peak_hours = [h for h, _ in hour_counts.most_common(5)]
        peak_hours.sort()

        # Estimate timezone: assume peak activity is roughly 9am-11pm local
        # Average peak hour in UTC, assume it maps to ~6pm local (18:00)
        avg_peak = sum(peak_hours[:3]) / min(len(peak_hours), 3)
        offset = int(round(18 - avg_peak))
        if offset > 12:
            offset -= 24
        if offset < -12:
            offset += 24

        tz_str = f"UTC{offset:+d}" if offset != 0 else "UTC"

        return {
            "peak_hours": peak_hours,
            "estimated_timezone": tz_str,
        }

    def _extract_locations(
        self, posts: list[dict], comments: list[dict],
    ) -> list[str]:
        """Extract self-disclosed locations from post/comment text."""
        locations: list[str] = []
        seen: set[str] = set()

        texts = []
        for post in posts:
            if post.get("selftext"):
                texts.append(post["selftext"])
            if post.get("title"):
                texts.append(post["title"])
        for comment in comments:
            if comment.get("body"):
                texts.append(comment["body"])

        for text in texts[:200]:  # Cap to avoid excessive processing
            for pattern in _LOCATION_PATTERNS:
                for match in re.finditer(pattern, text):
                    loc = match.group(1).strip()
                    loc_lower = loc.lower()
                    if loc_lower not in seen and len(loc) > 2:
                        seen.add(loc_lower)
                        locations.append(loc)

        return locations
