"""Staging + post-processing for the Claude Code subagent document pass.

This module is NOT an LLM-calling client — the orchestrator (Claude Code)
spawns the `.claude/agents/ingest-doc.md` subagent directly via its Agent
tool. This module's job is to:

1. Stage the document's extracted text and a manifest of inputs the
   subagent needs (DOC_PATH, SOURCE_PATH, DOC_ID, OUT_PATH) to a
   well-known location under `/tmp/docs_ingest/`.
2. After the subagent writes its JSON extraction, parse and validate
   the output and merge it with the regex-pass Finding.

The merge preserves regex findings verbatim and appends LLM entities
without duplication (IDs are compared first; for collisions the regex
version wins because it was derived from deterministic patterns).
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from osint_agent.models import (
    Entity,
    EntityType,
    Finding,
    Relationship,
    RelationType,
    Source,
)

logger = logging.getLogger(__name__)

STAGING_DIR = Path("/tmp/docs_ingest")


@dataclass
class SubagentManifest:
    """Inputs the subagent needs to do its job.

    Produced by stage_document(); the orchestrator reads fields off this
    and passes them verbatim into the subagent prompt. REGEX_PATH points
    to a JSON file listing entities the regex pass already extracted so
    the subagent uses those IDs verbatim for cross-references instead of
    guessing at a slug convention.
    """

    doc_id: str
    source_path: Path          # Original corpus file
    text_path: Path            # Plain-text staging file (extracted text)
    out_path: Path             # Where the subagent writes its extraction JSON
    manifest_path: Path        # Human-readable index of this stage
    regex_path: Path           # JSON list of regex-extracted entities (for ID cross-ref)

    def as_prompt_vars(self) -> dict[str, str]:
        return {
            "DOC_ID": self.doc_id,
            "SOURCE_PATH": str(self.source_path),
            "DOC_PATH": str(self.text_path),
            "OUT_PATH": str(self.out_path),
            "REGEX_PATH": str(self.regex_path),
        }


def _doc_hash(path: Path) -> str:
    """Keyed on absolute path so relative/absolute call sites converge."""
    return hashlib.sha1(
        str(path.resolve()).encode("utf-8"),
    ).hexdigest()[:10]


def stage_document(
    source_path: Path,
    text: str,
    doc_id: str | None = None,
    regex_finding: Finding | None = None,
) -> SubagentManifest:
    """Write the extracted text + regex hints to staging files.

    If `regex_finding` is provided, writes a companion JSON at
    `<digest>.regex.json` listing every regex-extracted entity with its ID,
    type, and label. The subagent Reads this before writing its own
    extraction so cross-references use the exact IDs (no slug-convention
    guessing). Idempotent: re-staging the same document overwrites.
    """
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    digest = _doc_hash(source_path)
    if doc_id is None:
        doc_id = f"document:corpus:{digest}"

    text_path = STAGING_DIR / f"{digest}.txt"
    out_path = STAGING_DIR / f"{digest}.extraction.json"
    manifest_path = STAGING_DIR / f"{digest}.manifest.json"
    regex_path = STAGING_DIR / f"{digest}.regex.json"

    text_path.write_text(text)

    # Regex hint file — always write, empty list if no regex pass ran yet
    regex_entities_out = []
    if regex_finding is not None:
        for e in regex_finding.entities:
            regex_entities_out.append({
                "id": e.id,
                "entity_type": e.entity_type.value,
                "label": e.label,
                "properties": {
                    k: v for k, v in e.properties.items()
                    if k in {"uei", "stage_hint", "agency_domain", "email_observed"}
                },
            })
    regex_path.write_text(json.dumps({
        "doc_id": doc_id,
        "entities": regex_entities_out,
    }, indent=2))

    manifest_path.write_text(json.dumps({
        "doc_id": doc_id,
        "source_path": str(source_path),
        "text_path": str(text_path),
        "out_path": str(out_path),
        "regex_path": str(regex_path),
        "char_count": len(text),
    }, indent=2))

    return SubagentManifest(
        doc_id=doc_id,
        source_path=source_path,
        text_path=text_path,
        out_path=out_path,
        manifest_path=manifest_path,
        regex_path=regex_path,
    )


def load_extraction(
    out_path: Path,
) -> dict:
    """Load and JSON-validate a subagent extraction file.

    Raises FileNotFoundError if the file doesn't exist yet and ValueError
    if JSON is malformed.
    """
    if not out_path.exists():
        raise FileNotFoundError(
            f"Subagent extraction not found at {out_path} — did the "
            "subagent complete?"
        )
    data = json.loads(out_path.read_text())
    if not isinstance(data, dict):
        raise ValueError(
            f"Extraction at {out_path} must be a JSON object, got {type(data)}"
        )
    data.setdefault("extracted_entities", [])
    data.setdefault("extracted_relationships", [])
    data.setdefault("extracted_leads", [])
    return data


def merge_extraction_into_finding(
    regex_finding: Finding,
    llm_extraction: dict,
    source_path: Path,
    doc_id: str,
) -> Finding:
    """Merge subagent output into the regex-derived Finding.

    Regex entities win on ID collision (they're more deterministic). LLM
    entities are stamped with a Source(tool='docs_ingest.llm') so we can
    always trace which pass produced them.
    """
    llm_src = Source(
        tool="docs_ingest.llm",
        source_url=f"file://{source_path}",
        confidence=0.7,
    )

    existing_ids = {e.id for e in regex_finding.entities}
    entities_out = list(regex_finding.entities)
    rels_out = list(regex_finding.relationships)

    skipped = 0
    for item in llm_extraction.get("extracted_entities", []):
        ent_id = item.get("id", "").strip()
        if not ent_id or ent_id in existing_ids:
            skipped += 1
            continue
        try:
            etype = EntityType(item["entity_type"])
        except (KeyError, ValueError):
            logger.warning(
                "LLM entity %s skipped: invalid entity_type %r",
                ent_id, item.get("entity_type"),
            )
            skipped += 1
            continue
        props = dict(item.get("properties") or {})
        conf = item.get("confidence")
        if conf is not None:
            props.setdefault("llm_confidence", conf)
        if item.get("reasoning"):
            props.setdefault("llm_reasoning", item["reasoning"])

        entities_out.append(Entity(
            id=ent_id,
            entity_type=etype,
            label=item.get("label") or ent_id,
            properties=props,
            sources=[llm_src.model_copy(update={"confidence": conf or 0.7})],
        ))
        existing_ids.add(ent_id)
        # Provenance: link to the source document via MENTIONED_IN
        if ent_id != doc_id:
            rels_out.append(Relationship(
                source_id=ent_id,
                target_id=doc_id,
                relation_type=RelationType.MENTIONED_IN,
                sources=[llm_src],
            ))

    # Relationships — require both endpoints to exist after merge
    all_ids = existing_ids
    for item in llm_extraction.get("extracted_relationships", []):
        src_id = item.get("source_id", "")
        tgt_id = item.get("target_id", "")
        if not src_id or not tgt_id:
            continue
        if src_id not in all_ids or tgt_id not in all_ids:
            logger.debug(
                "LLM relationship skipped: endpoint not in graph (%s -> %s)",
                src_id, tgt_id,
            )
            continue
        try:
            rtype = RelationType(item["relation_type"])
        except (KeyError, ValueError):
            logger.warning(
                "LLM relationship %s->%s skipped: invalid relation_type %r",
                src_id, tgt_id, item.get("relation_type"),
            )
            continue
        props = dict(item.get("properties") or {})
        conf = item.get("confidence")
        if conf is not None:
            props.setdefault("llm_confidence", conf)
        if item.get("reasoning"):
            props.setdefault("llm_reasoning", item["reasoning"])

        rels_out.append(Relationship(
            source_id=src_id,
            target_id=tgt_id,
            relation_type=rtype,
            properties=props,
            sources=[llm_src.model_copy(update={"confidence": conf or 0.7})],
        ))

    notes_parts = []
    if regex_finding.notes:
        notes_parts.append(regex_finding.notes)
    if llm_extraction.get("analysis_notes"):
        notes_parts.append(f"LLM: {llm_extraction['analysis_notes']}")
    if skipped:
        notes_parts.append(f"(LLM: {skipped} entities skipped as duplicates or invalid)")

    return Finding(
        entities=entities_out,
        relationships=rels_out,
        notes="\n".join(notes_parts),
    )
