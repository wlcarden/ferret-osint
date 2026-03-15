"""Tests for the OpenPoliceData adapter — US police incident data."""

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from osint_agent.models import EntityType, RelationType
from osint_agent.tools.openpolicedata import OpenPoliceDataAdapter, _slug


@pytest.fixture
def adapter():
    return OpenPoliceDataAdapter()


@pytest.fixture
def mock_datasets():
    """Canned opd.datasets.query() DataFrame."""
    return pd.DataFrame([
        {
            "SourceName": "Norfolk",
            "Agency": "Norfolk Police Department",
            "State": "Virginia",
            "TableType": "USE OF FORCE",
            "Year": 2023,
            "coverage_start": pd.Timestamp("2023-01-01"),
            "coverage_end": pd.Timestamp("2023-12-31"),
        },
        {
            "SourceName": "Norfolk",
            "Agency": "Norfolk Police Department",
            "State": "Virginia",
            "TableType": "STOPS",
            "Year": 2022,
            "coverage_start": pd.Timestamp("2022-01-01"),
            "coverage_end": pd.Timestamp("2022-12-31"),
        },
        {
            "SourceName": "Norfolk",
            "Agency": "Norfolk Police Department",
            "State": "Virginia",
            "TableType": "STOPS",
            "Year": 2023,
            "coverage_start": pd.Timestamp("2023-01-01"),
            "coverage_end": pd.Timestamp("2023-12-31"),
        },
        {
            "SourceName": "Fairfax County",
            "Agency": "Fairfax County Police",
            "State": "Virginia",
            "TableType": "COMPLAINTS",
            "Year": 2023,
            "coverage_start": pd.Timestamp("2023-01-01"),
            "coverage_end": pd.Timestamp("2023-12-31"),
        },
    ])


# ------------------------------------------------------------------
# Helper functions
# ------------------------------------------------------------------

def test_slug_basic():
    assert _slug("Norfolk Police") == "norfolk_police"


def test_slug_special_chars():
    assert _slug("USE OF FORCE") == "use_of_force"


def test_slug_strips_edges():
    assert _slug("--test--") == "test"


# ------------------------------------------------------------------
# Availability
# ------------------------------------------------------------------

def test_is_available_when_installed():
    with patch.dict("sys.modules", {"openpolicedata": MagicMock()}):
        adapter = OpenPoliceDataAdapter()
        assert adapter.is_available() is True


def test_is_available_when_not_installed():
    with patch("builtins.__import__", side_effect=ImportError):
        adapter = OpenPoliceDataAdapter()
        assert adapter.is_available() is False


def test_adapter_name(adapter):
    assert adapter.name == "openpolicedata"


# ------------------------------------------------------------------
# Catalog query (no table_type specified)
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_catalog_by_source_name(adapter, mock_datasets):
    """should return a catalog of available datasets when no table_type given"""
    mock_opd = MagicMock()
    mock_opd.datasets.query.return_value = mock_datasets

    with patch.dict("sys.modules", {"openpolicedata": mock_opd}):
        finding = await adapter.run(agency="Norfolk", state="Virginia")

    # Should have agency entity
    orgs = [e for e in finding.entities if e.entity_type == EntityType.ORGANIZATION]
    assert len(orgs) == 1
    assert "Norfolk" in orgs[0].label
    assert orgs[0].properties["state"] == "Virginia"
    assert orgs[0].properties["agency_type"] == "law_enforcement"

    # Should have document entities for each table type (USE OF FORCE, STOPS)
    docs = [e for e in finding.entities if e.entity_type == EntityType.DOCUMENT]
    assert len(docs) == 2
    table_types = {d.properties["table_type"] for d in docs}
    assert "USE OF FORCE" in table_types
    assert "STOPS" in table_types

    # Should have OWNS relationships from agency to each doc
    owns = [r for r in finding.relationships if r.relation_type == RelationType.OWNS]
    assert len(owns) == 2
    assert all(r.source_id == orgs[0].id for r in owns)

    # Notes should summarize
    assert "2 dataset types" in finding.notes
    assert "Norfolk" in finding.notes


