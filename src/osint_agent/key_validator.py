"""API key validation — lightweight checks at startup.

Tests that configured API keys are actually valid by making minimal
requests to each service. Prevents discovering broken keys mid-investigation.

Each validator makes the cheapest possible API call (often a metadata/
whoami endpoint) and reports success/failure.
"""

import asyncio
import os

import httpx


async def _check_courtlistener(client: httpx.AsyncClient) -> tuple[str, bool, str]:
    key = os.environ.get("COURTLISTENER_API_KEY", "")
    if not key:
        return ("CourtListener", False, "COURTLISTENER_API_KEY not set")
    try:
        resp = await client.get(
            "https://www.courtlistener.com/api/rest/v4/search/",
            params={"q": "test", "type": "o"},
            headers={"Authorization": f"Token {key}"},
        )
        if resp.status_code == 401:
            return ("CourtListener", False, "invalid API key (401)")
        if resp.status_code == 200:
            return ("CourtListener", True, "valid")
        return ("CourtListener", True, f"HTTP {resp.status_code} (key may be valid)")
    except httpx.HTTPError as e:
        return ("CourtListener", False, f"connection error: {e}")


async def _check_openfec(client: httpx.AsyncClient) -> tuple[str, bool, str]:
    key = os.environ.get("OPENFEC_API_KEY", "")
    if not key:
        return ("OpenFEC", False, "OPENFEC_API_KEY not set")
    try:
        resp = await client.get(
            "https://api.open.fec.gov/v1/candidates/",
            params={"api_key": key, "per_page": 1},
        )
        if resp.status_code == 403:
            return ("OpenFEC", False, "invalid API key (403)")
        if resp.status_code == 200:
            return ("OpenFEC", True, "valid")
        return ("OpenFEC", True, f"HTTP {resp.status_code}")
    except httpx.HTTPError as e:
        return ("OpenFEC", False, f"connection error: {e}")


async def _check_congress(client: httpx.AsyncClient) -> tuple[str, bool, str]:
    key = os.environ.get("CONGRESS_API_KEY", "")
    if not key:
        return ("Congress.gov", False, "CONGRESS_API_KEY not set")
    try:
        resp = await client.get(
            "https://api.congress.gov/v3/bill",
            params={"api_key": key, "format": "json", "limit": 1},
        )
        if resp.status_code in (401, 403):
            return ("Congress.gov", False, f"invalid API key ({resp.status_code})")
        if resp.status_code == 200:
            return ("Congress.gov", True, "valid")
        return ("Congress.gov", True, f"HTTP {resp.status_code}")
    except httpx.HTTPError as e:
        return ("Congress.gov", False, f"connection error: {e}")


async def _check_sec_edgar(client: httpx.AsyncClient) -> tuple[str, bool, str]:
    ua = os.environ.get("SEC_EDGAR_USER_AGENT", "")
    if not ua:
        return ("SEC EDGAR", False, "SEC_EDGAR_USER_AGENT not set")
    try:
        resp = await client.get(
            "https://efts.sec.gov/LATEST/search-index?q=test&dateRange=custom&startdt=2024-01-01&enddt=2024-01-02",
            headers={"User-Agent": ua},
        )
        if resp.status_code == 403:
            return ("SEC EDGAR", False, "User-Agent rejected (403)")
        return ("SEC EDGAR", True, "valid")
    except httpx.HTTPError as e:
        return ("SEC EDGAR", False, f"connection error: {e}")


async def _check_shodan(client: httpx.AsyncClient) -> tuple[str, bool, str]:
    key = os.environ.get("SHODAN_API_KEY", "")
    if not key:
        return ("Shodan", False, "SHODAN_API_KEY not set (optional)")
    try:
        resp = await client.get(
            "https://api.shodan.io/api-info",
            params={"key": key},
        )
        if resp.status_code == 401:
            return ("Shodan", False, "invalid API key (401)")
        if resp.status_code == 200:
            data = resp.json()
            credits = data.get("query_credits", "?")
            return ("Shodan", True, f"valid ({credits} query credits)")
        return ("Shodan", True, f"HTTP {resp.status_code}")
    except httpx.HTTPError as e:
        return ("Shodan", False, f"connection error: {e}")


async def _check_virustotal(client: httpx.AsyncClient) -> tuple[str, bool, str]:
    key = os.environ.get("VIRUSTOTAL_API_KEY", "")
    if not key:
        return ("VirusTotal", False, "VIRUSTOTAL_API_KEY not set (optional)")
    try:
        resp = await client.get(
            "https://www.virustotal.com/api/v3/users/current",
            headers={"x-apikey": key},
        )
        if resp.status_code == 401:
            return ("VirusTotal", False, "invalid API key (401)")
        if resp.status_code == 200:
            return ("VirusTotal", True, "valid")
        return ("VirusTotal", True, f"HTTP {resp.status_code}")
    except httpx.HTTPError as e:
        return ("VirusTotal", False, f"connection error: {e}")


# All validators — only check keys that are configured
_ALL_VALIDATORS = [
    _check_courtlistener,
    _check_openfec,
    _check_congress,
    _check_sec_edgar,
    _check_shodan,
    _check_virustotal,
]


async def validate_api_keys(
    only_configured: bool = True,
) -> list[tuple[str, bool, str]]:
    """Validate all configured API keys concurrently.

    Args:
        only_configured: If True, skip services with no key set.

    Returns:
        List of (service_name, is_valid, message) tuples.
    """
    async with httpx.AsyncClient(timeout=15) as client:
        results = await asyncio.gather(
            *[v(client) for v in _ALL_VALIDATORS],
            return_exceptions=True,
        )

    validated = []
    for result in results:
        if isinstance(result, Exception):
            validated.append(("Unknown", False, str(result)))
        else:
            name, valid, msg = result
            if only_configured and "not set" in msg:
                continue
            validated.append((name, valid, msg))

    return validated


def print_validation_report(
    results: list[tuple[str, bool, str]],
) -> None:
    """Print a formatted API key validation report."""
    from osint_agent import console

    console.validation_report(results)
