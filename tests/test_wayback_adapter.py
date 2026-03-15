"""Tests for the Wayback Machine adapter."""

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from osint_agent.models import EntityType
from osint_agent.tools.wayback import WaybackAdapter, _is_rate_limit


@pytest.fixture
def adapter():
    return WaybackAdapter()


@pytest.fixture
def mock_waybackpy():
    """Inject a mock waybackpy into sys.modules for the adapter's local imports."""
    mock_mod = MagicMock()
    with patch.dict(sys.modules, {"waybackpy": mock_mod}):
        yield mock_mod


# ------------------------------------------------------------------
# Availability
# ------------------------------------------------------------------

def test_is_available_when_installed(mock_waybackpy):
    assert WaybackAdapter().is_available() is True


def test_is_not_available_when_missing():
    with patch.dict(sys.modules, {"waybackpy": None}):
        # Force ImportError on import
        adapter = WaybackAdapter()
        result = adapter.is_available()
    # waybackpy=None causes ImportError on `import waybackpy`
    assert result is False


def test_adapter_name():
    assert WaybackAdapter().name == "wayback"


# ------------------------------------------------------------------
# Rate limit detection
# ------------------------------------------------------------------

def test_is_rate_limit_429_in_message():
    assert _is_rate_limit(Exception("HTTP Error 429: Too Many Requests")) is True


def test_is_rate_limit_too_many_requests():
    assert _is_rate_limit(Exception("too many requests")) is True


def test_is_rate_limit_nested_cause():
    inner = Exception("429 rate limit")
    outer = Exception("failed")
    outer.__cause__ = inner
    assert _is_rate_limit(outer) is True


def test_is_rate_limit_false():
    assert _is_rate_limit(Exception("404 Not Found")) is False


def test_is_rate_limit_unrelated():
    assert _is_rate_limit(ValueError("bad input")) is False


# ------------------------------------------------------------------
# Snapshot entity construction
# ------------------------------------------------------------------

def test_get_snapshot_newest(adapter, mock_waybackpy):
    mock_snap = MagicMock()
    mock_snap.archive_url = "https://web.archive.org/web/20240101/https://example.com"
    mock_snap.timestamp.return_value = "20240101120000"

    mock_avail = MagicMock()
    mock_avail.newest.return_value = mock_snap
    mock_waybackpy.WaybackMachineAvailabilityAPI.return_value = mock_avail

    finding = adapter._get_snapshot("https://example.com", "OSINT/0.1", newest=True)

    assert len(finding.entities) == 1
    ent = finding.entities[0]
    assert ent.entity_type == EntityType.DOCUMENT
    assert "example.com" in ent.label
    assert ent.properties["original_url"] == "https://example.com"
    assert ent.properties["archive_url"] == mock_snap.archive_url
    assert ent.properties["snapshot_type"] == "newest"
    assert "newest" in finding.notes.lower()


def test_get_snapshot_oldest(adapter, mock_waybackpy):
    mock_snap = MagicMock()
    mock_snap.archive_url = "https://web.archive.org/web/19991231/https://example.com"
    mock_snap.timestamp.return_value = "19991231000000"

    mock_avail = MagicMock()
    mock_avail.oldest.return_value = mock_snap
    mock_waybackpy.WaybackMachineAvailabilityAPI.return_value = mock_avail

    finding = adapter._get_snapshot("https://example.com", "OSINT/0.1", newest=False)

    assert finding.entities[0].properties["snapshot_type"] == "oldest"


def test_get_snapshot_not_found(adapter, mock_waybackpy):
    """should return notes finding when no snapshot exists."""
    mock_avail = MagicMock()
    mock_avail.newest.side_effect = Exception("No archive")
    mock_waybackpy.WaybackMachineAvailabilityAPI.return_value = mock_avail

    finding = adapter._get_snapshot("https://gone.example.com", "OSINT/0.1", newest=True)

    assert len(finding.entities) == 0
    assert "no newest snapshot" in finding.notes.lower()


# ------------------------------------------------------------------
# CDX snapshots
# ------------------------------------------------------------------

def test_get_cdx_snapshots(adapter, mock_waybackpy):
    mock_snap1 = MagicMock()
    mock_snap1.archive_url = "https://web.archive.org/web/20200101/https://example.com"
    mock_snap1.datetime_timestamp.isoformat.return_value = "2020-01-01T00:00:00"
    mock_snap1.statuscode = "200"
    mock_snap1.mimetype = "text/html"

    mock_snap2 = MagicMock()
    mock_snap2.archive_url = "https://web.archive.org/web/20210601/https://example.com"
    mock_snap2.datetime_timestamp.isoformat.return_value = "2021-06-01T00:00:00"
    mock_snap2.statuscode = "301"
    mock_snap2.mimetype = "text/html"

    mock_cdx = MagicMock()
    mock_cdx.snapshots.return_value = [mock_snap1, mock_snap2]
    mock_waybackpy.WaybackMachineCDXServerAPI.return_value = mock_cdx

    finding = adapter._get_cdx_snapshots("https://example.com", "OSINT/0.1")

    assert len(finding.entities) == 2
    assert finding.entities[0].properties["status_code"] == "200"
    assert finding.entities[1].properties["status_code"] == "301"
    assert "2 total snapshots" in finding.notes


def test_get_cdx_snapshots_caps_at_50(adapter, mock_waybackpy):
    """should limit output to 50 snapshots."""
    mock_snaps = []
    for i in range(100):
        snap = MagicMock()
        snap.archive_url = f"https://web.archive.org/web/{i}/https://example.com"
        snap.datetime_timestamp.isoformat.return_value = f"2020-01-{i:02d}T00:00:00"
        snap.statuscode = "200"
        snap.mimetype = "text/html"
        mock_snaps.append(snap)

    mock_cdx = MagicMock()
    mock_cdx.snapshots.return_value = mock_snaps
    mock_waybackpy.WaybackMachineCDXServerAPI.return_value = mock_cdx

    finding = adapter._get_cdx_snapshots("https://example.com", "OSINT/0.1")

    assert len(finding.entities) == 50
    assert "100 total" in finding.notes


def test_get_cdx_no_snapshots(adapter, mock_waybackpy):
    mock_cdx = MagicMock()
    mock_cdx.snapshots.return_value = []
    mock_waybackpy.WaybackMachineCDXServerAPI.return_value = mock_cdx

    finding = adapter._get_cdx_snapshots("https://gone.example.com", "OSINT/0.1")

    assert len(finding.entities) == 0
    assert "0 total" in finding.notes


# ------------------------------------------------------------------
# Retry behavior
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_snapshot_retries_on_429(adapter, mock_waybackpy):
    """should retry then succeed on rate limit."""
    mock_snap = MagicMock()
    mock_snap.archive_url = "https://web.archive.org/web/20240101/https://example.com"
    mock_snap.timestamp.return_value = "20240101"

    mock_avail = MagicMock()
    calls = [0]

    def newest_side_effect():
        calls[0] += 1
        if calls[0] == 1:
            raise Exception("429 Too Many Requests")
        return mock_snap

    mock_avail.newest = newest_side_effect
    mock_waybackpy.WaybackMachineAvailabilityAPI.return_value = mock_avail

    with patch("asyncio.sleep", new_callable=AsyncMock):
        finding = await adapter._get_snapshot_with_retry(
            "https://example.com", "OSINT/0.1", newest=True,
        )

    assert len(finding.entities) == 1
    assert calls[0] == 2  # First call 429, second succeeds