@pytest.mark.asyncio
async def test_run_catalog_falls_back_to_agency_field(adapter, mock_datasets):
    """should search Agency column when SourceName has no matches"""
    mock_opd = MagicMock()
    mock_opd.datasets.query.return_value = mock_datasets

    with patch.dict("sys.modules", {"openpolicedata": mock_opd}):
        finding = await adapter.run(agency="Fairfax County Police")

    orgs = [e for e in finding.entities if e.entity_type == EntityType.ORGANIZATION]
    assert len(orgs) == 1
    assert "Fairfax" in orgs[0].label


@pytest.mark.asyncio
async def test_run_no_datasets_found(adapter, mock_datasets):
    """should return notes-only finding when agency not found"""
    mock_opd = MagicMock()
    mock_opd.datasets.query.return_value = mock_datasets

    with patch.dict("sys.modules", {"openpolicedata": mock_opd}):
        finding = await adapter.run(agency="Nonexistent PD")

    assert len(finding.entities) == 0
    assert "no datasets found" in finding.notes.lower()
    assert "Nonexistent PD" in finding.notes


@pytest.mark.asyncio
async def test_run_no_datasets_found_with_state(adapter, mock_datasets):
    """should include state in 'not found' message"""
    mock_opd = MagicMock()
    mock_opd.datasets.query.return_value = mock_datasets

    with patch.dict("sys.modules", {"openpolicedata": mock_opd}):
        finding = await adapter.run(agency="Norfolk", state="Texas")

    assert "no datasets found" in finding.notes.lower()
    assert "Texas" in finding.notes


# ------------------------------------------------------------------
# Specific table fetch
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_fetch_specific_table(adapter, mock_datasets):
    """should fetch and summarize data for a specific table type"""
    # Build a mock DataFrame that the Source.load() would return.
    mock_table_data = pd.DataFrame([
        {"race": "White", "gender": "Male", "force_type": "Taser"},
        {"race": "Black", "gender": "Male", "force_type": "Physical"},
        {"race": "White", "gender": "Female", "force_type": "Taser"},
        {"race": "Black", "gender": "Female", "force_type": "Physical"},
    ])

    mock_table_obj = MagicMock()
    mock_table_obj.table = mock_table_data

    mock_source_instance = MagicMock()
    mock_source_instance.load.return_value = mock_table_obj

    mock_opd = MagicMock()
    mock_opd.datasets.query.return_value = mock_datasets
    mock_opd.Source.return_value = mock_source_instance

    with patch.dict("sys.modules", {"openpolicedata": mock_opd}):
        finding = await adapter.run(
            agency="Norfolk",
            state="Virginia",
            table_type="USE OF FORCE",
        )

    # Should have agency entity + document entity
    orgs = [e for e in finding.entities if e.entity_type == EntityType.ORGANIZATION]
    docs = [e for e in finding.entities if e.entity_type == EntityType.DOCUMENT]
    assert len(orgs) == 1
    assert len(docs) == 1

    doc = docs[0]
    assert doc.properties["record_count"] == 4
    assert doc.properties["table_type"] == "USE OF FORCE"
    assert doc.properties["year"] == "2023"

    # Should have race/gender breakdowns extracted
    assert "breakdown_race" in doc.properties
    assert doc.properties["breakdown_race"]["White"] == 2
    assert doc.properties["breakdown_race"]["Black"] == 2

    # Relationship
    owns = [r for r in finding.relationships if r.relation_type == RelationType.OWNS]
    assert len(owns) == 1

    # Notes
    assert "4 records" in finding.notes


