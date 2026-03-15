"""Tests for the OpenFEC adapter."""

import inspect
from unittest.mock import AsyncMock, patch

import pytest

from osint_agent.tools.openfec import OpenFECAdapter


def test_openfec_adapter_name():
    adapter = OpenFECAdapter()
    assert adapter.name == "openfec"


def test_run_accepts_employer_and_occupation_params():
    """run() signature must accept employer and occupation kwargs."""
    sig = inspect.signature(OpenFECAdapter.run)
    params = sig.parameters
    assert "employer" in params, "run() missing 'employer' parameter"
    assert "occupation" in params, "run() missing 'occupation' parameter"
    assert params["employer"].default is None
    assert params["occupation"].default is None


def test_search_contributors_accepts_employer_and_occupation():
    """_search_contributors() must accept employer and occupation kwargs."""
    sig = inspect.signature(OpenFECAdapter._search_contributors)
    params = sig.parameters
    assert "employer" in params
    assert "occupation" in params
    assert params["employer"].default is None
    assert params["occupation"].default is None


@pytest.mark.asyncio
async def test_run_passes_filters_to_search_contributors():
    """run() should forward employer/occupation to _search_contributors."""
    adapter = OpenFECAdapter()
    mock_search = AsyncMock(return_value=None)
    with patch.object(adapter, "_search_contributors", mock_search):
        await adapter.run(
            query="John Smith",
            mode="contributors",
            employer="Acme Corp",
            occupation="Engineer",
        )
    mock_search.assert_called_once_with(
        "John Smith", 20, employer="Acme Corp", occupation="Engineer",
    )


@pytest.mark.asyncio
async def test_run_defaults_filters_to_none():
    """run() should pass None for employer/occupation when not provided."""
    adapter = OpenFECAdapter()
    mock_search = AsyncMock(return_value=None)
    with patch.object(adapter, "_search_contributors", mock_search):
        await adapter.run(query="Jane Doe")
    mock_search.assert_called_once_with(
        "Jane Doe", 20, employer=None, occupation=None,
    )


@pytest.mark.asyncio
async def test_committees_mode_ignores_filters():
    """Employer/occupation should not affect committee searches."""
    adapter = OpenFECAdapter()
    mock_committees = AsyncMock(return_value=None)
    with patch.object(adapter, "_search_committees", mock_committees):
        await adapter.run(
            query="PAC Name",
            mode="committees",
            employer="Ignored",
        )
    mock_committees.assert_called_once_with("PAC Name", 20)


@pytest.mark.asyncio
async def test_candidates_mode_ignores_filters():
    """Employer/occupation should not affect candidate searches."""
    adapter = OpenFECAdapter()
    mock_candidates = AsyncMock(return_value=None)
    with patch.object(adapter, "_search_candidates", mock_candidates):
        await adapter.run(
            query="Candidate Name",
            mode="candidates",
            occupation="Ignored",
        )
    mock_candidates.assert_called_once_with("Candidate Name", 20)
