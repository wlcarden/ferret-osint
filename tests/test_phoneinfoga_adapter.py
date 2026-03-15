"""Tests for the PhoneInfoga adapter — phone number intelligence."""

import json
import subprocess
from unittest.mock import patch

import pytest

from osint_agent.models import EntityType
from osint_agent.tools.phoneinfoga import PhoneInfogaAdapter


@pytest.fixture
def adapter():
    return PhoneInfogaAdapter()


@pytest.fixture
def mock_scan_result():
    """Canned PhoneInfoga scan output (dict form)."""
    return {
        "valid": True,
        "carrier": "T-Mobile USA",
        "country": "United States",
        "countryCode": 1,
        "formatInternational": "+1 415-555-1234",
        "formatNational": "(415) 555-1234",
        "lineType": "mobile",
        "location": "California",
    }


@pytest.fixture
def mock_scan_result_alt_keys():
    """PhoneInfoga output using alternative key names (older version)."""
    return {
        "valid": True,
        "carrier": "Verizon",
        "country": "United States",
        "countryCode": 1,
        "international_format": "+1 202-555-9876",
        "local_format": "(202) 555-9876",
        "line_type": "landline",
        "location": "Washington DC",
    }


@pytest.fixture
def mock_scan_result_minimal():
    """PhoneInfoga output with minimal data."""
    return {
        "valid": False,
    }


# ------------------------------------------------------------------
# Availability and metadata
# ------------------------------------------------------------------

def test_adapter_name(adapter):
    assert adapter.name == "phoneinfoga"


def test_required_binary(adapter):
    assert adapter.required_binary == "phoneinfoga"


# ------------------------------------------------------------------
# Happy path — full scan result (dict)
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_full_scan(adapter, mock_scan_result):
    """should create PHONE entity with all extracted properties"""
    completed = subprocess.CompletedProcess(
        args=["phoneinfoga"],
        returncode=0,
        stdout=json.dumps(mock_scan_result),
        stderr="",
    )

    with patch.object(adapter, "run_subprocess", return_value=completed):
        finding = await adapter.run(phone_number="+14155551234")

    assert len(finding.entities) == 1
    phone = finding.entities[0]

    assert phone.entity_type == EntityType.PHONE
    assert phone.id == "phone:+14155551234"
    # Label should use international format
    assert phone.label == "+1 415-555-1234"

    # Properties
    assert phone.properties["raw_number"] == "+14155551234"
    assert phone.properties["source_system"] == "phoneinfoga"
    assert phone.properties["valid"] is True
    assert phone.properties["carrier"] == "T-Mobile USA"
    assert phone.properties["country"] == "United States"
    assert phone.properties["country_code"] == 1
    assert phone.properties["international_format"] == "+1 415-555-1234"
    assert phone.properties["local_format"] == "(415) 555-1234"
    assert phone.properties["line_type"] == "mobile"
    assert phone.properties["location"] == "California"

    # Source
    assert phone.sources[0].tool == "phoneinfoga"
    assert phone.sources[0].raw_data == mock_scan_result

    # Notes
    assert "T-Mobile USA" in finding.notes
    assert "United States" in finding.notes


@pytest.mark.asyncio
async def test_run_alt_key_names(adapter, mock_scan_result_alt_keys):
    """should handle alternative key names from older PhoneInfoga versions"""
    completed = subprocess.CompletedProcess(
        args=["phoneinfoga"],
        returncode=0,
        stdout=json.dumps(mock_scan_result_alt_keys),
        stderr="",
    )

    with patch.object(adapter, "run_subprocess", return_value=completed):
        finding = await adapter.run(phone_number="+12025559876")

    phone = finding.entities[0]
    assert phone.properties["international_format"] == "+1 202-555-9876"
    assert phone.properties["local_format"] == "(202) 555-9876"
    assert phone.properties["line_type"] == "landline"
    assert phone.label == "+1 202-555-9876"


# ------------------------------------------------------------------
# List output (some versions wrap in array)
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_list_output(adapter, mock_scan_result):
    """should handle output wrapped in a JSON array"""
    completed = subprocess.CompletedProcess(
        args=["phoneinfoga"],
        returncode=0,
        stdout=json.dumps([mock_scan_result]),
        stderr="",
    )

    with patch.object(adapter, "run_subprocess", return_value=completed):
        finding = await adapter.run(phone_number="+14155551234")

    assert len(finding.entities) == 1
    assert finding.entities[0].properties["carrier"] == "T-Mobile USA"


@pytest.mark.asyncio
async def test_run_empty_list_output(adapter):
    """should handle empty list output gracefully"""
    completed = subprocess.CompletedProcess(
        args=["phoneinfoga"],
        returncode=0,
        stdout="[]",
        stderr="",
    )

    with patch.object(adapter, "run_subprocess", return_value=completed):
        finding = await adapter.run(phone_number="+10000000000")

    # Empty list -> empty dict -> entity with minimal properties
    assert len(finding.entities) == 1
    phone = finding.entities[0]
    assert phone.properties["raw_number"] == "+10000000000"
    # No carrier/country extracted
    assert "carrier" not in phone.properties
    assert "country" not in phone.properties


