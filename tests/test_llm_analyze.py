"""Tests for multi-provider LLM analysis — provider detection, API calls, parsing."""

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from osint_agent.llm_analyze import (
    PROVIDERS,
    _parse_llm_response,
    analyze_via_api,
    detect_provider,
)


# ------------------------------------------------------------------
# Provider detection
# ------------------------------------------------------------------


def test_detect_anthropic():
    """should detect anthropic when ANTHROPIC_API_KEY is set"""
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}, clear=True):
        assert detect_provider() == "anthropic"


def test_detect_openai():
    """should detect openai when OPENAI_API_KEY is set"""
    with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=True):
        assert detect_provider() == "openai"


def test_detect_openrouter():
    """should detect openrouter when OPENROUTER_API_KEY is set"""
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-test"}, clear=True):
        assert detect_provider() == "openrouter"


def test_detect_local():
    """should detect local when LLM_BASE_URL is set"""
    with patch.dict(os.environ, {"LLM_BASE_URL": "http://localhost:11434/v1"}, clear=True):
        assert detect_provider() == "local"


def test_detect_priority():
    """should prefer anthropic over openai when both are set"""
    with patch.dict(
        os.environ,
        {"ANTHROPIC_API_KEY": "sk-a", "OPENAI_API_KEY": "sk-o"},
        clear=True,
    ):
        assert detect_provider() == "anthropic"


def test_detect_none_raises():
    """should raise RuntimeError when no provider env vars are set"""
    with patch.dict(os.environ, {}, clear=True):
        with pytest.raises(RuntimeError, match="No LLM provider detected"):
            detect_provider()


# ------------------------------------------------------------------
# Response parsing
# ------------------------------------------------------------------

_VALID_JSON = json.dumps({
    "extracted_entities": [],
    "extracted_relationships": [],
    "extracted_leads": [],
    "analysis_notes": "Nothing found.",
})


def test_parse_clean_json():
    """should pass through clean JSON unchanged"""
    result = _parse_llm_response(_VALID_JSON)
    assert json.loads(result) == json.loads(_VALID_JSON)


def test_parse_strips_markdown_fences():
    """should strip ```json ... ``` fences"""
    wrapped = f"```json\n{_VALID_JSON}\n```"
    result = _parse_llm_response(wrapped)
    assert json.loads(result) == json.loads(_VALID_JSON)


def test_parse_strips_plain_fences():
    """should strip plain ``` fences without language tag"""
    wrapped = f"```\n{_VALID_JSON}\n```"
    result = _parse_llm_response(wrapped)
    assert json.loads(result) == json.loads(_VALID_JSON)


def test_parse_invalid_json_raises():
    """should raise RuntimeError for invalid JSON"""
    with pytest.raises(RuntimeError, match="LLM returned invalid JSON"):
        _parse_llm_response("This is not JSON at all")


# ------------------------------------------------------------------
# Full pipeline (mocked LLM calls)
# ------------------------------------------------------------------


_MOCK_EXPORT = json.dumps({
    "meta": {"entity_count": 5, "relationship_count": 2},
    "entities": [],
    "relationships": [],
    "schema_reference": {},
})

_MOCK_LLM_RESPONSE = json.dumps({
    "extracted_entities": [],
    "extracted_relationships": [],
    "extracted_leads": [],
    "analysis_notes": "Test analysis complete.",
})

_MOCK_INGEST_RESULT = {
    "entities": 0,
    "relationships": 0,
    "leads": 0,
    "errors": 0,
}


@pytest.mark.asyncio
async def test_analyze_anthropic_provider():
    """should call Anthropic SDK when provider is anthropic"""
    mock_store = MagicMock()

    with (
        patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}),
        patch(
            "osint_agent.llm_analyze.export_investigation",
            new_callable=AsyncMock,
            return_value=_MOCK_EXPORT,
        ),
        patch(
            "osint_agent.llm_analyze._call_anthropic",
            return_value=_MOCK_LLM_RESPONSE,
        ),
        patch(
            "osint_agent.llm_analyze.ingest_extraction",
            new_callable=AsyncMock,
            return_value=_MOCK_INGEST_RESULT,
        ),
    ):
        result = await analyze_via_api(
            mock_store,
            provider="anthropic",
            model="claude-test",
        )
        assert result == _MOCK_INGEST_RESULT


