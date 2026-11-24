"""Tests for the PhoneInfoga adapter — phone number intelligence.

Fixtures below mirror the plain-text output of PhoneInfoga v2.11.0. The v1
``--output json`` flag no longer exists; passing it makes the binary exit
non-zero with "unknown flag: --output", so the adapter must not send it.
"""

import subprocess
from unittest.mock import patch

import pytest

from osint_agent.models import EntityType
from osint_agent.tools.phoneinfoga import PhoneInfogaAdapter


@pytest.fixture
def adapter():
    return PhoneInfogaAdapter()


@pytest.fixture
def v2_stdout():
    """Captured shape of a real `phoneinfoga scan -n +14155552671` run."""
    return (
        "Running scan for phone number +14155552671...\n"
        "\n"
        "Results for googlesearch\n"
        "Social media:\n"
        "\tURL: https://www.google.com/search?q=site%3Afacebook.com\n"
        "\n"
        "\tURL: https://www.google.com/search?q=site%3Atwitter.com\n"
        "Disposable providers:\n"
        "\tURL: https://www.google.com/search?q=site%3Ahs3x.com\n"
        "Reputation:\n"
        "\tURL: https://www.google.com/search?q=site%3Awhosenumber.info\n"
        "\n"
        "Results for local\n"
        "Raw local: 4155552671\n"
        "Local: (415) 555-2671\n"
        "E164: +14155552671\n"
        "International: 14155552671\n"
        "Country: US\n"
        "2 scanner(s) succeeded\n"
    )


@pytest.fixture
def v2_stdout_with_numverify():
    """Output when the numverify plugin is enabled — adds carrier/line type."""
    return (
        "Running scan for phone number +12025559876...\n"
        "\n"
        "Results for local\n"
        "Raw local: 2025559876\n"
        "Local: (202) 555-9876\n"
        "E164: +12025559876\n"
        "Country: US\n"
        "\n"
        "Results for numverify\n"
        "Valid: true\n"
        "Carrier: Verizon Wireless\n"
        "Line type: mobile\n"
        "Country: US\n"
        "2 scanner(s) succeeded\n"
    )