# ------------------------------------------------------------------
# Minimal / missing data
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_minimal_data(adapter, mock_scan_result_minimal):
    """should handle output with only 'valid' field"""
    completed = subprocess.CompletedProcess(
        args=["phoneinfoga"],
        returncode=0,
        stdout=json.dumps(mock_scan_result_minimal),
        stderr="",
    )

    with patch.object(adapter, "run_subprocess", return_value=completed):
        finding = await adapter.run(phone_number="+19999999999")

    phone = finding.entities[0]
    assert phone.properties["valid"] is False
    assert phone.properties["raw_number"] == "+19999999999"
    # Label should fall back to phone_number since no international_format
    assert phone.label == "+19999999999"

    # Notes should report unknown carrier/country
    assert "unknown" in finding.notes.lower()


@pytest.mark.asyncio
async def test_run_none_values_excluded(adapter):
    """should exclude properties with None values"""
    data = {
        "valid": True,
        "carrier": None,
        "country": "Germany",
        "countryCode": None,
        "formatInternational": None,
        "lineType": None,
    }
    completed = subprocess.CompletedProcess(
        args=["phoneinfoga"],
        returncode=0,
        stdout=json.dumps(data),
        stderr="",
    )

    with patch.object(adapter, "run_subprocess", return_value=completed):
        finding = await adapter.run(phone_number="+491234567890")

    phone = finding.entities[0]
    assert "carrier" not in phone.properties
    assert "country_code" not in phone.properties
    assert "international_format" not in phone.properties
    assert "line_type" not in phone.properties
    # country should be present
    assert phone.properties["country"] == "Germany"


# ------------------------------------------------------------------
# Error handling
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_nonzero_returncode(adapter):
    """should return notes finding when phoneinfoga exits with error"""
    completed = subprocess.CompletedProcess(
        args=["phoneinfoga"],
        returncode=1,
        stdout="",
        stderr="Error: invalid phone number format",
    )

    with patch.object(adapter, "run_subprocess", return_value=completed):
        finding = await adapter.run(phone_number="not-a-phone")

    assert len(finding.entities) == 0
    assert "failed" in finding.notes.lower()
    assert "invalid phone number" in finding.notes.lower()


@pytest.mark.asyncio
async def test_run_unparseable_output(adapter):
    """should return notes finding when output is not valid JSON"""
    completed = subprocess.CompletedProcess(
        args=["phoneinfoga"],
        returncode=0,
        stdout="Scanning number...\nDone.",
        stderr="",
    )

    with patch.object(adapter, "run_subprocess", return_value=completed):
        finding = await adapter.run(phone_number="+14155551234")

    assert len(finding.entities) == 0
    assert "unparseable" in finding.notes.lower()


@pytest.mark.asyncio
async def test_run_stderr_truncated(adapter):
    """should truncate long stderr in failure notes"""
    long_stderr = "X" * 1000
    completed = subprocess.CompletedProcess(
        args=["phoneinfoga"],
        returncode=1,
        stdout="",
        stderr=long_stderr,
    )

    with patch.object(adapter, "run_subprocess", return_value=completed):
        finding = await adapter.run(phone_number="+14155551234")

    # stderr[:500] used in notes
    assert len(finding.notes) < 600


# ------------------------------------------------------------------
# _parse_results direct tests
# ------------------------------------------------------------------

def test_parse_results_dict(adapter):
    """should parse dict data into phone entity"""
    data = {
        "carrier": "AT&T",
        "country": "US",
        "formatInternational": "+1 555-1234",
        "lineType": "voip",
    }
    finding = adapter._parse_results("+15551234", data)
    assert len(finding.entities) == 1
    assert finding.entities[0].properties["carrier"] == "AT&T"
    assert finding.entities[0].properties["line_type"] == "voip"


def test_parse_results_list_with_one_item(adapter):
    """should unwrap single-element list"""
    data = [{"carrier": "Sprint", "country": "US"}]
    finding = adapter._parse_results("+15559999", data)
    assert finding.entities[0].properties["carrier"] == "Sprint"


def test_parse_results_empty_dict(adapter):
    """should create entity with minimal properties for empty dict"""
    finding = adapter._parse_results("+10000000000", {})
    assert len(finding.entities) == 1
    phone = finding.entities[0]
    assert phone.properties["raw_number"] == "+10000000000"
    assert phone.properties["source_system"] == "phoneinfoga"
    assert phone.id == "phone:+10000000000"


def test_parse_results_empty_list(adapter):
    """should handle empty list by treating as empty dict"""
    finding = adapter._parse_results("+10000000000", [])
    assert len(finding.entities) == 1
    assert finding.entities[0].properties["raw_number"] == "+10000000000"
