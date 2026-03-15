"""Tests for ToolAdapter.safe_run() error recovery."""

import asyncio
from unittest.mock import MagicMock

import httpx
import pytest

from osint_agent.models import ErrorCategory, Finding
from osint_agent.tools.base import ToolAdapter


class _DummyAdapter(ToolAdapter):
    """Adapter stub that delegates run() to a configurable callable."""

    name = "dummy"

    def __init__(self, run_fn=None):
        self._run_fn = run_fn

    def is_available(self) -> bool:
        return True

    async def run(self, **kwargs) -> Finding:
        if self._run_fn:
            return await self._run_fn(**kwargs)
        return Finding(notes="ok")


def _make_http_status_error(status: int, headers: dict | None = None) -> httpx.HTTPStatusError:
    """Build an httpx.HTTPStatusError with a given status code."""
    request = httpx.Request("GET", "https://example.com")
    response = httpx.Response(status, request=request, headers=headers or {})
    return httpx.HTTPStatusError(
        f"HTTP {status}",
        request=request,
        response=response,
    )


# --- safe_run catches HTTPStatusError ---


@pytest.mark.asyncio
async def test_safe_run_catches_http_429():
    async def _raise(**kw):
        raise _make_http_status_error(429, {"Retry-After": "30"})

    adapter = _DummyAdapter(run_fn=_raise)
    finding = await adapter.safe_run()

    assert finding.error is not None
    assert finding.error.category == ErrorCategory.RATE_LIMIT
    assert finding.error.http_status == 429
    assert finding.error.retry_after == 30.0
    assert finding.error.tool == "dummy"


@pytest.mark.asyncio
async def test_safe_run_catches_http_401():
    async def _raise(**kw):
        raise _make_http_status_error(401)

    adapter = _DummyAdapter(run_fn=_raise)
    finding = await adapter.safe_run()

    assert finding.error is not None
    assert finding.error.category == ErrorCategory.AUTH
    assert "API key" in finding.error.suggestion


@pytest.mark.asyncio
async def test_safe_run_catches_http_500():
    async def _raise(**kw):
        raise _make_http_status_error(500)

    adapter = _DummyAdapter(run_fn=_raise)
    finding = await adapter.safe_run()

    assert finding.error is not None
    assert finding.error.category == ErrorCategory.SERVER


# --- safe_run catches network errors ---


@pytest.mark.asyncio
async def test_safe_run_catches_connect_error():
    async def _raise(**kw):
        raise httpx.ConnectError("Connection refused")

    adapter = _DummyAdapter(run_fn=_raise)
    finding = await adapter.safe_run()

    assert finding.error is not None
    assert finding.error.category == ErrorCategory.NETWORK
    assert "network" in finding.notes


@pytest.mark.asyncio
async def test_safe_run_catches_read_timeout():
    async def _raise(**kw):
        raise httpx.ReadTimeout("Read timed out")

    adapter = _DummyAdapter(run_fn=_raise)
    finding = await adapter.safe_run()

    assert finding.error is not None
    assert finding.error.category == ErrorCategory.NETWORK


# --- safe_run catches asyncio.TimeoutError ---


@pytest.mark.asyncio
async def test_safe_run_catches_timeout_error():
    async def _raise(**kw):
        raise asyncio.TimeoutError()

    adapter = _DummyAdapter(run_fn=_raise)
    finding = await adapter.safe_run()

    assert finding.error is not None
    assert finding.error.category == ErrorCategory.TIMEOUT
    assert "timed out" in finding.notes


# --- safe_run catches generic Exception ---


@pytest.mark.asyncio
async def test_safe_run_catches_generic_exception():
    async def _raise(**kw):
        raise ValueError("unexpected parse failure")

    adapter = _DummyAdapter(run_fn=_raise)
    finding = await adapter.safe_run()

    assert finding.error is not None
    assert finding.error.category == ErrorCategory.UNKNOWN
    assert "ValueError" in finding.error.message
    assert "unexpected parse failure" in finding.error.message


# --- safe_run passes through normal findings ---


@pytest.mark.asyncio
async def test_safe_run_passes_through_success():
    adapter = _DummyAdapter()
    finding = await adapter.safe_run()

    assert finding.error is None
    assert finding.notes == "ok"


@pytest.mark.asyncio
async def test_safe_run_passes_through_finding_with_error():
    """If run() itself returns a Finding with an error field, safe_run() returns it as-is."""
    from osint_agent.models import ToolError

    err = ToolError(
        tool="dummy",
        category=ErrorCategory.NOT_FOUND,
        message="no results",
    )

    async def _return_error(**kw):
        return Finding(notes="dummy: no results", error=err)

    adapter = _DummyAdapter(run_fn=_return_error)
    finding = await adapter.safe_run()

    assert finding.error is not None
    assert finding.error.category == ErrorCategory.NOT_FOUND


# --- safe_run preserves tool name ---


@pytest.mark.asyncio
async def test_safe_run_error_includes_tool_name():
    async def _raise(**kw):
        raise RuntimeError("boom")

    adapter = _DummyAdapter(run_fn=_raise)
    finding = await adapter.safe_run()

    assert finding.error.tool == "dummy"
