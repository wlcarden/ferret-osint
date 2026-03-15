"""Tests for the ExifTool adapter — image/media metadata extraction."""

import json
import subprocess
from unittest.mock import patch

import pytest

from osint_agent.models import EntityType, RelationType
from osint_agent.tools.exiftool import ExifToolAdapter


@pytest.fixture
def adapter():
    return ExifToolAdapter()


@pytest.fixture
def mock_exif_with_gps():
    """Canned ExifTool JSON output with GPS coordinates."""
    return [
        {
            "File:FileName": "IMG_20240315.jpg",
            "File:FileType": "JPEG",
            "File:FileSize": 4821504,
            "File:MIMEType": "image/jpeg",
            "File:ImageWidth": 4032,
            "File:ImageHeight": 3024,
            "EXIF:Make": "Apple",
            "EXIF:Model": "iPhone 14 Pro",
            "EXIF:Software": "17.3.1",
            "EXIF:CreateDate": "2024:03:15 14:30:22",
            "EXIF:ModifyDate": "2024:03:15 14:30:22",
            "EXIF:DateTimeOriginal": "2024:03:15 14:30:22",
            "EXIF:GPSLatitude": 45.523064,
            "EXIF:GPSLongitude": -122.676483,
            "EXIF:GPSAltitude": 15.3,
        },
    ]


@pytest.fixture
def mock_exif_no_gps():
    """Canned ExifTool JSON output without GPS data."""
    return [
        {
            "File:FileName": "screenshot.png",
            "File:FileType": "PNG",
            "File:FileSize": 102400,
            "File:MIMEType": "image/png",
            "File:ImageWidth": 1920,
            "File:ImageHeight": 1080,
            "EXIF:Software": "GIMP 2.10",
        },
    ]


@pytest.fixture
def mock_exif_composite_gps():
    """ExifTool output with GPS in Composite group (some cameras)."""
    return [
        {
            "File:FileName": "photo.jpg",
            "File:FileType": "JPEG",
            "File:FileSize": 2048000,
            "File:MIMEType": "image/jpeg",
            "Composite:GPSLatitude": 34.052235,
            "Composite:GPSLongitude": -118.243683,
        },
    ]


# ------------------------------------------------------------------
# Availability and metadata
# ------------------------------------------------------------------

def test_adapter_name(adapter):
    assert adapter.name == "exiftool"


def test_required_binary(adapter):
    assert adapter.required_binary == "exiftool"


# ------------------------------------------------------------------
# Happy path — full EXIF with GPS
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_with_gps(adapter, mock_exif_with_gps):
    """should create DOCUMENT + ADDRESS entities and HAS_ADDRESS relationship"""
    completed = subprocess.CompletedProcess(
        args=["exiftool"], returncode=0,
        stdout=json.dumps(mock_exif_with_gps), stderr="",
    )

    with patch.object(adapter, "run_subprocess", return_value=completed):
        finding = await adapter.run(file_path="/tmp/IMG_20240315.jpg")

    # Should have a document entity
    docs = [e for e in finding.entities if e.entity_type == EntityType.DOCUMENT]
    assert len(docs) == 1
    doc = docs[0]
    assert doc.id == "document:exif:/tmp/IMG_20240315.jpg"
    assert "IMG_20240315.jpg" in doc.label

    # Check extracted properties
    assert doc.properties["camera_make"] == "Apple"
    assert doc.properties["camera_model"] == "iPhone 14 Pro"
    assert doc.properties["file_type"] == "JPEG"
    assert doc.properties["mime_type"] == "image/jpeg"
    assert doc.properties["image_width"] == 4032
    assert doc.properties["image_height"] == 3024
    assert doc.properties["software"] == "17.3.1"
    assert doc.properties["create_date"] == "2024:03:15 14:30:22"
    assert doc.properties["file_path"] == "/tmp/IMG_20240315.jpg"

    # Should have an address entity with GPS coordinates
    addrs = [e for e in finding.entities if e.entity_type == EntityType.ADDRESS]
    assert len(addrs) == 1
    addr = addrs[0]
    assert addr.properties["latitude"] == pytest.approx(45.523064)
    assert addr.properties["longitude"] == pytest.approx(-122.676483)
    assert addr.properties["altitude"] == 15.3
    assert "45.523064" in addr.id
    assert "GPS:" in addr.label

    # Should have HAS_ADDRESS relationship
    rels = [r for r in finding.relationships if r.relation_type == RelationType.HAS_ADDRESS]
    assert len(rels) == 1
    assert rels[0].source_id == doc.id
    assert rels[0].target_id == addr.id
    assert rels[0].properties["address_type"] == "gps_coordinates"

    # Notes should mention GPS
    assert "GPS" in finding.notes


