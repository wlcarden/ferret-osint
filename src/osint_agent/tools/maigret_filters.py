"""False positive filtering for Maigret username search results.

Maigret checks 2500+ sites for username claims, but many sites return
false positives — they report any string as "Claimed" regardless of
whether an account actually exists. This module provides filtering to
suppress known false positives.

Detection signals:
  1. Static blocklist — sites empirically confirmed to claim any input
  2. HTTP status — 404/403 responses contradict a "Claimed" status
  3. (Future) Dynamic detection — sites that claim N/N searched usernames

The blocklist was built by searching 6 unrelated usernames and
identifying sites that returned "Claimed" for all 6.
"""

# Sites that return "Claimed" for any arbitrary string input.
# Confirmed by testing 6 unrelated usernames (2026-03-14):
#   jitkadambrosia, josephjacob, wlcarden, williamcarden,
#   leightoncarden, wcarden
# All 9 sites below claimed all 6 usernames.
BLOCKLISTED_SITES: frozenset[str] = frozenset({
    "AdultFriendFinder",
    "authorSTREAM",
    "Bibsonomy",
    "Blu-ray",
    "getmyuni",
    "hashnode",
    "Kaggle",
    "TechPowerUp",
    "Tom's guide",
})

# HTTP statuses that contradict a "Claimed" result.
# If the site returned one of these, the account doesn't exist.
_CONTRADICTING_HTTP_STATUSES: frozenset[int] = frozenset({
    403, 404, 410, 429, 500, 502, 503,
})


def is_false_positive(
    site_name: str,
    http_status: int | None = None,
) -> str | None:
    """Check whether a Maigret "Claimed" result is a known false positive.

    Returns a reason string if the result should be filtered, or None
    if it appears legitimate.
    """
    if site_name in BLOCKLISTED_SITES:
        return "blocklisted site (claims any username)"

    if http_status and http_status in _CONTRADICTING_HTTP_STATUSES:
        return f"HTTP {http_status} contradicts claimed status"

    return None