def _completed(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(
        args=["phoneinfoga"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


# ------------------------------------------------------------------
# Availability and metadata
# ------------------------------------------------------------------

def test_adapter_name(adapter):
    assert adapter.name == "phoneinfoga"


def test_required_binary(adapter):
    assert adapter.required_binary == "phoneinfoga"


# ------------------------------------------------------------------
# Invocation contract — regression guard for the v1/v2 flag break
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_does_not_pass_removed_output_flag(adapter, v2_stdout):
    """should not send --output, which v2 rejects with 'unknown flag'"""
    with patch.object(
        adapter, "run_subprocess", return_value=_completed(v2_stdout),
    ) as mock_run:
        await adapter.run(phone_number="+14155552671")

    argv = mock_run.call_args[0][0]
    assert "--output" not in argv
    assert "json" not in argv


@pytest.mark.asyncio
async def test_run_invokes_scan_subcommand(adapter, v2_stdout):
    """should invoke `phoneinfoga scan -n <number>`"""
    with patch.object(
        adapter, "run_subprocess", return_value=_completed(v2_stdout),
    ) as mock_run:
        await adapter.run(phone_number="+14155552671")

    argv = mock_run.call_args[0][0]
    assert argv[:3] == ["phoneinfoga", "scan", "-n"]
    assert argv[3] == "+14155552671"


# ------------------------------------------------------------------
# Happy path — v2 text parsing
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_parses_local_scanner_fields(adapter, v2_stdout):
    """should extract formatting and country from the local scanner"""
    with patch.object(adapter, "run_subprocess", return_value=_completed(v2_stdout)):
        finding = await adapter.run(phone_number="+14155552671")

    assert len(finding.entities) == 1
    phone = finding.entities[0]

    assert phone.entity_type == EntityType.PHONE
    assert phone.id == "phone:+14155552671"
    assert phone.properties["raw_number"] == "+14155552671"
    assert phone.properties["source_system"] == "phoneinfoga"
    assert phone.properties["raw_local"] == "4155552671"
    assert phone.properties["local_format"] == "(415) 555-2671"
    assert phone.properties["e164"] == "+14155552671"
    assert phone.properties["international_format"] == "14155552671"
    assert phone.properties["country"] == "US"


@pytest.mark.asyncio
async def test_run_labels_entity_with_e164(adapter, v2_stdout):
    """should prefer the E164 form as the entity label"""
    with patch.object(adapter, "run_subprocess", return_value=_completed(v2_stdout)):
        finding = await adapter.run(phone_number="+14155552671")

    assert finding.entities[0].label == "+14155552671"


@pytest.mark.asyncio
async def test_run_collects_search_dorks(adapter, v2_stdout):
    """should collect the googlesearch scanner's prebuilt dork URLs"""
    with patch.object(adapter, "run_subprocess", return_value=_completed(v2_stdout)):
        finding = await adapter.run(phone_number="+14155552671")

    dorks = finding.entities[0].properties["search_dorks"]
    assert len(dorks) == 4
    assert all(d.startswith("https://www.google.com/search?") for d in dorks)


@pytest.mark.asyncio
async def test_run_records_scanner_names(adapter, v2_stdout):
    """should record which scanners produced sections"""
    with patch.object(adapter, "run_subprocess", return_value=_completed(v2_stdout)):
        finding = await adapter.run(phone_number="+14155552671")

    assert finding.entities[0].properties["scanners"] == ["googlesearch", "local"]


@pytest.mark.asyncio
async def test_run_dork_urls_not_treated_as_kv_fields(adapter, v2_stdout):
    """should not fold 'URL:' lines into scanner key/value properties"""
    with patch.object(adapter, "run_subprocess", return_value=_completed(v2_stdout)):
        finding = await adapter.run(phone_number="+14155552671")

    assert "URL" not in finding.entities[0].properties
    assert "url" not in finding.entities[0].properties


# ------------------------------------------------------------------
# numverify enrichment
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_extracts_carrier_when_numverify_present(
    adapter, v2_stdout_with_numverify,
):
    """should extract carrier and line type when the numverify plugin ran"""
    with patch.object(
        adapter, "run_subprocess", return_value=_completed(v2_stdout_with_numverify),
    ):
        finding = await adapter.run(phone_number="+12025559876")

    phone = finding.entities[0]
    assert phone.properties["carrier"] == "Verizon Wireless"
    assert phone.properties["line_type"] == "mobile"
    assert phone.properties["valid"] == "true"
    assert "Verizon Wireless" in finding.notes


@pytest.mark.asyncio
async def test_run_notes_flag_missing_carrier(adapter, v2_stdout):
    """should say carrier needs the numverify plugin when absent"""
    with patch.object(adapter, "run_subprocess", return_value=_completed(v2_stdout)):
        finding = await adapter.run(phone_number="+14155552671")

    assert "numverify" in finding.notes
    assert "US" in finding.notes


# ------------------------------------------------------------------
# Degenerate output
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_no_scanner_sections(adapter):
    """should still emit a PHONE entity when no scanner section parses"""
    with patch.object(
        adapter, "run_subprocess", return_value=_completed("Running scan...\nDone.\n"),
    ):
        finding = await adapter.run(phone_number="+10000000000")

    assert len(finding.entities) == 1
    phone = finding.entities[0]
    assert phone.properties["raw_number"] == "+10000000000"
    assert phone.label == "+10000000000"
    assert "carrier" not in phone.properties
    assert "country" not in phone.properties


@pytest.mark.asyncio
async def test_run_empty_stdout(adapter):
    """should handle completely empty stdout without raising"""
    with patch.object(adapter, "run_subprocess", return_value=_completed("")):
        finding = await adapter.run(phone_number="+10000000000")

    assert len(finding.entities) == 1
    assert finding.entities[0].label == "+10000000000"


@pytest.mark.asyncio
async def test_run_omits_empty_dork_list(adapter):
    """should not set search_dorks when the googlesearch scanner found none"""
    stdout = "Results for local\nCountry: US\n1 scanner(s) succeeded\n"
    with patch.object(adapter, "run_subprocess", return_value=_completed(stdout)):
        finding = await adapter.run(phone_number="+10000000000")

    assert "search_dorks" not in finding.entities[0].properties


# ------------------------------------------------------------------
# Error handling
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_nonzero_returncode(adapter):
    """should return notes finding when phoneinfoga exits with error"""
    with patch.object(
        adapter,
        "run_subprocess",
        return_value=_completed(stderr="Error: invalid phone number format", returncode=1),
    ):
        finding = await adapter.run(phone_number="not-a-phone")

    assert len(finding.entities) == 0
    assert "failed" in finding.notes.lower()
    assert "invalid phone number" in finding.notes.lower()


@pytest.mark.asyncio
async def test_run_unknown_flag_surfaces_as_failure(adapter):
    """should report the v2 'unknown flag' rejection rather than swallow it"""
    with patch.object(
        adapter,
        "run_subprocess",
        return_value=_completed(stderr="unknown flag: --output", returncode=1),
    ):
        finding = await adapter.run(phone_number="+14155552671")

    assert len(finding.entities) == 0
    assert "unknown flag" in finding.notes


@pytest.mark.asyncio
async def test_run_stderr_truncated(adapter):
    """should truncate long stderr in failure notes"""
    with patch.object(
        adapter, "run_subprocess", return_value=_completed(stderr="X" * 1000, returncode=1),
    ):
        finding = await adapter.run(phone_number="+14155552671")

    assert len(finding.notes) < 600


# ------------------------------------------------------------------
# Section splitting internals
# ------------------------------------------------------------------

def test_split_sections_groups_by_scanner():
    """should key each scanner block by its lowercased name"""
    from osint_agent.tools.phoneinfoga import _split_sections

    sections = _split_sections(
        "Results for local\nCountry: US\n\nResults for numverify\nCarrier: AT&T\n",
    )
    assert sections["local"]["Country"] == "US"
    assert sections["numverify"]["Carrier"] == "AT&T"


def test_split_sections_ignores_preamble():
    """should discard lines before the first scanner header"""
    from osint_agent.tools.phoneinfoga import _split_sections

    sections = _split_sections("Running scan...\nNoise: value\nResults for local\nCountry: US\n")
    assert list(sections) == ["local"]
    assert "Noise" not in sections["local"]


def test_extract_dork_urls_strips_whitespace():
    """should pull URLs out of tab-indented URL lines"""
    from osint_agent.tools.phoneinfoga import _extract_dork_urls

    urls = _extract_dork_urls("\tURL: https://example.com/a\n\n\tURL: https://example.com/b\n")
    assert urls == ["https://example.com/a", "https://example.com/b"]


def test_extract_dork_urls_empty_when_absent():
    """should return an empty list when no URL lines are present"""
    from osint_agent.tools.phoneinfoga import _extract_dork_urls

    assert _extract_dork_urls("Results for local\nCountry: US\n") == []