@pytest.mark.asyncio
async def test_run_without_gps(adapter, mock_exif_no_gps):
    """should create only DOCUMENT entity when no GPS present"""
    completed = subprocess.CompletedProcess(
        args=["exiftool"], returncode=0,
        stdout=json.dumps(mock_exif_no_gps), stderr="",
    )

    with patch.object(adapter, "run_subprocess", return_value=completed):
        finding = await adapter.run(file_path="/tmp/screenshot.png")

    docs = [e for e in finding.entities if e.entity_type == EntityType.DOCUMENT]
    assert len(docs) == 1
    assert docs[0].properties["file_type"] == "PNG"
    assert docs[0].properties["software"] == "GIMP 2.10"

    # No address entity or relationship
    addrs = [e for e in finding.entities if e.entity_type == EntityType.ADDRESS]
    assert len(addrs) == 0
    assert len(finding.relationships) == 0

    # Notes should indicate no GPS
    assert "no GPS" in finding.notes


@pytest.mark.asyncio
async def test_run_composite_gps(adapter, mock_exif_composite_gps):
    """should extract GPS from Composite group as fallback"""
    completed = subprocess.CompletedProcess(
        args=["exiftool"], returncode=0,
        stdout=json.dumps(mock_exif_composite_gps), stderr="",
    )

    with patch.object(adapter, "run_subprocess", return_value=completed):
        finding = await adapter.run(file_path="/tmp/photo.jpg")

    addrs = [e for e in finding.entities if e.entity_type == EntityType.ADDRESS]
    assert len(addrs) == 1
    assert addrs[0].properties["latitude"] == pytest.approx(34.052235)
    assert addrs[0].properties["longitude"] == pytest.approx(-118.243683)


# ------------------------------------------------------------------
# Source tracking
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sources_include_raw_data(adapter, mock_exif_with_gps):
    """should store raw ExifTool output in document source"""
    completed = subprocess.CompletedProcess(
        args=["exiftool"], returncode=0,
        stdout=json.dumps(mock_exif_with_gps), stderr="",
    )

    with patch.object(adapter, "run_subprocess", return_value=completed):
        finding = await adapter.run(file_path="/tmp/test.jpg")

    doc = finding.entities[0]
    assert doc.sources[0].tool == "exiftool"
    assert doc.sources[0].raw_data is not None
    assert doc.sources[0].raw_data["EXIF:Make"] == "Apple"


# ------------------------------------------------------------------
# Error handling
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_nonzero_returncode(adapter):
    """should return notes finding when exiftool exits with error"""
    completed = subprocess.CompletedProcess(
        args=["exiftool"], returncode=1,
        stdout="", stderr="File not found: /nonexistent.jpg",
    )

    with patch.object(adapter, "run_subprocess", return_value=completed):
        finding = await adapter.run(file_path="/nonexistent.jpg")

    assert len(finding.entities) == 0
    assert "failed" in finding.notes.lower()
    assert "File not found" in finding.notes


@pytest.mark.asyncio
async def test_run_unparseable_json(adapter):
    """should return notes finding when output is not valid JSON"""
    completed = subprocess.CompletedProcess(
        args=["exiftool"], returncode=0,
        stdout="not valid json at all", stderr="",
    )

    with patch.object(adapter, "run_subprocess", return_value=completed):
        finding = await adapter.run(file_path="/tmp/corrupt.jpg")

    assert len(finding.entities) == 0
    assert "unparseable" in finding.notes.lower()


@pytest.mark.asyncio
async def test_run_empty_metadata_list(adapter):
    """should return notes finding when exiftool returns empty array"""
    completed = subprocess.CompletedProcess(
        args=["exiftool"], returncode=0,
        stdout="[]", stderr="",
    )

    with patch.object(adapter, "run_subprocess", return_value=completed):
        finding = await adapter.run(file_path="/tmp/empty.jpg")

    assert len(finding.entities) == 0
    assert "no metadata" in finding.notes.lower()


# ------------------------------------------------------------------
# Property filtering
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_empty_properties_filtered(adapter):
    """should exclude empty-string properties from the entity"""
    exif_data = [
        {
            "File:FileName": "minimal.jpg",
            "File:FileType": "JPEG",
            # camera_make, camera_model, software all missing
        },
    ]
    completed = subprocess.CompletedProcess(
        args=["exiftool"], returncode=0,
        stdout=json.dumps(exif_data), stderr="",
    )

    with patch.object(adapter, "run_subprocess", return_value=completed):
        finding = await adapter.run(file_path="/tmp/minimal.jpg")

    doc = finding.entities[0]
    # Empty-string fields should not be present
    assert "camera_make" not in doc.properties
    assert "camera_model" not in doc.properties
    assert "software" not in doc.properties
    # Non-empty fields should be present
    assert doc.properties["file_type"] == "JPEG"
    assert doc.properties["file_name"] == "minimal.jpg"


@pytest.mark.asyncio
async def test_truncated_stderr_on_failure(adapter):
    """should truncate stderr to 500 chars in failure notes"""
    long_stderr = "E" * 1000
    completed = subprocess.CompletedProcess(
        args=["exiftool"], returncode=1,
        stdout="", stderr=long_stderr,
    )

    with patch.object(adapter, "run_subprocess", return_value=completed):
        finding = await adapter.run(file_path="/tmp/bad.jpg")

    # Notes should contain at most 500 chars of stderr
    assert len(finding.notes) < 600
