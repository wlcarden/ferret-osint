"""LLM-powered analysis — multi-provider support.

Alternative to the Claude Code agent path (.claude/agents/analyze.md).
Calls an LLM API to analyze investigation data and extract implicit entities,
relationships, and leads. Uses the same export/ingest pipeline as the agent
path — only the reasoning step differs.

Supported providers (auto-detected from environment variables):
- anthropic:   ANTHROPIC_API_KEY   — Anthropic Messages API
- openai:      OPENAI_API_KEY      — OpenAI Chat Completions API
- openrouter:  OPENROUTER_API_KEY  — OpenRouter (200+ models, OpenAI-compatible)
- local:       LLM_BASE_URL        — Any OpenAI-compatible server (Ollama, LM Studio, vLLM)

Override with --provider, --model, --base-url CLI flags.
"""

import json
import os
import tempfile

import httpx

from osint_agent.graph.sqlite_store import SqliteStore
from osint_agent.llm_export import export_investigation, ingest_extraction

# -- Provider configuration --------------------------------------------------

PROVIDERS = {
    "anthropic": {
        "env_key": "ANTHROPIC_API_KEY",
        "base_url": None,  # uses SDK, not HTTP
        "default_model": "claude-sonnet-4-20250514",
    },
    "openai": {
        "env_key": "OPENAI_API_KEY",
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o",
    },
    "openrouter": {
        "env_key": "OPENROUTER_API_KEY",
        "base_url": "https://openrouter.ai/api/v1",
        "default_model": "anthropic/claude-sonnet-4-20250514",
    },
    "local": {
        "env_key": None,
        "base_url": "http://localhost:11434/v1",  # Ollama default
        "default_model": "llama3",
    },
}


def detect_provider() -> str:
    """Auto-detect provider from environment variables.

    Priority: anthropic > openai > openrouter > local (if LLM_BASE_URL set).
    Raises RuntimeError if nothing is detected.
    """
    for name, cfg in PROVIDERS.items():
        if cfg["env_key"] and os.environ.get(cfg["env_key"]):
            return name
    if os.environ.get("LLM_BASE_URL"):
        return "local"
    raise RuntimeError(
        "No LLM provider detected. Set one of: "
        "ANTHROPIC_API_KEY, OPENAI_API_KEY, OPENROUTER_API_KEY, or LLM_BASE_URL"
    )


# -- System prompt (shared across all providers) -----------------------------

_SYSTEM_PROMPT = """\
You are an OSINT analyst reviewing investigation data. Your job is to find what \
automated tools missed: implicit entities, unstated relationships, cross-source \
correlations, and high-value follow-up leads.

Analyze the investigation data provided and look for:

**Implicit entities** — Property values that should be their own graph nodes:
- An `employer` or `company` property on a PERSON with no corresponding ORGANIZATION entity
- An `address` that appears on multiple entities but has no ADDRESS entity
- A `phone` or `email` in properties not modeled as a separate PHONE/EMAIL entity
- A person mentioned in relationship properties (e.g., `treasurer_name`) with no PERSON entity

**Implicit relationships** — Connections present in the data but not modeled as edges:
- Two PERSON entities sharing the same `employer` value
- Two entities with the same `address` or `city+state` combination
- A PERSON entity whose `employer` matches an existing ORGANIZATION entity label
- An ACCOUNT username matching a USERNAME entity from a different tool

**Name variations and aliases** — Patterns suggesting two entities are the same:
- Same name, different sources (e.g., `person:fec:john_smith` and `person:cl_search:john_smith`)
- Partial name matches with corroborating properties
- Username patterns matching a real name

**Cross-source correlations** — Patterns across different tool outputs:
- Same phone/email appearing in entities from different tools
- Temporal patterns: accounts created or filings made around the same date
- Geographic clustering: multiple entities in the same unusual location

**Finding notes** — The `finding_notes` array contains raw tool output narratives. \
These are often the richest intelligence source. Look for:
- Reddit analysis: timezone estimates, location mentions, subreddit clustering
- LittleSis: relationship density, board memberships, donation patterns
- People search: address histories, relative names, phone numbers
- SEC EDGAR: filing counts, officer names, company relationships
- Court records: case types, filing dates, co-parties
- Any quantitative data not captured in entity properties

**Missing leads** — Valuable follow-up targets not yet in the lead queue:
- Organizations discovered as employers that haven't been investigated
- Domains found in source URLs that could be harvested
- Person names from relationship properties worth searching

## Output Format

Respond with ONLY valid JSON (no markdown fences, no commentary) in this exact format:

{
  "extracted_entities": [
    {
      "id": "<type>:llm:<normalized_value>",
      "entity_type": "<valid type from schema_reference>",
      "label": "Human-readable name",
      "properties": {"key": "value"},
      "confidence": 0.7,
      "reasoning": "Why this entity was extracted — cite the evidence"
    }
  ],
  "extracted_relationships": [
    {
      "source_id": "<existing or new entity ID>",
      "target_id": "<existing or new entity ID>",
      "relation_type": "<valid type from schema_reference>",
      "properties": {},
      "confidence": 0.6,
      "reasoning": "Why this relationship exists — cite matching properties"
    }
  ],
  "extracted_leads": [
    {
      "lead_type": "<username|email|domain|phone|person_name|organization|url>",
      "value": "the actual value to investigate",
      "score": 0.7,
      "entity_id": "<entity ID this lead came from, if applicable>",
      "notes": "Why this is worth investigating"
    }
  ],
  "analysis_notes": "Summary of analysis — key findings and confidence assessment"
}

## Rules

### Entity IDs
- Format: `<type>:llm:<normalized_value>`
- Normalize: lowercase, replace spaces with underscores, strip punctuation
- Check the export's existing entity IDs before creating new ones — do not duplicate

### Confidence levels
- **0.8-1.0**: Near-certain (e.g., exact phone match across two entities)
- **0.6-0.7**: Probable (e.g., same employer + same city)
- **0.4-0.5**: Possible (e.g., similar usernames)
- Below 0.4: Do not extract

### Constraints
- Never fabricate information. Only surface connections present in the exported data.
- Every extracted entity must have at least one relationship to an existing entity.
- Maximum per run: 50 entities, 30 relationships, 20 leads.
- Always include `reasoning` explaining your evidence chain.
- Use only valid values from the `schema_reference` in the export JSON.
- If nothing meaningful is found, return empty arrays with an analysis_notes explanation.
"""

