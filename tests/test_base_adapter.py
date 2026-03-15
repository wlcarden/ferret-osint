"""Tests for ToolAdapter — safe_run() error recovery and check_availability() diagnostics."""

import asyncio
from unittest.mock import patch

import httpx
import pytest

from osint_agent.models import ErrorCategory, Finding
from osint_agent.tools.base import ToolAdapter


class _DummyAdapter(ToolAdapter):
    """Adapter stub that delegates run() to a configurable callable."""

    name = "dummy"

    def __init__(self, run_fn=None):
        self._run_fn = run_fn

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


@pytest.mark.asyncio
async def test_safe_run_401_includes_env_key_hint():
    """should include specific env var in auth error when required_env_key is set"""

    class _KeyAdapter(_DummyAdapter):
        required_env_key = "MY_SECRET_KEY"

    async def _raise(**kw):
        raise _make_http_status_error(401)

    adapter = _KeyAdapter(run_fn=_raise)
    finding = await adapter.safe_run()

    assert finding.error.category == ErrorCategory.AUTH
    assert "MY_SECRET_KEY" in finding.error.suggestion


# --- check_availability() diagnostics ---


class _BinaryAdapter(ToolAdapter):
    name = "needs_binary"
    required_binary = "nonexistent_binary_xyz"
    install_hint = "apt install nonexistent"

    async def run(self, **kwargs):
        return Finding(notes="ok")


class _EnvAdapter(ToolAdapter):
    name = "needs_key"
    required_env_key = "FAKE_API_KEY_XYZ"

    async def run(self, **kwargs):
        return Finding(notes="ok")


class _PackageAdapter(ToolAdapter):
    name = "needs_pkg"
    required_package = "nonexistent_package_xyz"
    install_hint = "pip install nonexistent"

    async def run(self, **kwargs):
        return Finding(notes="ok")


class _NoRequirements(ToolAdapter):
    name = "always_ready"

    async def run(self, **kwargs):
        return Finding(notes="ok")


def test_check_availability_missing_binary():
    """should report missing binary with install hint"""
    adapter = _BinaryAdapter()
    ok, reason = adapter.check_availability()
    assert not ok
    assert "nonexistent_binary_xyz" in reason
    assert "apt install" in reason


def test_check_availability_missing_env_key():
    """should report missing env var by name"""
    with patch.dict("os.environ", {}, clear=True):
        adapter = _EnvAdapter()
        ok, reason = adapter.check_availability()
        assert not ok
        assert "FAKE_API_KEY_XYZ" in reason


def test_check_availability_missing_package():
    """should report missing package with install hint"""
    adapter = _PackageAdapter()
    ok, reason = adapter.check_availability()
    assert not ok
    assert "nonexistent_package_xyz" in reason
    assert "pip install" in reason


def test_check_availability_ready():
    """should return ready when no requirements are set"""
    adapter = _NoRequirements()
    ok, reason = adapter.check_availability()
    assert ok
    assert reason == "ready"


def test_is_available_default_binary():
    """should return False when required binary is missing"""
    adapter = _BinaryAdapter()
    assert not adapter.is_available()


def test_is_available_default_env_key():
    """should return False when required env var is missing"""
    with patch.dict("os.environ", {}, clear=True):
        adapter = _EnvAdapter()
        assert not adapter.is_available()


def test_is_available_default_no_requirements():
    """should return True when no requirements"""
    adapter = _NoRequirements()
    assert adapter.is_available()
