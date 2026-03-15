"""Playbook runner — executes playbooks with store and lead queue integration.

The runner handles:
  1. Creating an investigation in the store
  2. Running playbook steps (concurrently where possible)
  3. Ingesting findings into the graph
  4. Extracting and persisting leads
  5. Optionally following leads up to a depth limit
  6. Running entity resolution across findings
  7. Caching tool results to avoid redundant API calls
"""

import asyncio
from datetime import UTC, datetime

from osint_agent.cache import ToolCache
from osint_agent.graph.resolver import EntityResolver
from osint_agent.graph.sqlite_store import SqliteStore
from osint_agent.models import Entity, ErrorCategory, Finding, ToolError
from osint_agent.playbooks.base import Lead, Playbook, PlaybookResult, ToolStep
from osint_agent.tools.registry import INPUT_ROUTING, ToolRegistry


# Maps lead_type to the tools and kwargs needed to follow up
_LEAD_TOOL_MAP = {
    "username": [
        ("maigret", lambda v: {"username": v}),
        ("reddit", lambda v: {"username": v}),
        ("steam", lambda v: {"username": v}),
    ],
    "email": [
        ("holehe", lambda v: {"email": v}),
        ("gravatar", lambda v: {"email": v}),
    ],
    "domain": [
        ("theharvester", lambda v: {"domain": v}),
        ("whois", lambda v: {"domain": v}),
        ("wayback_ga", lambda v: {"url": v}),
        ("crtsh", lambda v: {"domain": v}),
        ("dns_enum", lambda v: {"domain": v}),
        ("builtwith", lambda v: {"url": v}),
    ],
    "phone": [
        ("phoneinfoga", lambda v: {"phone_number": v}),
    ],
    "person_name": [
        ("courtlistener", lambda v: {"name": v}),
        ("openfec", lambda v: {"query": v, "mode": "contributors"}),
        ("littlesis", lambda v: {"query": v}),
        ("documentcloud", lambda v: {"query": v}),
        ("fara", lambda v: {"name": v}),
        ("congress", lambda v: {"query": v, "mode": "member"}),
    ],
    "organization": [
        ("littlesis", lambda v: {"query": v}),
        ("fara", lambda v: {"name": v}),
        ("documentcloud", lambda v: {"query": v}),
        ("muckrock", lambda v: {"query": v, "mode": "foia"}),
        ("propublica_nonprofit", lambda v: {"query": v}),
        ("crosslinked", lambda v: {"company": v}),
    ],
    "url": [
        ("wayback", lambda v: {"url": v, "mode": "snapshots"}),
    ],
}


async def run_playbook(
    playbook: Playbook,
    seed: str,
    registry: ToolRegistry,
    store: SqliteStore,
    investigation_name: str | None = None,
    follow_leads: bool = True,
    max_depth: int = 1,
    lead_score_threshold: float = 0.5,
    max_leads_per_round: int = 5,
    cache: ToolCache | None = None,
    **kwargs,
) -> PlaybookResult:
    """Execute a playbook end-to-end.

    Args:
        playbook: The playbook to run.
        seed: Initial input value.
        registry: Tool registry for adapter access.
        store: SQLite store for persistence and lead queue.
        investigation_name: Name for the investigation (default: auto-generated).
        follow_leads: Whether to follow generated leads.
        max_depth: Maximum lead-following depth (1 = follow immediate leads only).
        lead_score_threshold: Minimum lead score to follow.
        max_leads_per_round: Max leads to follow per depth level.
        **kwargs: Additional args passed to playbook.steps().
    """
    result = PlaybookResult(
        playbook_name=playbook.name,
        investigation_id=None,
        started_at=datetime.now(UTC).isoformat(),
    )

    # Create investigation
    inv_name = investigation_name or f"{playbook.name}: {seed}"
    result.investigation_id = await store.create_investigation(inv_name)
    _print(f"Investigation #{result.investigation_id}: {inv_name}")

    # Initialize cache if not provided
    if cache is None:
        cache = ToolCache()

    # Phase 1: Run playbook steps
    _print(f"\n--- Phase 1: {playbook.description} ---")
    steps = playbook.steps(seed, **kwargs)
    phase1_findings = await _run_steps(steps, registry, cache, result)

    for finding in phase1_findings:
        await store.ingest_finding(finding, investigation_id=result.investigation_id)
    result.findings.extend(phase1_findings)

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
    _print(f"  Generated {len(leads)} leads")

    # Phase 2: Follow leads
    if follow_leads and leads and max_depth > 0:
        await _follow_leads(
            leads=leads,
            registry=registry,
            store=store,
            result=result,
            depth=1,
            max_depth=max_depth,
            score_threshold=lead_score_threshold,
            max_per_round=max_leads_per_round,
            cache=cache,
        )

    # Phase 3: Entity resolution
    all_entities = []
    for finding in result.findings:
        all_entities.extend(finding.entities)

    if len(all_entities) >= 2:
        _print("\n--- Entity Resolution ---")
        resolver = EntityResolver()
        aka_rels = resolver.resolve(all_entities)
        if aka_rels:
            _print(f"  Found {len(aka_rels)} cross-source links")
            for rel in aka_rels:
                await store.merge_relationship(rel)
                conf = rel.properties.get("confidence", 0)
                level = "HIGH" if conf >= 0.8 else "MEDIUM"
                src_label = rel.properties.get("source_label", rel.source_id)
                tgt_label = rel.properties.get("target_label", rel.target_id)
                _print(f"    {src_label} <-> {tgt_label} [{level} {conf:.0%}]")

    # Clean up cache
    if cache is not None:
        expired = await cache.clear_expired()
        if expired:
            _print(f"  Cleared {expired} expired cache entries")

    # Final counts
    result.entity_count = await store.entity_count()
    result.relationship_count = await store.relationship_count()
    result.completed_at = datetime.now(UTC).isoformat()

    _print(f"\n{result.summary()}")
    return result


