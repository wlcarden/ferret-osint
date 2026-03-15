"""Tests for the CrossLinked adapter — LinkedIn employee enumeration."""

import subprocess
from unittest.mock import patch

import pytest

from osint_agent.models import EntityType, RelationType
from osint_agent.tools.crosslinked import (
    CrossLinkedAdapter,
    _parse_stdout,
    _slug,
)


@pytest.fixture
def adapter():
    return CrossLinkedAdapter()


# ------------------------------------------------------------------
# Helper functions
# ------------------------------------------------------------------

def test_slug_basic():
    """should normalize name to lowercase underscore slug"""
    assert _slug("Jane Doe") == "jane_doe"


def test_slug_special_characters():
    """should strip non-alphanumeric characters"""
    assert _slug("O'Brien-Smith III") == "o_brien_smith_iii"


def test_slug_extra_whitespace():
    """should collapse whitespace into single underscore"""
    assert _slug("  Jane   Doe  ") == "jane_doe"


def test_parse_stdout_valid_names():
    """should extract two-word names from stdout lines"""
    stdout = "John Smith\nJane Doe\nAlice Johnson\n"
    result = _parse_stdout(stdout)
    assert len(result) == 3
    assert result[0]["name"] == "John Smith"
    assert result[1]["name"] == "Jane Doe"


def test_parse_stdout_skips_headers():
    """should ignore lines starting with [, #, -, ="""
    stdout = "# CrossLinked v1.0\n[*] Searching...\n---\nJohn Smith\n"
    result = _parse_stdout(stdout)
    assert len(result) == 1
    assert result[0]["name"] == "John Smith"


def test_parse_stdout_skips_non_alpha():
    """should reject lines containing non-alpha characters"""
    stdout = "john.smith@company.com\n192.168.1.1\nJohn Smith\n"
    result = _parse_stdout(stdout)
    assert len(result) == 1
    assert result[0]["name"] == "John Smith"


def test_parse_stdout_skips_single_word():
    """should reject single-word lines (not names)"""
    stdout = "Loading\nJohn Smith\n"
    result = _parse_stdout(stdout)
    assert len(result) == 1


def test_parse_stdout_skips_long_lines():
    """should reject lines with more than 5 words"""
    stdout = "This is a very long line indeed\nJohn Smith\n"
    result = _parse_stdout(stdout)
    assert len(result) == 1


def test_parse_stdout_empty():
    """should return empty list for empty/None input"""
    assert _parse_stdout("") == []
    assert _parse_stdout(None) == []


# ------------------------------------------------------------------
# Availability
# ------------------------------------------------------------------

def test_adapter_name(adapter):
    assert adapter.name == "crosslinked"


def test_required_package(adapter):
    assert adapter.required_package == "crosslinked"


# ------------------------------------------------------------------
# Happy path — CSV output parsed
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_parses_csv_output(adapter):
    """should parse CSV file and create org + person entities with WORKS_AT relationships"""
    # Write a CSV file that CrossLinked would produce
    csv_content = "name,title\nJane Doe,Software Engineer\nJohn Smith,Director of Sales\n"

    async def fake_subprocess(cmd, timeout=60):
        # Write CSV to the output file specified in the command
        outfile = cmd[cmd.index("-o") + 1]
        with open(outfile, "w") as f:
            f.write(csv_content)
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout="", stderr="",
        )

    with patch.object(adapter, "run_subprocess", side_effect=fake_subprocess):
        finding = await adapter.run(company="Acme Corp")

    # Should have 1 org + 2 persons = 3 entities
    orgs = [e for e in finding.entities if e.entity_type == EntityType.ORGANIZATION]
    persons = [e for e in finding.entities if e.entity_type == EntityType.PERSON]
    assert len(orgs) == 1
    assert orgs[0].label == "Acme Corp"
    assert orgs[0].id == "organization:linkedin:acme_corp"

    assert len(persons) == 2
    names = {p.label for p in persons}
    assert "Jane Doe" in names
    assert "John Smith" in names

    # Check person properties
    jane = next(p for p in persons if p.label == "Jane Doe")
    assert jane.properties["title"] == "Software Engineer"
    assert jane.properties["source_platform"] == "LinkedIn (via search engine)"
    assert jane.id == "person:linkedin:jane_doe"

    # Should have 2 WORKS_AT relationships
    works_at = [r for r in finding.relationships if r.relation_type == RelationType.WORKS_AT]
    assert len(works_at) == 2
    target_ids = {r.target_id for r in works_at}
    assert target_ids == {orgs[0].id}

    assert "2 employees" in finding.notes