_USER_MESSAGE_PREFIX = (
    "Analyze this investigation data and extract implicit entities, "
    "relationships, and leads. Respond with ONLY the JSON extraction.\n\n"
)


# -- LLM call implementations -----------------------------------------------

def _call_anthropic(api_key: str, model: str, user_message: str) -> str:
    """Call Anthropic Messages API (synchronous)."""
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=model,
        max_tokens=8192,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text


async def _call_openai_compat(
    api_key: str | None,
    base_url: str,
    model: str,
    user_message: str,
) -> str:
    """Call OpenAI-compatible Chat Completions API via httpx.

    Works with OpenAI, OpenRouter, Ollama, LM Studio, vLLM, and any server
    implementing the /v1/chat/completions endpoint.
    """
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    payload = {
        "model": model,
        "max_tokens": 8192,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
    }

    url = f"{base_url.rstrip('/')}/chat/completions"
    async with httpx.AsyncClient(timeout=300) as client:
        response = await client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()

    return data["choices"][0]["message"]["content"]


# -- Response parsing --------------------------------------------------------

def _parse_llm_response(raw_response: str) -> str:
    """Strip markdown fences and validate JSON. Returns clean JSON string."""
    text = raw_response.strip()
    if text.startswith("```"):
        # Remove opening fence (```json or ```)
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
    if text.endswith("```"):
        text = text[:-3].strip()

    try:
        json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"LLM returned invalid JSON: {exc}\n"
            f"First 500 chars of response: {raw_response[:500]}"
        ) from exc

    return text


# -- Main entry point --------------------------------------------------------

async def analyze_via_api(
    store: SqliteStore,
    investigation_id: int | None = None,
    investigation_name: str = "",
    provider: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
) -> dict:
    """Run LLM analysis via API and ingest results.

    Auto-detects provider from environment if not specified.
    Returns the ingest summary dict (entities, relationships, leads, errors).
    """
    if provider is None:
        provider = detect_provider()

    cfg = PROVIDERS.get(provider)
    if cfg is None:
        raise RuntimeError(
            f"Unknown provider: {provider}. "
            f"Available: {', '.join(PROVIDERS)}"
        )

    # Resolve credentials
    api_key = None
    if cfg["env_key"]:
        api_key = os.environ.get(cfg["env_key"])
        if not api_key and provider != "local":
            raise RuntimeError(
                f"{cfg['env_key']} environment variable is required for "
                f"the {provider} provider."
            )

    model = model or cfg["default_model"]
    base_url = base_url or os.environ.get("LLM_BASE_URL") or cfg["base_url"]

    # Step 1: Export investigation data
    export_json = await export_investigation(
        store,
        investigation_id=investigation_id,
        investigation_name=investigation_name,
    )

    meta = json.loads(export_json)["meta"]
    print(f"Exported {meta['entity_count']} entities for analysis...")

    # Step 2: Call LLM
    user_message = _USER_MESSAGE_PREFIX + export_json
    print(f"Calling {provider}/{model}...")

    if provider == "anthropic":
        raw_response = _call_anthropic(api_key, model, user_message)
    else:
        raw_response = await _call_openai_compat(
            api_key, base_url, model, user_message,
        )

    # Step 3: Parse and validate response
    text = _parse_llm_response(raw_response)

    # Step 4: Write to temp file and ingest through existing pipeline
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False,
    ) as f:
        f.write(text)
        tmp_path = f.name

    result = await ingest_extraction(
        store, tmp_path, investigation_id=investigation_id,
    )

    os.unlink(tmp_path)

    return result
