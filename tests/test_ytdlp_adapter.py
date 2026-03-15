"""Tests for the yt-dlp adapter — YouTube/video metadata extraction."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from osint_agent.models import EntityType, RelationType
from osint_agent.tools.ytdlp import YtDlpAdapter


@pytest.fixture
def adapter():
    return YtDlpAdapter()


@pytest.fixture
def mock_video_info():
    """Canned yt-dlp extract_info result for a single video."""
    return {
        "id": "dQw4w9WgXcQ",
        "title": "Rick Astley - Never Gonna Give You Up (Official Music Video)",
        "description": "The official video for Never Gonna Give You Up by Rick Astley.",
        "upload_date": "20091025",
        "duration": 213,
        "view_count": 1500000000,
        "like_count": 16000000,
        "comment_count": 2500000,
        "categories": ["Music"],
        "tags": ["rick astley", "never gonna give you up", "rickroll"],
        "webpage_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "thumbnail": "https://i.ytimg.com/vi/dQw4w9WgXcQ/maxresdefault.jpg",
        "channel": "Rick Astley",
        "channel_id": "UCuAXFkgsw1L7xaCfnd5JJOw",
        "channel_url": "https://www.youtube.com/channel/UCuAXFkgsw1L7xaCfnd5JJOw",
        "uploader": "Rick Astley",
        "uploader_id": "@RickAstleyYT",
        "channel_follower_count": 7500000,
    }


@pytest.fixture
def mock_channel_info():
    """Canned yt-dlp extract_info result for a channel/playlist."""
    return {
        "id": "UCuAXFkgsw1L7xaCfnd5JJOw",
        "title": "Rick Astley",
        "channel": "Rick Astley",
        "description": "Official YouTube channel of Rick Astley",
        "webpage_url": "https://www.youtube.com/channel/UCuAXFkgsw1L7xaCfnd5JJOw",
        "channel_follower_count": 7500000,
        "entries": [
            {
                "id": "dQw4w9WgXcQ",
                "title": "Never Gonna Give You Up",
                "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                "duration": 213,
                "view_count": 1500000000,
            },
            {
                "id": "lYBUbBu4W08",
                "title": "Together Forever",
                "url": "https://www.youtube.com/watch?v=lYBUbBu4W08",
                "duration": 203,
                "view_count": 80000000,
            },
            {
                "id": "AC3Ejf7vPEY",
                "title": "Whenever You Need Somebody",
                "url": "https://www.youtube.com/watch?v=AC3Ejf7vPEY",
                "duration": 195,
                "view_count": 12000000,
            },
        ],
    }


# ------------------------------------------------------------------
# Availability
# ------------------------------------------------------------------

def test_is_available_when_installed():
    with patch.dict("sys.modules", {"yt_dlp": MagicMock()}):
        adapter = YtDlpAdapter()
        assert adapter.is_available() is True


def test_is_available_when_not_installed():
    adapter = YtDlpAdapter()
    with patch("builtins.__import__", side_effect=ImportError):
        assert adapter.is_available() is False


def test_adapter_name(adapter):
    assert adapter.name == "yt-dlp"


def test_required_package():
    assert YtDlpAdapter.required_package == "yt_dlp"


# ------------------------------------------------------------------
# Single video — happy path
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_video_entities(adapter, mock_video_info):
    """should create video document and channel account entities"""
    mock_yt = MagicMock()
    mock_ydl_instance = MagicMock()
    mock_ydl_instance.__enter__ = MagicMock(return_value=mock_ydl_instance)
    mock_ydl_instance.__exit__ = MagicMock(return_value=False)
    mock_ydl_instance.extract_info.return_value = mock_video_info
    mock_yt.YoutubeDL.return_value = mock_ydl_instance

    with patch.dict("sys.modules", {"yt_dlp": mock_yt}):
        finding = await adapter.run(
            url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        )

    # Video entity (document)
    docs = [e for e in finding.entities if e.entity_type == EntityType.DOCUMENT]
    assert len(docs) == 1
    video = docs[0]
    assert video.id == "document:video:dQw4w9WgXcQ"
    assert "Never Gonna Give You Up" in video.label
    assert video.properties["title"] == "Rick Astley - Never Gonna Give You Up (Official Music Video)"
    assert video.properties["upload_date"] == "20091025"
    assert video.properties["duration"] == 213
    assert video.properties["view_count"] == 1500000000
    assert video.properties["like_count"] == 16000000
    assert video.properties["comment_count"] == 2500000
    assert video.properties["categories"] == ["Music"]
    assert "rickroll" in video.properties["tags"]
    assert video.properties["webpage_url"] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

    # Channel entity (account)
    accounts = [e for e in finding.entities if e.entity_type == EntityType.ACCOUNT]
    assert len(accounts) == 1
    channel = accounts[0]
    assert channel.id == "account:youtube:UCuAXFkgsw1L7xaCfnd5JJOw"
    assert channel.label == "Rick Astley"
    assert channel.properties["platform"] == "YouTube"
    assert channel.properties["subscriber_count"] == 7500000
    assert channel.properties["url"] == (
        "https://www.youtube.com/channel/UCuAXFkgsw1L7xaCfnd5JJOw"
    )

    # Channel OWNS video relationship
    owns = [r for r in finding.relationships if r.relation_type == RelationType.OWNS]
    assert len(owns) == 1
    assert owns[0].source_id == channel.id
    assert owns[0].target_id == video.id

    # Notes
    assert "Never Gonna Give You Up" in finding.notes
    assert "1,500,000,000 views" in finding.notes
    assert "Rick Astley" in finding.notes


@pytest.mark.asyncio
async def test_run_video_no_channel(adapter):
    """should handle video without channel info"""
    info = {
        "id": "abc123",
        "title": "Untitled Video",
        "view_count": 100,
    }
    mock_yt = MagicMock()
    mock_ydl_instance = MagicMock()
    mock_ydl_instance.__enter__ = MagicMock(return_value=mock_ydl_instance)
    mock_ydl_instance.__exit__ = MagicMock(return_value=False)
    mock_ydl_instance.extract_info.return_value = info
    mock_yt.YoutubeDL.return_value = mock_ydl_instance

    with patch.dict("sys.modules", {"yt_dlp": mock_yt}):
        finding = await adapter.run(url="https://example.com/video")

    # Video entity should exist
    docs = [e for e in finding.entities if e.entity_type == EntityType.DOCUMENT]
    assert len(docs) == 1
    assert docs[0].id == "document:video:abc123"

    # No channel/account entity
    accounts = [e for e in finding.entities if e.entity_type == EntityType.ACCOUNT]
    assert len(accounts) == 0

    # No relationships
    assert len(finding.relationships) == 0


@pytest.mark.asyncio
async def test_run_video_uses_uploader_fallback(adapter):
    """should fall back to uploader/uploader_id when channel fields are absent"""
    info = {
        "id": "xyz789",
        "title": "Fallback Test",
        "uploader": "Some Uploader",
        "uploader_id": "uploader123",
    }
    mock_yt = MagicMock()
    mock_ydl_instance = MagicMock()
    mock_ydl_instance.__enter__ = MagicMock(return_value=mock_ydl_instance)
    mock_ydl_instance.__exit__ = MagicMock(return_value=False)
    mock_ydl_instance.extract_info.return_value = info
    mock_yt.YoutubeDL.return_value = mock_ydl_instance

    with patch.dict("sys.modules", {"yt_dlp": mock_yt}):
        finding = await adapter.run(url="https://example.com/v")

    accounts = [e for e in finding.entities if e.entity_type == EntityType.ACCOUNT]
    assert len(accounts) == 1
    assert accounts[0].label == "Some Uploader"
    assert accounts[0].id == "account:youtube:uploader123"


@pytest.mark.asyncio
async def test_run_video_excludes_empty_properties(adapter):
    """should not include None/empty values in video properties"""
    info = {
        "id": "sparse1",
        "title": "Sparse Video",
        "description": "",
        "upload_date": None,
        "duration": None,
        "view_count": 0,
        "like_count": None,
        "tags": [],
        "categories": [],
    }
    mock_yt = MagicMock()
    mock_ydl_instance = MagicMock()
    mock_ydl_instance.__enter__ = MagicMock(return_value=mock_ydl_instance)
    mock_ydl_instance.__exit__ = MagicMock(return_value=False)
    mock_ydl_instance.extract_info.return_value = info
    mock_yt.YoutubeDL.return_value = mock_ydl_instance

    with patch.dict("sys.modules", {"yt_dlp": mock_yt}):
        finding = await adapter.run(url="https://example.com/v")

    video = [e for e in finding.entities if e.entity_type == EntityType.DOCUMENT][0]
    assert "description" not in video.properties
    assert "upload_date" not in video.properties
    assert "duration" not in video.properties
    assert "like_count" not in video.properties
    assert "tags" not in video.properties
    assert "categories" not in video.properties


@pytest.mark.asyncio
async def test_run_video_title_truncated(adapter):
    """should truncate very long titles in label"""
    long_title = "A" * 200
    info = {
        "id": "long1",
        "title": long_title,
    }
    mock_yt = MagicMock()
    mock_ydl_instance = MagicMock()
    mock_ydl_instance.__enter__ = MagicMock(return_value=mock_ydl_instance)
    mock_ydl_instance.__exit__ = MagicMock(return_value=False)
    mock_ydl_instance.extract_info.return_value = info
    mock_yt.YoutubeDL.return_value = mock_ydl_instance

    with patch.dict("sys.modules", {"yt_dlp": mock_yt}):
        finding = await adapter.run(url="https://example.com/v")

    video = [e for e in finding.entities if e.entity_type == EntityType.DOCUMENT][0]
    assert len(video.label) <= 120


# ------------------------------------------------------------------
# Channel/playlist — happy path
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_channel_entities(adapter, mock_channel_info):
    """should create channel account and video document entities"""
    mock_yt = MagicMock()
    mock_ydl_instance = MagicMock()
    mock_ydl_instance.__enter__ = MagicMock(return_value=mock_ydl_instance)
    mock_ydl_instance.__exit__ = MagicMock(return_value=False)
    mock_ydl_instance.extract_info.return_value = mock_channel_info
    mock_yt.YoutubeDL.return_value = mock_ydl_instance

    with patch.dict("sys.modules", {"yt_dlp": mock_yt}):
        finding = await adapter.run(
            url="https://www.youtube.com/channel/UCuAXFkgsw1L7xaCfnd5JJOw",
        )

    # Channel entity
    accounts = [e for e in finding.entities if e.entity_type == EntityType.ACCOUNT]
    assert len(accounts) == 1
    channel = accounts[0]
    assert channel.id == "account:youtube:UCuAXFkgsw1L7xaCfnd5JJOw"
    assert channel.label == "Rick Astley"
    assert channel.properties["platform"] == "YouTube"
    assert channel.properties["subscriber_count"] == 7500000
    assert channel.properties["description"] == "Official YouTube channel of Rick Astley"

    # Video entities
    docs = [e for e in finding.entities if e.entity_type == EntityType.DOCUMENT]
    assert len(docs) == 3
    vid_ids = {d.id for d in docs}
    assert "document:video:dQw4w9WgXcQ" in vid_ids
    assert "document:video:lYBUbBu4W08" in vid_ids
    assert "document:video:AC3Ejf7vPEY" in vid_ids

    # Video properties
    rickroll = next(d for d in docs if d.id == "document:video:dQw4w9WgXcQ")
    assert rickroll.properties["title"] == "Never Gonna Give You Up"
    assert rickroll.properties["url"] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert rickroll.properties["duration"] == 213
    assert rickroll.properties["view_count"] == 1500000000

    # Relationships: channel OWNS each video
    owns = [r for r in finding.relationships if r.relation_type == RelationType.OWNS]
    assert len(owns) == 3
    assert all(r.source_id == channel.id for r in owns)
    target_ids = {r.target_id for r in owns}
    assert target_ids == vid_ids

    # Notes
    assert "Rick Astley" in finding.notes
    assert "3 videos" in finding.notes


@pytest.mark.asyncio
async def test_run_channel_empty_entries(adapter):
    """should handle channel with empty entries list"""
    info = {
        "id": "UCempty",
        "title": "Empty Channel",
        "channel": "Empty Channel",
        "entries": [],
        "webpage_url": "https://www.youtube.com/channel/UCempty",
    }
    mock_yt = MagicMock()
    mock_ydl_instance = MagicMock()
    mock_ydl_instance.__enter__ = MagicMock(return_value=mock_ydl_instance)
    mock_ydl_instance.__exit__ = MagicMock(return_value=False)
    mock_ydl_instance.extract_info.return_value = info
    mock_yt.YoutubeDL.return_value = mock_ydl_instance

    with patch.dict("sys.modules", {"yt_dlp": mock_yt}):
        finding = await adapter.run(url="https://www.youtube.com/channel/UCempty")

    accounts = [e for e in finding.entities if e.entity_type == EntityType.ACCOUNT]
    assert len(accounts) == 1
    assert accounts[0].label == "Empty Channel"

    docs = [e for e in finding.entities if e.entity_type == EntityType.DOCUMENT]
    assert len(docs) == 0

    assert "0 videos" in finding.notes


@pytest.mark.asyncio
async def test_run_channel_skips_none_entries(adapter):
    """should skip None entries in the entries list"""
    info = {
        "id": "UCskip",
        "title": "Skip Channel",
        "channel": "Skip Channel",
        "entries": [
            None,
            {"id": "vid1", "title": "Real Video", "url": "https://example.com/v1"},
            None,
        ],
        "webpage_url": "https://www.youtube.com/channel/UCskip",
    }
    mock_yt = MagicMock()
    mock_ydl_instance = MagicMock()
    mock_ydl_instance.__enter__ = MagicMock(return_value=mock_ydl_instance)
    mock_ydl_instance.__exit__ = MagicMock(return_value=False)
    mock_ydl_instance.extract_info.return_value = info
    mock_yt.YoutubeDL.return_value = mock_ydl_instance

    with patch.dict("sys.modules", {"yt_dlp": mock_yt}):
        finding = await adapter.run(url="https://example.com/channel")

    docs = [e for e in finding.entities if e.entity_type == EntityType.DOCUMENT]
    assert len(docs) == 1
    assert docs[0].id == "document:video:vid1"


@pytest.mark.asyncio
async def test_run_channel_skips_entries_without_id_or_title(adapter):
    """should skip entries missing id or title"""
    info = {
        "id": "UCincomplete",
        "title": "Incomplete Channel",
        "channel": "Incomplete Channel",
        "entries": [
            {"id": "", "title": "No ID"},
            {"id": "vid2", "title": ""},
            {"id": "vid3", "title": "Complete Video", "url": "https://example.com/v3"},
        ],
        "webpage_url": "https://www.youtube.com/channel/UCincomplete",
    }
    mock_yt = MagicMock()
    mock_ydl_instance = MagicMock()
    mock_ydl_instance.__enter__ = MagicMock(return_value=mock_ydl_instance)
    mock_ydl_instance.__exit__ = MagicMock(return_value=False)
    mock_ydl_instance.extract_info.return_value = info
    mock_yt.YoutubeDL.return_value = mock_ydl_instance

    with patch.dict("sys.modules", {"yt_dlp": mock_yt}):
        finding = await adapter.run(url="https://example.com/channel")

    docs = [e for e in finding.entities if e.entity_type == EntityType.DOCUMENT]
    assert len(docs) == 1
    assert docs[0].id == "document:video:vid3"


@pytest.mark.asyncio
async def test_run_channel_limits_to_20_entries(adapter):
    """should only include the first 20 video entries"""
    entries = [
        {"id": f"vid{i}", "title": f"Video {i}", "url": f"https://example.com/v{i}"}
        for i in range(30)
    ]
    info = {
        "id": "UCmany",
        "title": "Many Videos Channel",
        "channel": "Many Videos Channel",
        "entries": entries,
        "webpage_url": "https://www.youtube.com/channel/UCmany",
    }
    mock_yt = MagicMock()
    mock_ydl_instance = MagicMock()
    mock_ydl_instance.__enter__ = MagicMock(return_value=mock_ydl_instance)
    mock_ydl_instance.__exit__ = MagicMock(return_value=False)
    mock_ydl_instance.extract_info.return_value = info
    mock_yt.YoutubeDL.return_value = mock_ydl_instance

    with patch.dict("sys.modules", {"yt_dlp": mock_yt}):
        finding = await adapter.run(url="https://example.com/channel")

    docs = [e for e in finding.entities if e.entity_type == EntityType.DOCUMENT]
    assert len(docs) == 20
    assert "20 videos" in finding.notes


@pytest.mark.asyncio
async def test_run_channel_uses_title_fallback(adapter):
    """should use 'title' when 'channel' field is absent"""
    info = {
        "id": "UCfallback",
        "title": "Fallback Title",
        "entries": [],
        "webpage_url": "https://example.com/channel",
    }
    mock_yt = MagicMock()
    mock_ydl_instance = MagicMock()
    mock_ydl_instance.__enter__ = MagicMock(return_value=mock_ydl_instance)
    mock_ydl_instance.__exit__ = MagicMock(return_value=False)
    mock_ydl_instance.extract_info.return_value = info
    mock_yt.YoutubeDL.return_value = mock_ydl_instance

    with patch.dict("sys.modules", {"yt_dlp": mock_yt}):
        finding = await adapter.run(url="https://example.com/channel")

    accounts = [e for e in finding.entities if e.entity_type == EntityType.ACCOUNT]
    assert len(accounts) == 1
    assert accounts[0].label == "Fallback Title"


# ------------------------------------------------------------------
# Error handling
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_extract_exception(adapter):
    """should handle yt-dlp extraction failure"""
    mock_yt = MagicMock()
    mock_ydl_instance = MagicMock()
    mock_ydl_instance.__enter__ = MagicMock(return_value=mock_ydl_instance)
    mock_ydl_instance.__exit__ = MagicMock(return_value=False)
    mock_ydl_instance.extract_info.side_effect = RuntimeError("Video unavailable")
    mock_yt.YoutubeDL.return_value = mock_ydl_instance

    with patch.dict("sys.modules", {"yt_dlp": mock_yt}):
        finding = await adapter.run(url="https://www.youtube.com/watch?v=deleted")

    assert "error" in finding.notes.lower()
    assert "Video unavailable" in finding.notes
    assert len(finding.entities) == 0


@pytest.mark.asyncio
async def test_run_returns_none_info(adapter):
    """should handle extract_info returning None"""
    mock_yt = MagicMock()
    mock_ydl_instance = MagicMock()
    mock_ydl_instance.__enter__ = MagicMock(return_value=mock_ydl_instance)
    mock_ydl_instance.__exit__ = MagicMock(return_value=False)
    mock_ydl_instance.extract_info.return_value = None
    mock_yt.YoutubeDL.return_value = mock_ydl_instance

    with patch.dict("sys.modules", {"yt_dlp": mock_yt}):
        finding = await adapter.run(url="https://www.youtube.com/watch?v=gone")

    assert "no data" in finding.notes.lower()
    assert len(finding.entities) == 0


# ------------------------------------------------------------------
# Detection: video vs channel
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatches_to_video_when_no_entries(adapter, mock_video_info):
    """should use _build_video_finding when 'entries' key is absent"""
    mock_yt = MagicMock()
    mock_ydl_instance = MagicMock()
    mock_ydl_instance.__enter__ = MagicMock(return_value=mock_ydl_instance)
    mock_ydl_instance.__exit__ = MagicMock(return_value=False)
    mock_ydl_instance.extract_info.return_value = mock_video_info
    mock_yt.YoutubeDL.return_value = mock_ydl_instance

    with patch.dict("sys.modules", {"yt_dlp": mock_yt}):
        finding = await adapter.run(url="https://www.youtube.com/watch?v=dQw4w9WgXcQ")

    # Should be treated as video, not channel
    accounts = [e for e in finding.entities if e.entity_type == EntityType.ACCOUNT]
    docs = [e for e in finding.entities if e.entity_type == EntityType.DOCUMENT]
    assert len(docs) == 1
    assert len(accounts) == 1


@pytest.mark.asyncio
async def test_dispatches_to_channel_when_entries_present(adapter, mock_channel_info):
    """should use _build_channel_finding when 'entries' key is present"""
    mock_yt = MagicMock()
    mock_ydl_instance = MagicMock()
    mock_ydl_instance.__enter__ = MagicMock(return_value=mock_ydl_instance)
    mock_ydl_instance.__exit__ = MagicMock(return_value=False)
    mock_ydl_instance.extract_info.return_value = mock_channel_info
    mock_yt.YoutubeDL.return_value = mock_ydl_instance

    with patch.dict("sys.modules", {"yt_dlp": mock_yt}):
        finding = await adapter.run(url="https://www.youtube.com/channel/UC123")

    assert "Channel:" in finding.notes


# ------------------------------------------------------------------
# Channel description truncation
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_channel_description_truncated(adapter):
    """should truncate channel description to 500 chars"""
    long_desc = "X" * 1000
    info = {
        "id": "UCtrunc",
        "title": "Truncate Channel",
        "channel": "Truncate Channel",
        "description": long_desc,
        "entries": [],
        "webpage_url": "https://example.com/channel",
    }
    mock_yt = MagicMock()
    mock_ydl_instance = MagicMock()
    mock_ydl_instance.__enter__ = MagicMock(return_value=mock_ydl_instance)
    mock_ydl_instance.__exit__ = MagicMock(return_value=False)
    mock_ydl_instance.extract_info.return_value = info
    mock_yt.YoutubeDL.return_value = mock_ydl_instance

    with patch.dict("sys.modules", {"yt_dlp": mock_yt}):
        finding = await adapter.run(url="https://example.com/channel")

    channel = [e for e in finding.entities if e.entity_type == EntityType.ACCOUNT][0]
    assert len(channel.properties["description"]) <= 500


# ------------------------------------------------------------------
# Registry
# ------------------------------------------------------------------

def test_registered_in_registry():
    from osint_agent.tools.registry import INPUT_ROUTING

    assert "yt-dlp" in INPUT_ROUTING["url"]
