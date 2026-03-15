# ferret-osint

An agentic OSINT toolkit that chains 35+ data source adapters, normalizes findings into a unified entity graph, and uses playbook-driven investigations to systematically map digital footprints from any seed input.

Built for investigative journalists and researchers who need to move from a name, username, or email to a structured intelligence profile without clicking through dozens of web interfaces.

## How it works

```
seed input (name, email, username, domain, ...)
    │
    ▼
┌─────────────────────────────────┐
│   Playbook / Individual Tool    │  ← orchestrates tool sequence
└──────────────┬──────────────────┘
               │
    ┌──────────┼──────────┐
    ▼          ▼          ▼
┌────────┐ ┌────────┐ ┌────────┐
│ Maigret│ │ EDGAR  │ │OpenFEC │ ... 35+ adapters
└───┬────┘ └───┬────┘ └───┬────┘
    │          │          │
    ▼          ▼          ▼
┌─────────────────────────────────┐
│     Unified Entity Graph        │  ← SQLite, entities + relationships
│  (entity resolution across      │
│   sources via weighted scoring) │
└──────────────┬──────────────────┘
               │
    ┌──────────┼──────────┬───────────┐
    ▼          ▼          ▼           ▼
 Report     Graph     Timeline    Obsidian
  (.md)    (.html)   (.md/.html)   vault
```

Every tool adapter normalizes its output into typed entities (PERSON, ORGANIZATION, DOMAIN, EMAIL, ...) and relationships (WORKS_AT, DONATED_TO, PARTY_TO, ...) with full source provenance. Entity resolution runs automatically across sources, linking records by weighted property matching (email > phone > employer > city).

## Features

- **Playbook investigations** — Structured multi-tool sequences for common workflows:
  - `username_to_identity` — Deanonymize a handle (platform search → web search → email leads)
  - `name_to_surface` — Map a person's digital footprint (people search → courts → donations → usernames)
  - `org_to_members` — Find an organization's people (SEC → contracts → patents → courts)
- **35+ tool adapters** — Each wraps a public data source and normalizes output:

  | Category       | Tools                                                                                            |
  | -------------- | ------------------------------------------------------------------------------------------------ |
  | Identity       | Maigret (2500+ platforms), Holehe (email-to-platform), Gravatar, email permutations              |
  | People         | Multi-aggregator people search (6+ sources), CrossLinked (LinkedIn dorks)                        |
  | Financial      | OpenFEC (campaign finance), SEC EDGAR (filings + insider trades), ProPublica (nonprofit 990s)    |
  | Legal          | CourtListener (federal courts), DocumentCloud (130M+ FOIA docs)                                  |
  | Corporate      | USASpending (federal contracts), SBIR/STTR awards, USPTO patents, LittleSis (power networks)     |
  | Government     | FARA (foreign agent registrations), Congress.gov (legislators + bills), MuckRock (FOIA requests) |
  | Infrastructure | WHOIS, crt.sh (certificate transparency), DNS enumeration, IP WHOIS, BuiltWith, Common Crawl     |
  | Social         | Reddit (profile + post analysis), YouTube (yt-dlp metadata)                                      |
  | Archives       | Wayback Machine, Wayback Machine Google Analytics ID extraction                                  |
  | Recon          | DuckDuckGo (web + news), ExifTool (image metadata), PhoneInfoga                                  |

- **Persistent graph store** — SQLite-backed entity graph accumulates across sessions. Multi-investigation support with scoping.
- **Entity resolution** — Weighted cross-source matching with configurable thresholds. Produces CONFIRMED/PROBABLE/single-source confidence levels.
- **Lead tracking** — Tools automatically extract follow-up targets (emails, usernames, domains, orgs) with priority scores. Playbooks can auto-follow high-scoring leads.
- **Multiple output formats**:
  - Markdown reports with confidence badges and corroboration evidence
  - Interactive force-directed graph visualizations (self-contained HTML)
  - Chronological timelines (markdown or interactive HTML)
  - Obsidian vault export (folder of interlinked markdown files)
- **LLM analysis** — Optional pass that uses an LLM to find implicit entities, unstated relationships, and cross-source correlations that rule-based extraction missed. Supports Anthropic, OpenAI, OpenRouter, and local models (Ollama, LM Studio, vLLM).
- **Claude Code integration** — Ships with agent definitions for autonomous investigation and analysis when used inside Claude Code.

## Installation

```bash
git clone https://github.com/wlcarden/ferret-osint.git
cd ferret-osint

python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Some tools require external binaries
# See scripts/bootstrap.sh for full setup
```

