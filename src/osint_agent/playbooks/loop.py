"""Autonomous investigation loop — keeps following threads until done.

Replaces the fixed-depth lead-following in runner.py with a goal-directed
loop that terminates when:
  1. Completeness criteria are met (enough entity types filled)
  2. All leads are exhausted (nothing left above score threshold)
  3. Diminishing returns detected (N consecutive stale rounds)
  4. Hard iteration cap reached

The loop uses the SqliteStore lead queue as its central control structure:
each iteration dequeues the highest-priority pending lead, runs the
appropriate tools (skipping already-tried combinations), ingests findings,
extracts new leads, and checks termination conditions.

Tool coverage tracking prevents duplicate work: a (tool_name, input_value)
pair is never run twice within the same investigation.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime

from osint_agent.graph.resolver import EntityResolver
from osint_agent.graph.sqlite_store import SqliteStore
from osint_agent.models import EntityType, Finding
from osint_agent.playbooks.base import (
    LEAD_TOOL_MAP,
    Playbook,
    PlaybookResult,
    ToolStep,
    extract_leads_from_findings,
)
from osint_agent.playbooks.runner import _run_steps
from osint_agent.tools.registry import ToolRegistry

_LEAD_TOOL_MAP = LEAD_TOOL_MAP

# Default completeness criteria: minimum entity counts by type
# to consider the investigation "complete enough" for a report.
DEFAULT_COMPLETENESS = {
    EntityType.PERSON: 1,     # At least one identified person
    EntityType.ACCOUNT: 1,    # At least one online account
}


@dataclass
class LoopConfig:
    """Configuration for the investigation loop."""

    max_iterations: int = 20
    max_stale_rounds: int = 3
    lead_score_threshold: float = 0.4
    max_leads_per_round: int = 3
    completeness_criteria: dict[EntityType, int] = field(
        default_factory=lambda: dict(DEFAULT_COMPLETENESS),
    )
    # LLM extraction after Phase 1 (None = disabled)
    llm_provider: str | None = None
    llm_model: str | None = None
    llm_base_url: str | None = None


@dataclass
class LoopState:
    """Mutable state tracked across loop iterations."""

    iteration: int = 0
    stale_rounds: int = 0
    tried: set = field(default_factory=set)  # set[tuple[str, str]]: (tool_name, input_value)
    entity_count_before: int = 0
    stop_reason: str = ""


async def run_investigation_loop(
    playbook: Playbook,
    seed: str,
    registry: ToolRegistry,
    store: SqliteStore,
    config: LoopConfig | None = None,
    investigation_name: str | None = None,
    **kwargs,
) -> PlaybookResult:
    """Run an autonomous investigation loop.

    Starts with the playbook's initial steps, then enters a loop that
    follows leads from the queue until termination conditions are met.

    Args:
        playbook: Playbook defining initial steps and completeness criteria.
        seed: Initial input value.
        registry: Tool registry.
        store: SQLite store for persistence and lead queue.
        config: Loop configuration (defaults if None).
        investigation_name: Custom name for the investigation.
        **kwargs: Additional args for playbook.steps().
    """
    cfg = config or LoopConfig()
    state = LoopState()

    # Use playbook-specific completeness criteria if available
    if hasattr(playbook, "completeness_criteria"):
        cfg.completeness_criteria = playbook.completeness_criteria

    result = PlaybookResult(
        playbook_name=playbook.name,
        investigation_id=None,
        started_at=datetime.now(UTC).isoformat(),
    )

    # Create investigation
    inv_name = investigation_name or f"{playbook.name}: {seed}"
    result.investigation_id = await store.create_investigation(inv_name)
    _log(f"Investigation #{result.investigation_id}: {inv_name}")
    _log(f"Loop config: max_iter={cfg.max_iterations}, "
         f"stale_limit={cfg.max_stale_rounds}, "
         f"min_score={cfg.lead_score_threshold}")

    # ── Phase 1: Run playbook's initial steps ────────────────────
    _log(f"\n{'='*60}")
    _log(f"PHASE 1: {playbook.description}")
    _log(f"{'='*60}")

    steps = playbook.steps(seed, **kwargs)
    phase1_findings = await _run_steps(steps, registry)

    for finding in phase1_findings:
        await store.ingest_finding(finding, investigation_id=result.investigation_id)
    result.findings.extend(phase1_findings)

    # Mark initial steps as tried
    for step in steps:
        _mark_tried(state, step.tool_name, _step_input_value(step))

    # Extract and persist leads
    leads = playbook.extract_leads(phase1_findings)
    for lead in leads:
        await store.add_lead(
            lead_type=lead.lead_type,
            value=lead.value,
            score=lead.score,
            investigation_id=result.investigation_id,
            entity_id=lead.source_entity_id,
            notes=lead.notes,
        )
    result.leads.extend(leads)

    state.entity_count_before = await store.entity_count()
    _log(f"\nPhase 1 complete: {state.entity_count_before} entities, "
         f"{len(leads)} leads generated")

    # ── Phase 1.5: LLM extraction (optional) ─────────────────────
    if cfg.llm_provider:
        await _run_llm_extraction(
            store, result, cfg, state,
        )

    # ── Phase 2: Autonomous lead-following loop ──────────────────
    _log(f"\n{'='*60}")
    _log("PHASE 2: Autonomous lead following")
    _log(f"{'='*60}")

    while True:
        state.iteration += 1

        # Check termination conditions
        stop = await _check_termination(state, cfg, store, result)
        if stop:
            state.stop_reason = stop
            _log(f"\nSTOPPING: {stop}")
            break

        # Get next batch of leads
        pending = await store.get_leads(
            status="pending",
            investigation_id=result.investigation_id,
            limit=cfg.max_leads_per_round,
        )

        # Filter to leads above threshold
        actionable = [
            lead for lead in pending
            if lead["score"] >= cfg.lead_score_threshold
        ]

        if not actionable:
            state.stop_reason = "No actionable leads above score threshold"
            _log(f"\nSTOPPING: {state.stop_reason}")
            break

        _log(f"\n--- Iteration {state.iteration} "
             f"({len(actionable)} leads, "
             f"best score={actionable[0]['score']:.2f}) ---")

        round_findings: list[Finding] = []

        for lead_row in actionable:
            lead_id = lead_row["id"]
            lead_type = lead_row["lead_type"]
            lead_value = lead_row["value"]

            # Get tool configs for this lead type
            tool_configs = _LEAD_TOOL_MAP.get(lead_type, [])
            if not tool_configs:
                await store.update_lead(lead_id, "skipped", "No tools for this lead type")
                continue

            # Build steps, filtering out already-tried combinations
            steps = []
            for tool_name, kwargs_fn in tool_configs:
                if _is_tried(state, tool_name, lead_value):
                    continue
                tool = registry.get(tool_name)
                if not tool or not tool.is_available():
                    continue
                steps.append(ToolStep(
                    tool_name=tool_name,
                    kwargs=kwargs_fn(lead_value),
                    description=f"{lead_type}={lead_value} via {tool_name}",
                ))

            if not steps:
                await store.update_lead(lead_id, "exhausted", "All applicable tools already tried")
                continue

            # Run tools
            findings = await _run_steps(steps, registry)
            for finding in findings:
                await store.ingest_finding(finding, investigation_id=result.investigation_id)
            round_findings.extend(findings)

            # Mark as tried
            for step in steps:
                _mark_tried(state, step.tool_name, lead_value)

            # Update lead status
            await store.update_lead(
                lead_id, "completed",
                f"Ran {len(steps)} tools, got {len(findings)} findings",
            )

        result.findings.extend(round_findings)

        # Extract new leads from this round's findings
        new_leads = extract_leads_from_findings(round_findings)
        existing_values = {(l.lead_type, l.value) for l in result.leads}
        novel = [l for l in new_leads if (l.lead_type, l.value) not in existing_values]

        for lead in novel:
            await store.add_lead(
                lead_type=lead.lead_type,
                value=lead.value,
                score=lead.score,
                investigation_id=result.investigation_id,
                entity_id=lead.source_entity_id,
                notes=lead.notes,
            )
        result.leads.extend(novel)

        # Check diminishing returns
        current_count = await store.entity_count()
        new_entities = current_count - state.entity_count_before

        if new_entities == 0:
            state.stale_rounds += 1
            _log(f"  Stale round ({state.stale_rounds}/{cfg.max_stale_rounds}) "
                 f"— no new entities")
        else:
            state.stale_rounds = 0
            _log(f"  +{new_entities} new entities, "
                 f"+{len(novel)} new leads")

        state.entity_count_before = current_count

    # ── Phase 3: Entity resolution ───────────────────────────────
    all_entities = []
    for finding in result.findings:
        all_entities.extend(finding.entities)

    if len(all_entities) >= 2:
        _log(f"\n{'='*60}")
        _log("PHASE 3: Entity resolution")
        _log(f"{'='*60}")

        resolver = EntityResolver()
        aka_rels = resolver.resolve(all_entities)
        if aka_rels:
            _log(f"  Found {len(aka_rels)} cross-source links")
            for rel in aka_rels:
                await store.merge_relationship(rel)
                conf = rel.properties.get("confidence", 0)
                level = "HIGH" if conf >= 0.8 else "MEDIUM"
                src = rel.properties.get("source_label", rel.source_id)
                tgt = rel.properties.get("target_label", rel.target_id)
                _log(f"    {src} <-> {tgt} [{level} {conf:.0%}]")

    # Final state
    result.entity_count = await store.entity_count()
    result.relationship_count = await store.relationship_count()
    result.completed_at = datetime.now(UTC).isoformat()

    _log(f"\n{'='*60}")
    _log(f"COMPLETE ({state.stop_reason})")
    _log(f"{'='*60}")
    _log(result.summary())
    _log(f"  Iterations: {state.iteration}")
    _log(f"  Tools tried: {len(state.tried)}")
    pending_remaining = await store.pending_lead_count()
    _log(f"  Leads remaining: {pending_remaining}")

    return result


async def _run_llm_extraction(
    store: SqliteStore,
    result: PlaybookResult,
    cfg: LoopConfig,
    state: LoopState,
) -> None:
    """Run LLM extraction on Phase 1 findings and queue new leads.

    Calls analyze_via_api() which exports the current graph, sends it
    to the configured LLM, and ingests extracted entities/relationships/
    leads back into the store.
    """
    from osint_agent.llm_analyze import analyze_via_api

    _log(f"\n{'='*60}")
    _log("PHASE 1.5: LLM extraction")
    _log(f"{'='*60}")

    entity_before = await store.entity_count()
    leads_before = await store.pending_lead_count()

    try:
        inv_name = f"Investigation #{result.investigation_id}"
        summary = await analyze_via_api(
            store,
            investigation_id=result.investigation_id,
            investigation_name=inv_name,
            provider=cfg.llm_provider,
            model=cfg.llm_model,
            base_url=cfg.llm_base_url,
        )

        new_entities = await store.entity_count() - entity_before
        new_leads = await store.pending_lead_count() - leads_before

        _log(
            f"  LLM extracted: {summary['entities']} entities, "
            f"{summary['relationships']} relationships, "
            f"{summary['leads']} leads"
        )
        if summary["errors"]:
            _log(f"  LLM validation errors: {summary['errors']}")
        _log(
            f"  Net new: +{new_entities} entities, "
            f"+{new_leads} leads in queue"
        )

        # Update state so Phase 2 stale-round detection
        # accounts for LLM-added entities
        state.entity_count_before = await store.entity_count()

    except Exception as exc:
        _log(f"  LLM extraction failed: {exc}")
        _log("  Continuing without LLM results...")


async def _check_termination(
    state: LoopState,
    cfg: LoopConfig,
    store: SqliteStore,
    result: PlaybookResult,
) -> str:
    """Check all termination conditions. Returns reason string or empty."""
    if state.iteration > cfg.max_iterations:
        return f"Max iterations reached ({cfg.max_iterations})"

    if state.stale_rounds >= cfg.max_stale_rounds:
        return (f"Diminishing returns — {cfg.max_stale_rounds} consecutive "
                "rounds with no new entities")

    # Check completeness
    if cfg.completeness_criteria:
        complete = await _check_completeness(store, cfg.completeness_criteria)
        if complete:
            return "Completeness criteria met"

    return ""


async def _check_completeness(
    store: SqliteStore,
    criteria: dict[EntityType, int],
) -> bool:
    """Check if entity type counts meet completeness criteria."""
    db = await store._ensure_db()

    for entity_type, min_count in criteria.items():
        cursor = await db.execute(
            "SELECT COUNT(*) FROM entities WHERE entity_type = ?",
            (entity_type.value,),
        )
        row = await cursor.fetchone()
        if row[0] < min_count:
            return False

    return True


def _mark_tried(state: LoopState, tool_name: str, input_value: str) -> None:
    """Record a (tool, input) pair as tried."""
    state.tried.add((tool_name, input_value.lower().strip()))


def _is_tried(state: LoopState, tool_name: str, input_value: str) -> bool:
    """Check if a (tool, input) pair has already been tried."""
    return (tool_name, input_value.lower().strip()) in state.tried


def _step_input_value(step: ToolStep) -> str:
    """Extract the primary input value from a ToolStep's kwargs."""
    # Try common parameter names in priority order
    for key in ("username", "email", "query", "name", "domain",
                "url", "phone_number", "file_path"):
        if key in step.kwargs:
            return str(step.kwargs[key])
    # Fallback: first kwarg value
    if step.kwargs:
        return str(next(iter(step.kwargs.values())))
    return ""


def _log(msg: str) -> None:
    """Print progress."""
    from osint_agent import console

    console.status(msg)