@pytest.mark.asyncio
async def test_run_deduplicates_names(adapter):
    """should deduplicate employees by slug"""
    csv_content = "name,title\nJane Doe,Engineer\nJane Doe,Engineer\njane doe,Manager\n"

    async def fake_subprocess(cmd, timeout=60):
        outfile = cmd[cmd.index("-o") + 1]
        with open(outfile, "w") as f:
            f.write(csv_content)
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout="", stderr="",
        )

    with patch.object(adapter, "run_subprocess", side_effect=fake_subprocess):
        finding = await adapter.run(company="TestCo")

    persons = [e for e in finding.entities if e.entity_type == EntityType.PERSON]
    assert len(persons) == 1
    assert "1 employees" in finding.notes


@pytest.mark.asyncio
async def test_run_no_title(adapter):
    """should handle entries with no title gracefully"""
    csv_content = "name,title\nJane Doe,\n"

    async def fake_subprocess(cmd, timeout=60):
        outfile = cmd[cmd.index("-o") + 1]
        with open(outfile, "w") as f:
            f.write(csv_content)
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout="", stderr="",
        )

    with patch.object(adapter, "run_subprocess", side_effect=fake_subprocess):
        finding = await adapter.run(company="SomeCo")

    persons = [e for e in finding.entities if e.entity_type == EntityType.PERSON]
    assert len(persons) == 1
    # Title should be absent (filtered by the truthy check)
    assert "title" not in persons[0].properties


# ------------------------------------------------------------------
# Fallback — stdout parsing
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_falls_back_to_stdout(adapter):
    """should fall back to stdout parsing when CSV is empty"""
    async def fake_subprocess(cmd, timeout=60):
        # Write an empty CSV file
        outfile = cmd[cmd.index("-o") + 1]
        with open(outfile, "w") as f:
            f.write("")
        return subprocess.CompletedProcess(
            args=cmd, returncode=0,
            stdout="Alice Johnson\nBob Williams\n",
            stderr="",
        )

    with patch.object(adapter, "run_subprocess", side_effect=fake_subprocess):
        finding = await adapter.run(company="FallbackCo")

    persons = [e for e in finding.entities if e.entity_type == EntityType.PERSON]
    assert len(persons) == 2
    names = {p.label for p in persons}
    assert "Alice Johnson" in names
    assert "Bob Williams" in names


# ------------------------------------------------------------------
# Error handling
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_subprocess_failure(adapter):
    """should return notes finding when subprocess raises"""
    with patch.object(
        adapter, "run_subprocess",
        side_effect=RuntimeError("crosslinked not found"),
    ):
        finding = await adapter.run(company="FailCo")

    assert len(finding.entities) == 0
    assert "error" in finding.notes.lower()
    assert "crosslinked not found" in finding.notes


@pytest.mark.asyncio
async def test_run_empty_results(adapter):
    """should return notes finding when no employees found"""
    async def fake_subprocess(cmd, timeout=60):
        outfile = cmd[cmd.index("-o") + 1]
        with open(outfile, "w") as f:
            f.write("name,title\n")  # Headers only, no data
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout="", stderr="",
        )

    with patch.object(adapter, "run_subprocess", side_effect=fake_subprocess):
        finding = await adapter.run(company="GhostCo")

    assert len(finding.entities) == 0
    assert "no employees found" in finding.notes.lower()
    assert "GhostCo" in finding.notes


@pytest.mark.asyncio
async def test_run_csv_only_blank_names(adapter):
    """should treat rows with blank names as empty results"""
    csv_content = "name,title\n,Engineer\n ,Manager\n"

    async def fake_subprocess(cmd, timeout=60):
        outfile = cmd[cmd.index("-o") + 1]
        with open(outfile, "w") as f:
            f.write(csv_content)
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout="", stderr="",
        )

    with patch.object(adapter, "run_subprocess", side_effect=fake_subprocess):
        finding = await adapter.run(company="BlankCo")

    assert len(finding.entities) == 0
    assert "no employees found" in finding.notes.lower()


# ------------------------------------------------------------------
# Source tracking
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sources_set_to_crosslinked(adapter):
    """should tag all entities and relationships with crosslinked source"""
    csv_content = "name,title\nJane Doe,Engineer\n"

    async def fake_subprocess(cmd, timeout=60):
        outfile = cmd[cmd.index("-o") + 1]
        with open(outfile, "w") as f:
            f.write(csv_content)
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout="", stderr="",
        )

    with patch.object(adapter, "run_subprocess", side_effect=fake_subprocess):
        finding = await adapter.run(company="SourceCo")

    for entity in finding.entities:
        assert entity.sources[0].tool == "crosslinked"
    for rel in finding.relationships:
        assert rel.sources[0].tool == "crosslinked"
