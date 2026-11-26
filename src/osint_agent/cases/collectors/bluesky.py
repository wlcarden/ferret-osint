"""Collect an account's public posts from Bluesky. No key or login is needed.

Bluesky publishes an open interface for reading public posts, and its terms do
not restrict that use. Everything returned is untrusted text.

A post's date is the one Bluesky's server recorded when it received the post.
The date inside the post itself is typed by the author's app and can be set to
anything, including a day before the review period.
"""

import re
import time
from collections.abc import Callable

import httpx

from osint_agent.cases.collectors.guard import is_public_name
from osint_agent.cases.models import CollectedPost, Collection, SourceStatus

FEED_URL = "https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed"
USER_AGENT = "osint-agent-cases/0.1"
PAGE_SIZE = 100
MAX_CURSOR_LENGTH = 512

_DID = re.compile(r"did:(plc:[a-z2-7]{24}|web:[a-z0-9.%-]{4,200})")
_RECORD_KEY = re.compile(r"[A-Za-z0-9._~-]{1,128}")


def _media(embed: object) -> int:
    """How many images or videos a post's own record carries."""
    if not isinstance(embed, dict):
        return 0
    kind = str(embed.get("$type") or "")
    if kind.endswith("recordWithMedia"):
        return _media(embed.get("media"))
    if kind.endswith("images"):
        images = embed.get("images")
        return len(images) if isinstance(images, list) else 0
    return 1 if kind.endswith("video") else 0


def _quoted(view: object) -> str:
    """The post this one quotes, as context. Someone else's words."""
    if not isinstance(view, dict):
        return ""
    record = view.get("record")
    if isinstance(record, dict) and isinstance(record.get("record"), dict):
        record = record["record"]  # A quote that also carries media
    if not isinstance(record, dict):
        return ""
    author = record.get("author") if isinstance(record.get("author"), dict) else {}
    value = record.get("value") if isinstance(record.get("value"), dict) else {}
    handle, text = str(author.get("handle") or ""), str(value.get("text") or "")
    return f"quoting @{handle}: {text}" if handle and text else ""


def _web_address(author: str, uri: str) -> str | None:
    key = uri.rsplit("/", 1)[-1]
    if is_public_name(author) and _RECORD_KEY.fullmatch(key):
        return f"https://bsky.app/profile/{author}/post/{key}"
    return None


def _post(item: object) -> CollectedPost | None:
    """One feed entry as a collected post, or None when it cannot be used."""
    if not isinstance(item, dict) or not isinstance(item.get("post"), dict):
        return None
    post = item["post"]
    record = post.get("record") if isinstance(post.get("record"), dict) else {}
    uri = str(post.get("uri") or "")
    if not uri:
        return None
    author = post.get("author") if isinstance(post.get("author"), dict) else {}
    handle = str(author.get("handle") or "").lower()
    text = str(record.get("text") or "")
    received = post.get("indexedAt") or record.get("createdAt")
    reason = item.get("reason") if isinstance(item.get("reason"), dict) else {}
    if str(reason.get("$type") or "").endswith("#reasonRepost"):
        return CollectedPost(
            native_id=f"repost:{uri}",
            posted_at=reason.get("indexedAt") or received,
            kind="repost",
            text=f"Repost of @{handle}: {text}",
            url=_web_address(handle, uri),
            media=_media(record.get("embed")),
        )
    context = []
    if record.get("reply"):
        thread = item.get("reply") if isinstance(item.get("reply"), dict) else {}
        parent = thread.get("parent") if isinstance(thread.get("parent"), dict) else {}
        parent_author = (
            parent.get("author") if isinstance(parent.get("author"), dict) else {}
        )
        parent_record = (
            parent.get("record") if isinstance(parent.get("record"), dict) else {}
        )
        parent_handle = str(parent_author.get("handle") or "")
        if parent_handle:
            said = str(parent_record.get("text") or "")
            context.append(
                f"replying to @{parent_handle}" + (f": {said}" if said else "")
            )
    quoted = _quoted(post.get("embed"))
    if quoted:
        context.append(quoted)
    return CollectedPost(
        native_id=uri,
        posted_at=received,
        kind="reply" if record.get("reply") else "post",
        text=text,
        context="; ".join(context),
        url=_web_address(handle, uri),
        media=_media(record.get("embed")),
    )


class BlueskyCollector:
    """Reads an account's posts, replies and reposts, newest first."""

    def __init__(
        self,
        client: httpx.Client | None = None,
        max_posts: int = 5000,
        pause: float = 0.3,
        wait: Callable[[float], None] = time.sleep,
    ):
        self._client = client or httpx.Client(
            timeout=20.0, follow_redirects=False, headers={"User-Agent": USER_AGENT}
        )
        self._max_posts = max_posts
        self._pause = pause
        self._wait = wait

    def __call__(self, handle: str, since: str | None = None) -> Collection:
        """Collect posts dated on or after `since` (an ISO date), or all of them."""
        actor = handle.strip().lstrip("@").lower()
        if not (is_public_name(actor) or _DID.fullmatch(actor)):
            return Collection(
                status=SourceStatus.FAILED,
                note="That is not a Bluesky handle. Expected a name such as someone.bsky.social.",
            )
        posts: list[CollectedPost] = []
        cursor: str | None = None
        while True:
            params: dict[str, str | int] = {"actor": actor, "limit": PAGE_SIZE}
            if cursor:
                params["cursor"] = cursor
            try:
                reply = self._client.get(FEED_URL, params=params)
            except httpx.HTTPError as error:
                return self._stopped(
                    posts, f"Bluesky could not be reached ({type(error).__name__})."
                )
            if reply.status_code == 400 and not posts:
                return Collection(
                    status=SourceStatus.FAILED,
                    note="Bluesky has no account with that handle.",
                )
            if reply.status_code != 200:
                return self._stopped(
                    posts, f"Bluesky answered HTTP {reply.status_code}."
                )
            try:
                data = reply.json()
            except ValueError:
                return self._stopped(posts, "Bluesky's reply could not be read.")
            feed = data.get("feed") if isinstance(data, dict) else None
            page = [
                post
                for post in map(_post, feed if isinstance(feed, list) else [])
                if post
            ]
            fresh = [
                p
                for p in page
                if not since or not p.posted_at or p.posted_at[:10] >= since
            ]
            posts += fresh
            cursor = data.get("cursor") if isinstance(data, dict) else None
            if not isinstance(cursor, str) or not 0 < len(cursor) <= MAX_CURSOR_LENGTH:
                break
            # Stop only when a whole page is older than the review period. One old
            # item among newer ones, such as a pinned post, must not end collection.
            if not page or not fresh:
                break
            if len(posts) >= self._max_posts:
                return self._stopped(
                    posts[: self._max_posts], "The post limit was reached."
                )
            self._wait(self._pause)
        return Collection(
            status=SourceStatus.ANSWERED if posts else SourceStatus.EMPTY, posts=posts
        )

    @staticmethod
    def _stopped(posts: list[CollectedPost], why: str) -> Collection:
        """What to return when collection ends early: a failure, or a partial result."""
        if not posts:
            return Collection(status=SourceStatus.FAILED, note=why)
        return Collection(
            posts=posts,
            note=f"{why} Collection stopped after {len(posts)} posts; older ones are missing.",
            complete=False,
        )