async def _run_steps(
    steps: list[ToolStep],
    registry: ToolRegistry,
    cache: ToolCache | None = None,
    result: PlaybookResult | None = None,
) -> list[Finding]:
    """Run a list of tool steps concurrently, collecting findings."""
    tasks = []
    for step in steps:
        tool = registry.get(step.tool_name)
        if not tool or not tool.is_available():
            _print(f"  SKIP {step.description} ({step.tool_name} not available)")
            continue
        _print(f"  RUN  {step.description}")
        tasks.append(_run_one_step(tool, step, cache))

    if not tasks:
        return []

    results = await asyncio.gather(*tasks, return_exceptions=True)
    findings = []
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            _print(f"  ERROR: {r}")
            if result is not None:
                result.errors.append(ToolError(
                    tool="unknown",
                    category=ErrorCategory.UNKNOWN,
                    message=f"{type(r).__name__}: {r}",
                ))
            continue
        if r:
            if r.error is not None:
                _print_error(r.error)
                if result is not None:
                    result.errors.append(r.error)
            findings.append(r)
    return findings


async def _run_one_step(
    tool, step: ToolStep, cache: ToolCache | None = None,
) -> Finding | None:
    """Run a single tool step, checking cache first."""
    # Check cache
    if cache is not None:
        cached = await cache.get(step.tool_name, step.kwargs)
        if cached is not None:
            _print(f"  CACHE {step.description}")
            return cached

    # CourtListener uses search_party() instead of run()
    if step.tool_name == "courtlistener" and "name" in step.kwargs:
        try:
            finding = await tool.search_party(name=step.kwargs["name"])
        except Exception as exc:
            finding = Finding(
                notes=f"courtlistener: {type(exc).__name__}: {exc}",
                error=ToolError(
                    tool="courtlistener",
                    category=ErrorCategory.UNKNOWN,
                    message=f"{type(exc).__name__}: {exc}",
                ),
            )
    else:
        finding = await tool.safe_run(**step.kwargs)

    # Store in cache (don't cache error findings)
    if cache is not None and finding is not None and finding.error is None:
        await cache.set(step.tool_name, step.kwargs, finding)

    return finding


async def _follow_leads(
    leads: list[Lead],
    registry: ToolRegistry,
    store: SqliteStore,
    result: PlaybookResult,
    depth: int,
    max_depth: int,
    score_threshold: float,
    max_per_round: int,
    cache: ToolCache | None = None,
) -> None:
    """Follow leads by running appropriate tools for each lead type."""
    # Filter and limit leads
    actionable = [
        lead for lead in leads
        if lead.score >= score_threshold
    ][:max_per_round]

    if not actionable:
        return

    _print(f"\n--- Phase 2 (depth {depth}): Following {len(actionable)} leads ---")

    new_findings: list[Finding] = []
    new_leads: list[Lead] = []

    for lead in actionable:
        tool_configs = _LEAD_TOOL_MAP.get(lead.lead_type, [])
        if not tool_configs:
            continue

        steps = []
        for tool_name, kwargs_fn in tool_configs:
            steps.append(ToolStep(
                tool_name=tool_name,
                kwargs=kwargs_fn(lead.value),
                description=f"Follow lead: {lead.lead_type}={lead.value} via {tool_name}",
            ))

        findings = await _run_steps(steps, registry, cache, result)
        for finding in findings:
            await store.ingest_finding(finding, investigation_id=result.investigation_id)
        new_findings.extend(findings)

        # Mark lead as completed in store
        # (We don't have the store lead ID here, but the lead was already persisted)

    result.findings.extend(new_findings)

    # Extract new leads from follow-up findings
    from osint_agent.playbooks.base import extract_leads_from_findings

    follow_leads = extract_leads_from_findings(new_findings)
    # Avoid re-following leads we already have
    existing_values = {(l.lead_type, l.value) for l in result.leads}
    novel_leads = [
        l for l in follow_leads
        if (l.lead_type, l.value) not in existing_values
    ]

    for lead in novel_leads:
        await store.add_lead(
            lead_type=lead.lead_type,
            value=lead.value,
            score=lead.score,
            investigation_id=result.investigation_id,
            notes=lead.notes,
        )
    result.leads.extend(novel_leads)
    _print(f"  Generated {len(novel_leads)} new leads from follow-up")

    # Recurse if we have depth budget
    if novel_leads and depth < max_depth:
        await _follow_leads(
            leads=novel_leads,
            registry=registry,
            store=store,
            result=result,
            depth=depth + 1,
            max_depth=max_depth,
            score_threshold=score_threshold,
            max_per_round=max_per_round,
            cache=cache,
        )


def _print(msg: str) -> None:
    """Print progress output."""
    print(msg)


def _print_error(error: ToolError) -> None:
    """Print a structured error with actionable suggestion."""
    _print(f"  ERROR [{error.category.value}] {error.tool}: {error.message}")
    if error.suggestion:
        _print(f"    -> {error.suggestion}")