@pytest.mark.asyncio
async def test_analyze_openai_provider():
    """should call OpenAI-compatible endpoint when provider is openai"""
    mock_store = MagicMock()

    with (
        patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}),
        patch(
            "osint_agent.llm_analyze.export_investigation",
            new_callable=AsyncMock,
            return_value=_MOCK_EXPORT,
        ),
        patch(
            "osint_agent.llm_analyze._call_openai_compat",
            new_callable=AsyncMock,
            return_value=_MOCK_LLM_RESPONSE,
        ),
        patch(
            "osint_agent.llm_analyze.ingest_extraction",
            new_callable=AsyncMock,
            return_value=_MOCK_INGEST_RESULT,
        ),
    ):
        result = await analyze_via_api(
            mock_store,
            provider="openai",
        )
        assert result == _MOCK_INGEST_RESULT


@pytest.mark.asyncio
async def test_analyze_local_no_key_required():
    """should not require an API key for local provider"""
    mock_store = MagicMock()

    with (
        patch.dict(os.environ, {}, clear=True),
        patch(
            "osint_agent.llm_analyze.export_investigation",
            new_callable=AsyncMock,
            return_value=_MOCK_EXPORT,
        ),
        patch(
            "osint_agent.llm_analyze._call_openai_compat",
            new_callable=AsyncMock,
            return_value=_MOCK_LLM_RESPONSE,
        ),
        patch(
            "osint_agent.llm_analyze.ingest_extraction",
            new_callable=AsyncMock,
            return_value=_MOCK_INGEST_RESULT,
        ),
    ):
        result = await analyze_via_api(
            mock_store,
            provider="local",
            base_url="http://localhost:11434/v1",
        )
        assert result == _MOCK_INGEST_RESULT


@pytest.mark.asyncio
async def test_analyze_unknown_provider_raises():
    """should raise RuntimeError for unknown provider"""
    mock_store = MagicMock()
    with pytest.raises(RuntimeError, match="Unknown provider"):
        await analyze_via_api(mock_store, provider="magic")


@pytest.mark.asyncio
async def test_analyze_missing_key_raises():
    """should raise RuntimeError when required API key is missing"""
    mock_store = MagicMock()
    with patch.dict(os.environ, {}, clear=True):
        with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
            await analyze_via_api(mock_store, provider="openai")


@pytest.mark.asyncio
async def test_analyze_auto_detects_provider():
    """should auto-detect provider when not specified"""
    mock_store = MagicMock()

    with (
        patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-test"}, clear=True),
        patch(
            "osint_agent.llm_analyze.export_investigation",
            new_callable=AsyncMock,
            return_value=_MOCK_EXPORT,
        ),
        patch(
            "osint_agent.llm_analyze._call_openai_compat",
            new_callable=AsyncMock,
            return_value=_MOCK_LLM_RESPONSE,
        ) as mock_call,
        patch(
            "osint_agent.llm_analyze.ingest_extraction",
            new_callable=AsyncMock,
            return_value=_MOCK_INGEST_RESULT,
        ),
    ):
        await analyze_via_api(mock_store)
        mock_call.assert_called_once()


@pytest.mark.asyncio
async def test_analyze_base_url_override():
    """should use custom base_url when provided"""
    mock_store = MagicMock()
    custom_url = "http://my-server:8080/v1"

    with (
        patch.dict(os.environ, {}, clear=True),
        patch(
            "osint_agent.llm_analyze.export_investigation",
            new_callable=AsyncMock,
            return_value=_MOCK_EXPORT,
        ),
        patch(
            "osint_agent.llm_analyze._call_openai_compat",
            new_callable=AsyncMock,
            return_value=_MOCK_LLM_RESPONSE,
        ) as mock_call,
        patch(
            "osint_agent.llm_analyze.ingest_extraction",
            new_callable=AsyncMock,
            return_value=_MOCK_INGEST_RESULT,
        ),
    ):
        await analyze_via_api(
            mock_store,
            provider="local",
            base_url=custom_url,
        )
        # base_url should be passed through to the call
        call_args = mock_call.call_args
        assert call_args[0][1] == custom_url  # second positional arg is base_url


def test_provider_config_completeness():
    """should have all required keys in every provider config"""
    required_keys = {"env_key", "base_url", "default_model"}
    for name, cfg in PROVIDERS.items():
        missing = required_keys - set(cfg.keys())
        assert not missing, f"Provider {name} missing keys: {missing}"