Copy `.env.example` to `.env` and add your API keys. Most tools work without keys, but some (CourtListener, OpenFEC, Congress.gov) require free API keys for full functionality.

## Quick start

```bash
# Check which tools are available
PYTHONPATH=src python -m osint_agent status

# Investigate a username across 2500+ platforms
PYTHONPATH=src python -m osint_agent username johndoe

# Run a full playbook investigation
PYTHONPATH=src python -m osint_agent playbook name_to_surface "Jane Smith" --state California

# Generate a report from investigation findings
PYTHONPATH=src python -m osint_agent report --investigation-id 1 -o reports/jane_smith.md

# Generate an interactive graph
PYTHONPATH=src python -m osint_agent graph --investigation-id 1 -o reports/jane_smith_graph.html

# Generate a timeline
PYTHONPATH=src python -m osint_agent timeline --investigation-id 1 -o reports/jane_smith_timeline.html --format html

# Run LLM analysis (auto-detects provider from env vars)
PYTHONPATH=src python -m osint_agent analyze --run --investigation-id 1

# Use a specific provider/model
PYTHONPATH=src python -m osint_agent analyze --run --provider local --model llama3 --investigation-id 1
```

## Architecture

```
src/osint_agent/
├── models.py           # Entity/Relationship/Source Pydantic models
├── tools/              # 35+ tool adapters (each normalizes to models.py types)
│   ├── base.py         # BaseTool interface
│   ├── registry.py     # Tool discovery and registration
│   ├── edgar.py        # SEC EDGAR adapter (example)
│   └── ...
├── graph/
│   └── sqlite_store.py # Persistent graph store with entity resolution
├── playbooks/          # Multi-tool investigation sequences
│   ├── name_to_surface.py
│   ├── username_to_identity.py
│   └── org_to_members.py
├── agent/              # Entity resolution engine
├── report.py           # Markdown report generator
├── graph_export.py     # Interactive HTML graph visualization
├── timeline.py         # Chronological timeline reconstruction
├── vault_export.py     # Obsidian vault export
├── llm_analyze.py      # Multi-provider LLM analysis
├── llm_export.py       # Export/ingest pipeline for LLM analysis
└── __main__.py         # CLI entry point
```

### Adding a new tool adapter

Tool adapters inherit from `BaseTool` and implement `run()`:

```python
from osint_agent.tools.base import BaseTool
from osint_agent.models import Entity, EntityType, Finding, Source

class MyToolAdapter(BaseTool):
    name = "mytool"
    description = "What this tool does"

    async def run(self, input_data: str, **kwargs) -> Finding:
        # Call external API or CLI tool
        # Normalize results into Entity/Relationship objects
        # Return Finding with entities, relationships, and notes
        ...
```

Register it in `tools/registry.py` and it becomes available via the CLI and playbooks.

## LLM analysis providers

The `analyze --run` command supports multiple LLM providers. It auto-detects from environment variables, or you can specify explicitly:

| Provider     | Env Variable         | Default Model                      | Notes                         |
| ------------ | -------------------- | ---------------------------------- | ----------------------------- |
| `anthropic`  | `ANTHROPIC_API_KEY`  | claude-sonnet-4-20250514           | Anthropic Messages API        |
| `openai`     | `OPENAI_API_KEY`     | gpt-4o                             | OpenAI Chat Completions       |
| `openrouter` | `OPENROUTER_API_KEY` | anthropic/claude-sonnet-4-20250514 | 200+ models via OpenRouter    |
| `local`      | `LLM_BASE_URL`       | llama3                             | Ollama, LM Studio, vLLM, etc. |

```bash
# Local Ollama
PYTHONPATH=src python -m osint_agent analyze --run \
  --provider local --base-url http://localhost:11434/v1 --model llama3.1

# OpenRouter with a specific model
PYTHONPATH=src python -m osint_agent analyze --run \
  --provider openrouter --model google/gemini-2.5-pro
```

## Testing

```bash
PYTHONPATH=src python -m pytest tests/ -x -q
```

Tests mock all external API calls. No network access or API keys needed to run the test suite.

## Legal and ethical use

This toolkit accesses publicly available data through legitimate APIs and public records. It does not:

- Bypass authentication or access controls
- Scrape platforms that prohibit it in their ToS (tools that require accounts are opt-in)
- Store or transmit collected data to third parties

Users are responsible for complying with applicable laws and the terms of service of data sources they access. This tool is intended for journalism, research, and authorized investigations.

## License

Apache 2.0 — see [LICENSE](LICENSE).
