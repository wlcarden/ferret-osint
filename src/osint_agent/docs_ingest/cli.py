"""CLI entry point for the docs_ingest pipeline.

Exposes two modes on the `ingest-docs` subcommand:

    --regex-only    Extract text + run regex pass + ingest into graph.
                    Pure Python, deterministic, fast. Run this first across
                    an entire bundle.

    --merge-llm     For each document with a staged subagent extraction
                    at /tmp/docs_ingest/<hash>.extraction.json, merge the
                    LLM findings into the existing graph.

Default behavior (no flags): --regex-only + stage for LLM. Does NOT invoke
the LLM itself — the orchestrator (Claude Code main conversation) must
spawn the .claude/agents/ingest-doc.md subagent per document.

Investigation scoping: each top-level subfolder under ice_intel_corpus
(e.g. "Tech surveillance/20260408 Analytic Support Services") becomes
one investigation. The investigation name is pulled from the README
index when a title match is found; otherwise falls back to the
subfolder name.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from osint_agent.docs_ingest.extract import extract_text
from osint_agent.docs_ingest.llm_pass import (
    STAGING_DIR,
    load_extraction,
    merge_extraction_into_finding,
    stage_document,
)
from osint_agent.docs_ingest.regex_extract import extract_from_document
from osint_agent.graph.sqlite_store import SqliteStore
from osint_agent.models import Finding

logger = logging.getLogger(__name__)

CORPUS_ROOT = Path("/home/wlcarden/Desktop/OSINT/data/ice_intel_corpus")
INVENTORY_PATH = CORPUS_ROOT / ".inventory.json"
README_INDEX_PATH = CORPUS_ROOT / ".readme_index.json"
AUDIT_LOG_PATH = CORPUS_ROOT / ".ingest_log.jsonl"


@dataclass
class _ReadmeLookup:
    """Maps corpus subfolders to README-derived investigation names + URLs."""

    entries: list[dict]

    def lookup(self, subfolder: str) -> dict | None:
        """Best-effort match of a subfolder name to a README entry.

        The corpus subfolders are dated slugs like
        "20260408 Analytic Support Services". The README titles are human
        prose like "Analytic Support Services". We normalize both and look
        for substring containment, preferring the longest matching title.
        """
        norm = _normalize(subfolder)
        best: dict | None = None
        best_len = 0
        for entry in self.entries:
            title_norm = _normalize(entry["title"])
            # Either direction is a valid match signal
            if title_norm in norm or norm in title_norm:
                if len(title_norm) > best_len:
                    best = entry
                    best_len = len(title_norm)
        return best


def _normalize(s: str) -> str:
    import re
    s = s.lower()
    s = re.sub(r"\d{6,}", "", s)  # strip YYYYMMDD date prefixes
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def _load_inventory() -> list[dict]:
    if not INVENTORY_PATH.exists():
        raise RuntimeError(
            f"Inventory not found at {INVENTORY_PATH}. "
            "Run scripts/ice_intel_inventory.py first."
        )
    return json.loads(INVENTORY_PATH.read_text())


def _load_readme() -> _ReadmeLookup:
    if not README_INDEX_PATH.exists():
        logger.warning(
            "README index not found; subfolder names will be used as investigation titles"
        )
        return _ReadmeLookup(entries=[])
    data = json.loads(README_INDEX_PATH.read_text())
    return _ReadmeLookup(entries=data.get("entries", []))


def _filter_targets(
    inventory: list[dict],
    target: Path,
) -> list[dict]:
    """Subset the inventory to files beneath `target` (directory or file)."""
    target_str = str(target.resolve())
    out = []
    for rec in inventory:
        if rec["path"] == target_str or rec["path"].startswith(target_str + "/"):
            out.append(rec)
    return out


def _group_by_subfolder(
    records: list[dict],
) -> dict[tuple[str, str], list[dict]]:
    """Group inventory records by (bundle, subfolder).

    Records with no subfolder (root-level docs like README) get an empty
    string as subfolder.
    """
    buckets: dict[tuple[str, str], list[dict]] = {}
    for rec in records:
        key = (rec["bundle"], rec.get("subfolder") or "")
        buckets.setdefault(key, []).append(rec)
    return buckets


def _log_audit(event: dict) -> None:
    AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with AUDIT_LOG_PATH.open("a") as f:
        f.write(json.dumps(event) + "\n")


async def _ensure_investigation(
    store: SqliteStore,
    bundle: str,
    subfolder: str,
    readme: _ReadmeLookup,
) -> tuple[int, str]:
    """Create (or reuse) an investigation for a (bundle, subfolder) pair.

    Reuse is by investigation name exact match — we build a stable name
    combining bundle + README-title (or subfolder fallback).
    """
    entry = readme.lookup(subfolder) if subfolder else None
    if entry:
        inv_name = f"[{bundle}] {entry['title']} ({entry['date']})"
        notes = f"Sources:\n" + "\n".join(entry.get("urls", []))
    else:
        inv_name = f"[{bundle}] {subfolder or '(root)'}"
        notes = ""

    existing = await store.list_investigations()
    for inv in existing:
        if inv["name"] == inv_name:
            return inv["id"], inv_name

    inv_id = await store.create_investigation(inv_name, notes)
    return inv_id, inv_name


async def ingest_path(
    target: Path,
    store: SqliteStore,
    regex_only: bool = False,
    merge_llm: bool = False,
    dry_run: bool = False,
) -> dict:
    """Run the ingestion pipeline against a folder or single file.

    Returns a summary dict with per-investigation statistics.
    """
    inventory = _load_inventory()
    readme = _load_readme()
    target_abs = target.resolve()
    records = _filter_targets(inventory, target_abs)
    if not records:
        return {"error": f"No inventory records found under {target_abs}"}

    buckets = _group_by_subfolder(records)
    summary: dict[str, dict] = {}

    for (bundle, subfolder), items in sorted(buckets.items()):
        if dry_run:
            entry = readme.lookup(subfolder) if subfolder else None
            if entry:
                inv_name = f"[{bundle}] {entry['title']} ({entry['date']})"
            else:
                inv_name = f"[{bundle}] {subfolder or '(root)'}"
            inv_id = -1
        else:
            inv_id, inv_name = await _ensure_investigation(
                store, bundle, subfolder, readme,
            )

        per_inv = {
            "investigation_id": inv_id,
            "investigation_name": inv_name,
            "files": len(items),
            "extracted": 0,
            "regex_entities": 0,
            "regex_relationships": 0,
            "llm_merged": 0,
            "llm_entities_added": 0,
            "failures": [],
        }

        for rec in items:
            src = Path(rec["path"])
            er = extract_text(src)
            if not er.ok:
                per_inv["failures"].append(
                    {"file": rec["rel_path"], "error": er.error},
                )
                continue
            if not er.text.strip():
                # Empty extraction — mostly redacted or image-only PDFs
                per_inv["failures"].append(
                    {"file": rec["rel_path"], "error": "empty_extraction"},
                )
                continue

            per_inv["extracted"] += 1
            rx = extract_from_document(src, er.text, CORPUS_ROOT)
            finding = rx.finding

            # Stage for subagent (even in regex-only mode — cheap, lets us
            # come back later and run --merge-llm without re-extracting).
            # Pass the regex finding so the subagent can reuse its IDs.
            manifest = stage_document(
                src, er.text,
                doc_id=next(
                    (e.id for e in finding.entities if e.entity_type.value == "document"),
                    None,
                ),
                regex_finding=finding,
            )

            # Merge LLM extraction if requested and available
            if merge_llm and manifest.out_path.exists():
                try:
                    data = load_extraction(manifest.out_path)
                    before = len(finding.entities)
                    finding = merge_extraction_into_finding(
                        finding, data, src, manifest.doc_id,
                    )
                    per_inv["llm_merged"] += 1
                    per_inv["llm_entities_added"] += len(finding.entities) - before
                except (ValueError, FileNotFoundError) as exc:
                    per_inv["failures"].append(
                        {"file": rec["rel_path"], "error": f"llm_merge: {exc}"},
                    )

            per_inv["regex_entities"] += len(finding.entities)
            per_inv["regex_relationships"] += len(finding.relationships)

            if not dry_run:
                await store.ingest_finding(finding, investigation_id=inv_id)

            _log_audit({
                "investigation_id": inv_id,
                "file": rec["rel_path"],
                "entities": len(finding.entities),
                "relationships": len(finding.relationships),
                "merged_llm": merge_llm and manifest.out_path.exists(),
                "stage_hint": next(
                    (e.properties.get("stage_hint") for e in finding.entities
                     if e.entity_type.value == "document"),
                    None,
                ),
            })

        summary[f"{inv_id}:{inv_name}"] = per_inv

    return summary


async def list_staged_docs() -> list[dict]:
    """List every staged document waiting on LLM subagent output.

    A document is "staged" if its .txt is in /tmp/docs_ingest/ and its
    .extraction.json does NOT yet exist.
    """
    if not STAGING_DIR.exists():
        return []
    staged = []
    for manifest_file in sorted(STAGING_DIR.glob("*.manifest.json")):
        m = json.loads(manifest_file.read_text())
        has_extraction = Path(m["out_path"]).exists()
        staged.append({
            "doc_id": m["doc_id"],
            "source_path": m["source_path"],
            "text_path": m["text_path"],
            "out_path": m["out_path"],
            "char_count": m.get("char_count", 0),
            "extraction_ready": has_extraction,
        })
    return staged
