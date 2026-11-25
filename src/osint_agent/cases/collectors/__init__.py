"""Collectors: programs that fetch the posts of an account the subject listed.

Only platforms that offer an open way to read public posts have one. A
collector is called with a handle and an optional earliest date, and returns a
Collection. Collectors are keyed by platform name, which is what a listed
handle carries before its colon: "bluesky:someone.bsky.social".
"""

from collections.abc import Callable

from osint_agent.cases.collectors.bluesky import BlueskyCollector
from osint_agent.cases.collectors.mastodon import MastodonCollector
from osint_agent.cases.models import Collection

PostCollector = Callable[[str, str | None], Collection]


def default_collectors() -> dict[str, PostCollector]:
    """One collector for each platform a program is allowed to read."""
    return {"bluesky": BlueskyCollector(), "mastodon": MastodonCollector()}


__all__ = [
    "BlueskyCollector",
    "MastodonCollector",
    "PostCollector",
    "default_collectors",
]
