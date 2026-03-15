"""LLM-powered extraction — export investigation data and ingest LLM results.

Two main functions:
  - export_investigation(): Serialize investigation entities/relationships/leads
    as LLM-optimized JSON (with schema reference for valid enum values).
  - ingest_extraction(): Parse LLM-produced JSON containing extracted entities,
    relationships, and leads, validate it, and merge into the store.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from osint_agent.graph.sqlite_store import SqliteStore
from osint_agent.models import (
    Entity,
    EntityType,
    Finding,
    Relationship,
    RelationType,
    Source,
)
from osint_agent.playbooks.base import extract_leads_from_findings
from osint_agent.report import _reconstruct_entity, _reconstruct_relationship

# Lead types that the system can route to tools
_LEAD_TYPES = [
    "username", "email", "domain", "phone",
    "person_name", "organization", "url",
]


async def export_investigation(
    store: SqliteStore,
    investigation_id: int | None = None,
    investigation_name: str = "",
) -> str:
    """Export investigation data as LLM-optimized JSON.

    Returns a JSON string containing all entities, relationships, leads,
    and a schema reference so the LLM knows valid enum values.
    """
    # Query store (scoped or all)
    if investigation_id is not None:
        entity_rows = await store.query(f"inv:{investigation_id}:all_nodes")
        rel_rows = await store.query(f"inv:{investigation_id}:all_edges")
        leads = await store.get_leads(investigation_id=investigation_id, limit=200)
        finding_notes = await store.get_finding_notes(investigation_id=investigation_id)
    else:
        entity_rows = await store.query("all_nodes")
        rel_rows = await store.query("all_edges")
        leads = await store.get_leads(limit=200)
        finding_notes = await store.get_finding_notes()

    # Reconstruct model objects
    entities = [_reconstruct_entity(row) for row in entity_rows]
    relationships = [_reconstruct_relationship(row) for row in rel_rows]

    # Serialize entities
    entity_dicts = []
    for e in entities:
        entity_dicts.append({
            "id": e.id,
            "entity_type": e.entity_type.value,
            "label": e.label,
            "properties": e.properties,
            "sources": [
                {
                    "tool": s.tool,
                    "source_url": s.source_url,
                    "confidence": s.confidence,
                }
                for s in e.sources
            ],
        })

    # Serialize relationships
    rel_dicts = []
    for r in relationships:
        rel_dicts.append({
            "source_id": r.source_id,
            "target_id": r.target_id,
            "relation_type": r.relation_type.value,
            "properties": r.properties,
        })

    # Serialize leads
    lead_dicts = []
    for lead in leads:
        lead_dicts.append({
            "lead_type": lead.get("lead_type", ""),
            "value": lead.get("value", ""),
            "score": lead.get("score", 0),
            "status": lead.get("status", "pending"),
            "entity_id": lead.get("entity_id"),
            "notes": lead.get("notes", ""),
        })

    export = {
        "meta": {
            "investigation_id": investigation_id,
            "investigation_name": investigation_name,
            "exported_at": datetime.now(UTC).isoformat(),
            "entity_count": len(entity_dicts),
            "relationship_count": len(rel_dicts),
            "lead_count": len(lead_dicts),
            "finding_notes_count": len(finding_notes),
        },
        "entities": entity_dicts,
        "relationships": rel_dicts,
        "leads": lead_dicts,
        "finding_notes": finding_notes,
        "schema_reference": {
            "entity_types": [t.value for t in EntityType],
            "relation_types": [t.value for t in RelationType],
            "lead_types": _LEAD_TYPES,
            "entity_id_convention": "<type>:llm:<normalized_value>",
        },
    }

    return json.dumps(export, indent=2, default=str)


async def ingest_extraction(
    store: SqliteStore,
    json_path: str,
    investigation_id: int | None = None,
) -> dict:
    """Ingest LLM-extracted entities, relationships, and leads from a JSON file.

    Validates enum values, constructs Finding objects with llm_extraction
    provenance, and merges into the store. Returns a summary dict.
    """
    raw = Path(json_path).read_text()
    data = json.loads(raw)

    entities = []
    relationships = []
    errors = []

    # Parse entities
    for i, item in enumerate(data.get("extracted_entities", [])):
        try:
            entity_type = EntityType(item["entity_type"])
        except (ValueError, KeyError):
            errors.append(
                f"Entity {i}: invalid entity_type "
                f"'{item.get('entity_type', '<missing>')}' — skipped"
            )
            continue

        props = dict(item.get("properties", {}))
        reasoning = item.get("reasoning", "")
        if reasoning:
            props["llm_reasoning"] = reasoning

        confidence = item.get("confidence", 0.7)

        entities.append(Entity(
            id=item["id"],
            entity_type=entity_type,
            label=item.get("label", ""),
            properties=props,
            sources=[Source(
                tool="llm_extraction",
                confidence=confidence,
            )],
        ))

    # Parse relationships
    for i, item in enumerate(data.get("extracted_relationships", [])):
        try:
            relation_type = RelationType(item["relation_type"])
        except (ValueError, KeyError):
            errors.append(
                f"Relationship {i}: invalid relation_type "
                f"'{item.get('relation_type', '<missing>')}' — skipped"
            )
            continue

        props = dict(item.get("properties", {}))
        reasoning = item.get("reasoning", "")
        if reasoning:
            props["llm_reasoning"] = reasoning

        confidence = item.get("confidence", 0.7)

        relationships.append(Relationship(
            source_id=item["source_id"],
            target_id=item["target_id"],
            relation_type=relation_type,
            properties=props,
            sources=[Source(
                tool="llm_extraction",
                confidence=confidence,
            )],
        ))

    # Print validation errors
    from osint_agent import console

    for err in errors:
        console.warning(err)

    # Ingest as a Finding
    finding = Finding(
        entities=entities,
        relationships=relationships,
        notes=data.get("analysis_notes", "LLM extraction"),
    )
    await store.ingest_finding(finding, investigation_id=investigation_id)

    # Ingest explicit leads
    explicit_leads = data.get("extracted_leads", [])
    explicit_keys = set()
    for lead in explicit_leads:
        lead_type = lead.get("lead_type", "")
        value = lead.get("value", "")
        if not lead_type or not value:
            continue
        if lead_type not in _LEAD_TYPES:
            console.warning(
                f"unknown lead_type '{lead_type}' -- skipped",
            )
            continue
        explicit_keys.add((lead_type, value))
        await store.add_lead(
            lead_type=lead_type,
            value=value,
            score=lead.get("score", 0.5),
            investigation_id=investigation_id,
            entity_id=lead.get("entity_id"),
            notes=lead.get("notes", ""),
        )

    # Auto-extract leads from new entities (safety net)
    auto_leads = extract_leads_from_findings([finding])
    auto_count = 0
    for lead in auto_leads:
        key = (lead.lead_type, lead.value)
        if key in explicit_keys:
            continue
        await store.add_lead(
            lead_type=lead.lead_type,
            value=lead.value,
            score=lead.score,
            investigation_id=investigation_id,
            entity_id=lead.source_entity_id,
            notes=lead.notes,
        )
        auto_count += 1

    total_leads = len(explicit_leads) + auto_count

    return {
        "entities": len(entities),
        "relationships": len(relationships),
        "leads": total_leads,
        "errors": len(errors),
    }