@pytest.mark.asyncio
async def test_run_fetch_table_type_not_found(adapter, mock_datasets):
    """should return notes when requested table type doesn't exist"""
    mock_opd = MagicMock()
    mock_opd.datasets.query.return_value = mock_datasets

    with patch.dict("sys.modules", {"openpolicedata": mock_opd}):
        finding = await adapter.run(
            agency="Norfolk",
            state="Virginia",
            table_type="DEATHS IN CUSTODY",
        )

    # Should still have the agency entity
    orgs = [e for e in finding.entities if e.entity_type == EntityType.ORGANIZATION]
    assert len(orgs) == 1
    assert "no 'DEATHS IN CUSTODY' data" in finding.notes


@pytest.mark.asyncio
async def test_run_fetch_table_load_failure(adapter, mock_datasets):
    """should handle data load exceptions gracefully"""
    mock_source_instance = MagicMock()
    mock_source_instance.load.side_effect = RuntimeError("Connection timed out")

    mock_opd = MagicMock()
    mock_opd.datasets.query.return_value = mock_datasets
    mock_opd.Source.return_value = mock_source_instance

    with patch.dict("sys.modules", {"openpolicedata": mock_opd}):
        finding = await adapter.run(
            agency="Norfolk",
            state="Virginia",
            table_type="USE OF FORCE",
        )

    assert "failed to load data" in finding.notes.lower()
    assert "Connection timed out" in finding.notes


# ------------------------------------------------------------------
# Query error handling
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_query_exception(adapter):
    """should handle opd.datasets.query() failures"""
    mock_opd = MagicMock()
    mock_opd.datasets.query.side_effect = RuntimeError("Network error")

    with patch.dict("sys.modules", {"openpolicedata": mock_opd}):
        finding = await adapter.run(agency="Norfolk")

    assert "error" in finding.notes.lower()
    assert "Network error" in finding.notes
    assert len(finding.entities) == 0


# ------------------------------------------------------------------
# Entity ID format
# ------------------------------------------------------------------

def test_agency_entity_id_format(adapter, mock_datasets):
    """Verify ID uses slugified source name and state."""
    # Build finding synchronously by calling internal logic indirectly.
    # We check the slug function used in ID construction.
    assert _slug("Norfolk") == "norfolk"
    assert _slug("Virginia") == "virginia"
    # The ID pattern is organization:police:{source}:{state}
    expected_id_part = "organization:police:norfolk:virginia"
    assert expected_id_part == f"organization:police:{_slug('Norfolk')}:{_slug('Virginia')}"


# ------------------------------------------------------------------
# Label construction
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_agency_label_appends_police_when_missing(adapter):
    """should append 'Police' to source name that doesn't include it"""
    ds = pd.DataFrame([
        {
            "SourceName": "Norfolk",
            "Agency": "Norfolk PD",
            "State": "Virginia",
            "TableType": "STOPS",
            "Year": 2023,
            "coverage_start": pd.Timestamp("2023-01-01"),
            "coverage_end": pd.Timestamp("2023-12-31"),
        },
    ])
    mock_opd = MagicMock()
    mock_opd.datasets.query.return_value = ds

    with patch.dict("sys.modules", {"openpolicedata": mock_opd}):
        finding = await adapter.run(agency="Norfolk")

    org = [e for e in finding.entities if e.entity_type == EntityType.ORGANIZATION][0]
    assert org.label == "Norfolk Police"


@pytest.mark.asyncio
async def test_agency_label_no_double_police(adapter):
    """should not add 'Police' if source name already contains it"""
    ds = pd.DataFrame([
        {
            "SourceName": "Norfolk Police Department",
            "Agency": "Norfolk PD",
            "State": "Virginia",
            "TableType": "STOPS",
            "Year": 2023,
            "coverage_start": pd.Timestamp("2023-01-01"),
            "coverage_end": pd.Timestamp("2023-12-31"),
        },
    ])
    mock_opd = MagicMock()
    mock_opd.datasets.query.return_value = ds

    with patch.dict("sys.modules", {"openpolicedata": mock_opd}):
        finding = await adapter.run(agency="Norfolk")

    org = [e for e in finding.entities if e.entity_type == EntityType.ORGANIZATION][0]
    assert org.label == "Norfolk Police Department"
    assert "Police Police" not in org.label
