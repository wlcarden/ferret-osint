"""Tests for API key validation."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from osint_agent.key_validator import (
    _check_congress,
    _check_courtlistener,
    _check_openfec,
    _check_sec_edgar,
    _check_shodan,
    _check_virustotal,
    print_validation_report,
    validate_api_keys,
)


def _mock_client(status_code: int = 200, json_data: dict | None = None):
    """Create a mock httpx.AsyncClient with a canned response."""
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.json.return_value = json_data or {}
    client = AsyncMock()
    client.get = AsyncMock(return_value=mock_resp)
    return client


# ------------------------------------------------------------------
# Individual validators — missing key
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_courtlistener_missing_key():
    with patch.dict("os.environ", {}, clear=True):
        name, valid, msg = await _check_courtlistener(_mock_client())
    assert name == "CourtListener"
    assert valid is False
    assert "not set" in msg


@pytest.mark.asyncio
async def test_openfec_missing_key():
    with patch.dict("os.environ", {}, clear=True):
        name, valid, msg = await _check_openfec(_mock_client())
    assert name == "OpenFEC"
    assert valid is False
    assert "not set" in msg


@pytest.mark.asyncio
async def test_congress_missing_key():
    with patch.dict("os.environ", {}, clear=True):
        name, valid, msg = await _check_congress(_mock_client())
    assert name == "Congress.gov"
    assert valid is False


@pytest.mark.asyncio
async def test_sec_edgar_missing_key():
    with patch.dict("os.environ", {}, clear=True):
        name, valid, msg = await _check_sec_edgar(_mock_client())
    assert name == "SEC EDGAR"
    assert valid is False


@pytest.mark.asyncio
async def test_shodan_missing_key():
    with patch.dict("os.environ", {}, clear=True):
        name, valid, msg = await _check_shodan(_mock_client())
    assert name == "Shodan"
    assert valid is False
    assert "optional" in msg.lower()


@pytest.mark.asyncio
async def test_virustotal_missing_key():
    with patch.dict("os.environ", {}, clear=True):
        name, valid, msg = await _check_virustotal(_mock_client())
    assert name == "VirusTotal"
    assert valid is False


# ------------------------------------------------------------------
# Individual validators — valid key
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_courtlistener_valid_key():
    with patch.dict("os.environ", {"COURTLISTENER_API_KEY": "test_key"}):
        name, valid, msg = await _check_courtlistener(_mock_client(200))
    assert valid is True
    assert msg == "valid"


@pytest.mark.asyncio
async def test_openfec_valid_key():
    with patch.dict("os.environ", {"OPENFEC_API_KEY": "test_key"}):
        name, valid, msg = await _check_openfec(_mock_client(200))
    assert valid is True


@pytest.mark.asyncio
async def test_congress_valid_key():
    with patch.dict("os.environ", {"CONGRESS_API_KEY": "test_key"}):
        name, valid, msg = await _check_congress(_mock_client(200))
    assert valid is True


@pytest.mark.asyncio
async def test_shodan_valid_with_credits():
    with patch.dict("os.environ", {"SHODAN_API_KEY": "test_key"}):
        name, valid, msg = await _check_shodan(
            _mock_client(200, {"query_credits": 100}),
        )
    assert valid is True
    assert "100" in msg


# ------------------------------------------------------------------
# Individual validators — invalid key
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_courtlistener_invalid_key():
    with patch.dict("os.environ", {"COURTLISTENER_API_KEY": "bad"}):
        name, valid, msg = await _check_courtlistener(_mock_client(401))
    assert valid is False
    assert "401" in msg


@pytest.mark.asyncio
async def test_openfec_invalid_key():
    with patch.dict("os.environ", {"OPENFEC_API_KEY": "bad"}):
        name, valid, msg = await _check_openfec(_mock_client(403))
    assert valid is False
    assert "403" in msg


@pytest.mark.asyncio
async def test_virustotal_invalid_key():
    with patch.dict("os.environ", {"VIRUSTOTAL_API_KEY": "bad"}):
        name, valid, msg = await _check_virustotal(_mock_client(401))
    assert valid is False


# ------------------------------------------------------------------
# Individual validators — connection error
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_courtlistener_connection_error():
    import httpx

    client = AsyncMock()
    client.get = AsyncMock(side_effect=httpx.ConnectError("timeout"))
    with patch.dict("os.environ", {"COURTLISTENER_API_KEY": "key"}):
        name, valid, msg = await _check_courtlistener(client)
    assert valid is False
    assert "connection error" in msg


# ------------------------------------------------------------------
# validate_api_keys integration
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_validate_api_keys_skips_unconfigured():
    """should skip services with no key when only_configured=True."""
    with patch.dict("os.environ", {}, clear=True):
        results = await validate_api_keys(only_configured=True)
    # All validators should report "not set", which get filtered out
    assert len(results) == 0


@pytest.mark.asyncio
async def test_validate_api_keys_includes_unconfigured():
    """should include all services when only_configured=False."""
    with patch.dict("os.environ", {}, clear=True):
        # Mock httpx.AsyncClient to avoid real network calls
        mock_client = _mock_client(200)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        with patch("osint_agent.key_validator.httpx.AsyncClient", return_value=mock_client):
            results = await validate_api_keys(only_configured=False)
    assert len(results) == 6  # All 6 validators reported


# ------------------------------------------------------------------
# print_validation_report
# ------------------------------------------------------------------

def test_print_validation_report_no_results(capsys):
    print_validation_report([])
    output = capsys.readouterr().err
    assert "No API keys configured" in output


def test_print_validation_report_mixed(capsys):
    results = [
        ("CourtListener", True, "valid"),
        ("OpenFEC", False, "invalid API key (403)"),
    ]
    print_validation_report(results)
    output = capsys.readouterr().err
    assert "OK" in output and "CourtListener" in output
    assert "FAIL" in output and "OpenFEC" in output
    assert "1 key(s) failed" in output


def test_print_validation_report_all_valid(capsys):
    results = [("CourtListener", True, "valid")]
    print_validation_report(results)
    output = capsys.readouterr().err
    assert "OK" in output
    assert "WARNING" not in output
