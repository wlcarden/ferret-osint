"""Collect an account's public posts from a Mastodon-style server. No key needed.

The handle names the server, so the server is checked before anything is
fetched from it (see guard.py) and redirects are never followed. Everything
returned is untrusted text, and an identifier from the server is checked whole
before it goes into an address.
"""

import html
import re
import time
from collections.abc import Callable

import httpx

from osint_agent.cases.collectors import guard
from osint_agent.cases.models import CollectedPost, Collection, SourceStatus

USER_AGENT = "osint-agent-cases/0.1"
PAGE_SIZE = 40

_HANDLE = re.compile(r"(?P<user>[A-Za-z0-9_]{1,64})@(?P<host>[A-Za-z0-9.-]{4,253})")
_SERVER_ID = re.compile(r"[A-Za-z0-9]{1,64}")


class _CollectionError(Exception):
    """Collection cannot go on. The message says why, in plain words."""


def _plain(status: dict) -> str:
    """A post's words without markup, behind its content warning if it has one."""
    marked = re.sub(r"(?i)<br\s*/?>|</p>\s*<p>", "\n", str(status.get("content") or ""))
    text = html.unescape(re.sub(r"<[^>]+>", "", marked)).strip()
    warning = str(status.get("spoiler_text") or "").strip()
    return f"{warning}: {text}" if warning else text


def _attachments(status: dict) -> int:
    media = status.get("media_attachments")
    return len(media) if isinstance(media, list) else 0


def _post(status: object, host: str) -> CollectedPost | None:
    """One status as a collected post, or None when it cannot be used."""
    if not isinstance(status, dict):
        return None
    status_id = str(status.get("id") or "")
    if not _SERVER_ID.fullmatch(status_id):
        return None
    shared = status.get("reblog")
    if isinstance(shared, dict):
        account = (
            shared.get("account") if isinstance(shared.get("account"), dict) else {}
        )
        return CollectedPost(
            native_id=f"{host}:{status_id}",
            posted_at=status.get("created_at"),
            kind="repost",
            text=f"Repost of @{account.get('acct') or 'unknown'}: {_plain(shared)}",
            url=shared.get("url") or status.get("url"),
            media=_attachments(shared),
        )
    return CollectedPost(
        native_id=f"{host}:{status_id}",
        posted_at=status.get("created_at"),
        kind="reply" if status.get("in_reply_to_id") else "post",
        text=_plain(status),
        url=status.get("url"),
        media=_attachments(status),
    )


class MastodonCollector:
    """Reads an account's posts, replies and reposts, newest first."""

    def __init__(
        self,
        client: httpx.Client | None = None,
        max_posts: int = 5000,
        pause: float = 0.3,
        wait: Callable[[float], None] = time.sleep,
        resolver: guard.Resolver = guard.resolve,
    ):
        self._client = client or httpx.Client(
            timeout=20.0, follow_redirects=False, headers={"User-Agent": USER_AGENT}
        )
        self._max_posts = max_posts
        self._pause = pause
        self._wait = wait
        self._resolver = resolver

    def __call__(self, handle: str, since: str | None = None) -> Collection:
        """Collect posts dated on or after `since` (an ISO date), or all of them."""
        match = _HANDLE.fullmatch(handle.strip().lstrip("@"))
        if not match:
            return Collection(
                status=SourceStatus.FAILED,
                note="That is not a Mastodon handle. Expected name@server.",
            )
        user, host = match.group("user"), match.group("host").lower()
        why = guard.refusal(host, self._resolver)
        if why:
            return Collection(status=SourceStatus.FAILED, note=f"{host}: {why}.")
        posts: list[CollectedPost] = []
        try:
            account = self._get(
                f"https://{host}/api/v1/accounts/lookup", {"acct": user}
            )
            account_id = (
                str(account.get("id") or "") if isinstance(account, dict) else ""
            )
            if not _SERVER_ID.fullmatch(account_id):
                raise _CollectionError(
                    f"{host} gave no usable account for that handle."
                )
            oldest: str | None = None
            while True:
                params: dict[str, str | int] = {"limit": PAGE_SIZE}
                if oldest:
                    params["max_id"] = oldest
                listed = self._get(
                    f"https://{host}/api/v1/accounts/{account_id}/statuses", params
                )
                statuses = listed if isinstance(listed, list) else []
                page = [post for post in (_post(s, host) for s in statuses) if post]
                fresh = [
                    p
                    for p in page
                    if not since or not p.posted_at or p.posted_at[:10] >= since
                ]
                posts += fresh
                last = statuses[-1] if statuses else None
                last_id = str(last.get("id") or "") if isinstance(last, dict) else ""
                # Stop only when a whole page is older than the review period. One old
                # item among newer ones, such as a pinned post, must not end collection.
                if not page or not fresh or not _SERVER_ID.fullmatch(last_id):
                    break
                if last_id == oldest:
                    break  # The server ignored the paging marker
                oldest = last_id
                if len(posts) >= self._max_posts:
                    raise _CollectionError("The post limit was reached.")
                self._wait(self._pause)
        except _CollectionError as stop:
            if not posts:
                return Collection(status=SourceStatus.FAILED, note=str(stop))
            kept = posts[: self._max_posts]
            return Collection(
                posts=kept,
                note=f"{stop} Collection stopped after {len(kept)} posts; older ones are missing.",
                complete=False,
            )
        return Collection(
            status=SourceStatus.ANSWERED if posts else SourceStatus.EMPTY, posts=posts
        )

    def _get(self, url: str, params: dict) -> object:
        host = httpx.URL(url).host
        try:
            reply = self._client.get(url, params=params, follow_redirects=False)
        except (httpx.HTTPError, httpx.InvalidURL) as error:
            raise _CollectionError(
                f"{host} could not be reached ({type(error).__name__})."
            ) from error
        if reply.status_code == 404:
            raise _CollectionError(f"{host} has no account with that handle.")
        if 300 <= reply.status_code < 400:
            raise _CollectionError(f"{host} sent a redirect, which is not followed.")
        if reply.status_code != 200:
            raise _CollectionError(f"{host} answered HTTP {reply.status_code}.")
        try:
            return reply.json()
        except ValueError as error:
            raise _CollectionError(f"{host}'s reply could not be read.") from error
