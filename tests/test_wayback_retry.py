"""Tests for Wayback Machine adapter retry/backoff logic."""

from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

import pytest

from osint_agent.tools.wayback import WaybackAdapter, _is_rate_limit


@pytest.fixture
def adapter():
    return WaybackAdapter()


# ------------------------------------------------------------------
# _is_rate_limit detection
# ------------------------------------------------------------------

def test_is_rate_limit_429_in_message():
    """should detect 429 in exception message"""
    exc = Exception("HTTP Error 429: Too Many Requests")
    assert _is_rate_limit(exc) is True


def test_is_rate_limit_too_many_requests():
    """should detect 'too many requests' in exception message"""
    exc = Exception("too many requests")
    assert _is_rate_limit(exc) is True


def test_is_rate_limit_nested_cause():
    """should detect 429 in nested __cause__"""
    inner = Exception("429")
    outer = Exception("request failed")
    outer.__cause__ = inner
    assert _is_rate_limit(outer) is True


def test_is_rate_limit_nested_context():
    """should detect 429 in nested __context__"""
    inner = Exception("HTTP 429")
    outer = Exception("wrapped error")
    outer.__context__ = inner
    assert _is_rate_limit(outer) is True


def test_is_rate_limit_false_for_404():
    """should return False for non-429 errors"""
    exc = Exception("HTTP Error 404: Not Found")
    assert _is_rate_limit(exc) is False


def test_is_rate_limit_false_for_generic():
    """should return False for generic exceptions"""
    exc = Exception("something went wrong")
    assert _is_rate_limit(exc) is False


# ------------------------------------------------------------------
# Retry logic for snapshots
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_snapshot_retries_on_429(adapter):
    """should retry on 429 and succeed on second attempt"""
    rate_limit_exc = Exception("HTTP Error 429: Too Many Requests")
    mock_snapshot = MagicMock()
    mock_snapshot.archive_url = "https://web.archive.org/web/test"
    mock_snapshot.timestamp = MagicMock(return_value="20240101")

    call_count = 0

    def mock_newest():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise rate_limit_exc
        return mock_snapshot

    with patch("waybackpy.WaybackMachineAvailabilityAPI") as mock_api:
        mock_instance = MagicMock()
        mock_instance.newest = mock_newest
        mock_api.return_value = mock_instance

        with patch("osint_agent.tools.wayback.asyncio.sleep"):
            finding = await adapter.run(
                url="https://example.com", mode="newest",
            )

    assert len(finding.entities) == 1
    assert call_count == 2


@pytest.mark.asyncio
async def test_snapshot_returns_finding_on_non_429_error(adapter):
    """should return error Finding immediately for non-429 errors"""
    not_found_exc = Exception("No archive found")

    with patch("waybackpy.WaybackMachineAvailabilityAPI") as mock_api:
        mock_instance = MagicMock()
        mock_instance.newest = MagicMock(side_effect=not_found_exc)
        mock_api.return_value = mock_instance

        finding = await adapter.run(
            url="https://example.com", mode="newest",
        )

    assert len(finding.entities) == 0
    assert "no newest snapshot" in finding.notes


# ------------------------------------------------------------------
# Retry logic for CDX
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cdx_retries_on_429(adapter):
    """should retry CDX on 429 and succeed on second attempt"""
    rate_limit_exc = Exception("429 Too Many Requests")

    mock_snap = MagicMock()
    mock_snap.archive_url = "https://web.archive.org/web/snap1"
    mock_snap.datetime_timestamp = MagicMock()
    mock_snap.datetime_timestamp.isoformat = MagicMock(
        return_value="2024-01-01T00:00:00",
    )
    mock_snap.statuscode = "200"
    mock_snap.mimetype = "text/html"

    call_count = 0

    def mock_snapshots():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise rate_limit_exc
        return [mock_snap]

    with patch("waybackpy.WaybackMachineCDXServerAPI") as mock_cdx:
        mock_instance = MagicMock()
        mock_instance.snapshots = mock_snapshots
        mock_cdx.return_value = mock_instance

        with patch("osint_agent.tools.wayback.asyncio.sleep"):
            finding = await adapter.run(
                url="https://example.com", mode="snapshots",
            )

    assert len(finding.entities) == 1
    assert call_count == 2


@pytest.mark.asyncio
async def test_cdx_returns_finding_on_non_429_error(adapter):
    """should return error Finding immediately for non-429 CDX errors"""
    generic_exc = Exception("connection timeout")

    with patch("waybackpy.WaybackMachineCDXServerAPI") as mock_cdx:
        mock_instance = MagicMock()
        mock_instance.snapshots = MagicMock(side_effect=generic_exc)
        mock_cdx.return_value = mock_instance

        finding = await adapter.run(
            url="https://example.com", mode="snapshots",
        )

    assert len(finding.entities) == 0
    assert "no snapshots" in finding.notes
