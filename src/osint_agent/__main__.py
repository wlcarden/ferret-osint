"""CLI entry point for the OSINT agent toolkit.

Usage:
    python -m osint_agent status                     # Show tool availability
    python -m osint_agent username <username>         # Username search (Maigret)
    python -m osint_agent email <email>               # Email platform check (Holehe)
    python -m osint_agent email-perms <first> <last> <domain>  # Email permutation scan
    python -m osint_agent company <ticker_or_name>    # SEC EDGAR lookup
    python -m osint_agent insiders <ticker>           # SEC insider transactions
    python -m osint_agent court <name>                # CourtListener party search
    python -m osint_agent donors <name>               # OpenFEC contributor search
    python -m osint_agent domain <domain>             # theHarvester email/subdomain harvest
    python -m osint_agent wayback <url>               # Wayback Machine snapshots
    python -m osint_agent exif <file_path>            # Image metadata extraction
    python -m osint_agent phone <number>              # Phone number scan
    python -m osint_agent whois <domain>               # WHOIS domain registration lookup
    python -m osint_agent contracts <query>            # USASpending federal contract search
    python -m osint_agent patents <query>              # USPTO patent search
    python -m osint_agent sbir <query>                 # SBIR/STTR award search
    python -m osint_agent commoncrawl <domain_or_url>  # Common Crawl index search
    python -m osint_agent people <name>                 # People search across 6+ aggregators
    python -m osint_agent reddit <username>             # Reddit profile + post history analysis
    python -m osint_agent gravatar <email>              # Gravatar profile lookup (email → identity bridge)
    python -m osint_agent search <query>              # DuckDuckGo web/news search
    python -m osint_agent investigate <input>          # Auto-route: detect input type, run all applicable tools
    python -m osint_agent playbook <name> <seed>       # Run a structured investigation playbook
    python -m osint_agent report                       # Generate report from graph data (with corroboration evidence)
    python -m osint_agent graph                        # Generate interactive Cytoscape.js graph visualization
"""

import argparse
import asyncio
import sys

from osint_agent.models import Finding
from osint_agent.tools.registry import ToolRegistry


def print_finding(finding: Finding):
    """Print a Finding in a human-readable, scannable format.

    Shows a one-line summary, then entities grouped by type with the
    most relevant detail per entity. Large result sets are capped
    with a count of omitted items.
    """
    if finding.notes:
        print(f"\n  {finding.notes}")

    if not finding.entities and not finding.relationships:
        return

    # One-line summary
    type_counts = {}
    for e in finding.entities:
        type_counts[e.entity_type.value] = type_counts.get(e.entity_type.value, 0) + 1
    summary_parts = [f"{count} {t}" for t, count in sorted(type_counts.items())]
    if finding.relationships:
        summary_parts.append(f"{len(finding.relationships)} rel")
    if summary_parts:
        print(f"  Summary: {', '.join(summary_parts)}")

    # Multi-source entities first (entities found by 2+ tools are highest signal)
    multi_source = [e for e in finding.entities if len(e.sources) > 1]
    if multi_source:
        print(f"\n  [MULTI-SOURCE] ({len(multi_source)} entities from 2+ tools)")
        for e in multi_source[:10]:
            tools = ", ".join(s.tool for s in e.sources)
            print(f"    {e.label} [{e.entity_type.value}] ({tools})")
        if len(multi_source) > 10:
            print(f"    ... and {len(multi_source) - 10} more")

    # Group remaining entities by type
    entities_by_type: dict[str, list] = {}
    multi_ids = {e.id for e in multi_source}
    for e in finding.entities:
        if e.id in multi_ids:
            continue  # Already shown in multi-source section
        t = e.entity_type.value
        entities_by_type.setdefault(t, []).append(e)

    for entity_type, entities in sorted(entities_by_type.items()):
        print(f"\n  [{entity_type.upper()}] ({len(entities)})")
        for e in entities[:15]:  # Cap display at 15 per type
            detail = _entity_detail(e)
            print(f"    {e.label}{detail}")
        if len(entities) > 15:
            print(f"    ... and {len(entities) - 15} more")

    if finding.relationships:
        print(f"\n  Relationships: {len(finding.relationships)}")
        rel_counts: dict[str, int] = {}
        for r in finding.relationships:
            rel_counts[r.relation_type.value] = rel_counts.get(r.relation_type.value, 0) + 1
        for rtype, count in sorted(rel_counts.items()):
            print(f"    {rtype}: {count}")


def _entity_detail(e) -> str:
    """Extract the most relevant detail string for an entity."""
    p = e.properties
    if p.get("url"):
        return f" → {p['url']}"
    if p.get("platform"):
        return f" ({p['platform']})"
    if p.get("court"):
        return f" [{p['court']}]"
    if p.get("ticker") or p.get("tickers"):
        tickers = p.get("tickers", [p.get("ticker", "")])
        return f" [{', '.join(str(t) for t in tickers)}]"
    if p.get("party"):
        state = f", {p['state']}" if p.get("state") else ""
        return f" ({p['party']}{state})"
    if p.get("foia_status"):
        return f" [{p['foia_status']}]"
    if p.get("status"):
        return f" [{p['status']}]"
    return ""


async def run_tool(
    registry: ToolRegistry,
    tool_name: str,
    use_cache: bool = True,
    **kwargs,
) -> Finding:
    """Run a single tool and return its Finding, with optional caching."""
    adapter = registry.get(tool_name)
    if not adapter:
        return Finding(notes=f"Tool '{tool_name}' not found in registry")
    if not adapter.is_available():
        return Finding(notes=f"Tool '{tool_name}' is not available (not installed or missing API key)")

    if use_cache:
        from osint_agent.cache import ToolCache

        cache = ToolCache()
        cached = await cache.get(tool_name, kwargs)
        if cached is not None:
            print(f"  [CACHE HIT] {tool_name}")
            return cached

        finding = await adapter.safe_run(**kwargs)
        if finding.error is None:
            await cache.set(tool_name, kwargs, finding)
        else:
            _print_tool_error(finding.error)
        return finding

    finding = await adapter.safe_run(**kwargs)
    if finding.error is not None:
        _print_tool_error(finding.error)
    return finding


def _detect_input_type(input_value: str) -> str:
    """Detect the type of an OSINT input value."""
    import re

    if re.match(r"^[^@]+@[^@]+\.[^@]+$", input_value):
        return "email"
    elif re.match(r"^\+?\d[\d\s\-()]{7,}$", input_value):
        return "phone"
    elif re.match(r"^https?://", input_value):
        return "url"
    elif "." in input_value and " " not in input_value and "/" not in input_value:
        return "domain"
    elif " " in input_value:
        return "person_name"
    return "username"


def _print_tool_error(error) -> None:
    """Print a structured tool error with actionable suggestion."""
    print(f"  ERROR [{error.category.value}] {error.tool}: {error.message}")
    if error.suggestion:
        print(f"    -> {error.suggestion}")


def _normalize_cli_input(input_type: str, value: str) -> str:
    """Normalize a CLI input value, printing a message if it changed."""
    from osint_agent.input_validation import InputValidationError, normalize_input

    try:
        normalized = normalize_input(input_type, value)
        if normalized != value.strip():
            print(f"  [normalized] '{value}' → '{normalized}'")
        return normalized
    except InputValidationError as e:
        print(f"  WARNING: {e}")
        return value.strip()


def _build_tool_call(tool, input_value: str, **kwargs):
    """Build the correct async call for a tool based on its name.

    Uses safe_run() for most tools (catches unhandled exceptions).
    CourtListener uses search_party() which is not covered by safe_run().
    """
    dispatch = {
        "courtlistener": lambda: tool.search_party(name=input_value),
        "openfec": lambda: tool.safe_run(
            query=input_value,
            mode="contributors",
            employer=kwargs.get("employer"),
            occupation=kwargs.get("occupation"),
        ),
        "edgar": lambda: tool.safe_run(query=input_value, mode="search"),
        "theharvester": lambda: tool.safe_run(domain=input_value),
        "wayback": lambda: tool.safe_run(url=input_value, mode="snapshots"),
        "exiftool": lambda: tool.safe_run(file_path=input_value),
        "maigret": lambda: tool.safe_run(username=input_value),
        "holehe": lambda: tool.safe_run(email=input_value),
        "phoneinfoga": lambda: tool.safe_run(phone_number=input_value),
        "whois": lambda: tool.safe_run(domain=input_value),
        "usaspending": lambda: tool.safe_run(query=input_value, mode="recipient"),
        "sbir": lambda: tool.safe_run(query=input_value, mode="firm"),
        "patents": lambda: tool.safe_run(query=input_value, mode="inventor"),
        "commoncrawl": lambda: tool.safe_run(query=input_value),
        "peoplesearch": lambda: tool.safe_run(
            query=input_value,
            state=kwargs.get("state", ""),
            city=kwargs.get("city", ""),
        ),
        "ddg_search": lambda: tool.safe_run(query=input_value),
        "reddit": lambda: tool.safe_run(username=input_value),
        "gravatar": lambda: tool.safe_run(email=input_value),
        "steam": lambda: tool.safe_run(username=input_value),
    }
    call = dispatch.get(tool.name, lambda: tool.safe_run(input_value))
    return call()


async def _run_tool_safely(tool, input_value: str, **kwargs):
    """Run a single tool with error recovery. Returns (name, finding)."""
    finding = await _build_tool_call(tool, input_value, **kwargs)
    return (tool.name, finding)


async def investigate(
    registry: ToolRegistry,
    input_value: str,
    **kwargs,
) -> list[Finding]:
    """Auto-detect input type and run all applicable tools in parallel."""
    input_type = _detect_input_type(input_value)
    input_value = _normalize_cli_input(input_type, input_value)
    print(f"  Detected: {input_type}")

    tools = registry.for_input_type(input_type)
    if not tools:
        print(f"  No available tools for input type '{input_type}'")
        return []

    print(f"  Running {len(tools)} tools in parallel: {', '.join(t.name for t in tools)}")

    # Run all tools concurrently
    tasks = [
        _run_tool_safely(tool, input_value, **kwargs)
        for tool in tools
    ]
    results = await asyncio.gather(*tasks)

    # Collect results and print
    findings = []
    for result in results:
        name, finding = result[0], result[1]
        if finding is None:
            continue
        if finding.error is not None:
            print(f"\n  --- {name} ---")
            _print_tool_error(finding.error)
            continue
        findings.append(finding)
        print(f"\n  --- {name} ---")
        print_finding(finding)

    return findings


async def _run_entity_resolution(
    store,
    findings: list[Finding],
) -> None:
    """Run entity resolution across all ingested entities."""
    from osint_agent.graph.resolver import EntityResolver

    # Collect all entities from all findings
    all_entities = []
    for finding in findings:
        all_entities.extend(finding.entities)

    if len(all_entities) < 2:
        return

    resolver = EntityResolver()
    aka_rels = resolver.resolve(all_entities)

    if not aka_rels:
        return

    # Ingest the ALSO_KNOWN_AS relationships
    aka_finding = Finding(relationships=aka_rels)
    await store.ingest_finding(aka_finding)

    print(f"\n  Entity Resolution: {len(aka_rels)} cross-source links found")
    for rel in aka_rels:
        conf = rel.properties.get("confidence", 0)
        src_label = rel.properties.get("source_label", rel.source_id)
        tgt_label = rel.properties.get("target_label", rel.target_id)
        level = "HIGH" if conf >= 0.8 else "MEDIUM" if conf >= 0.6 else "LOW"
        print(f"    {src_label} ↔ {tgt_label} [{level} {conf:.0%}]")


async def main_async(args):
    from dotenv import load_dotenv
    load_dotenv()
    registry = ToolRegistry()

    if args.command == "status":
        print(registry.summary())

        # Validate configured API keys
        if getattr(args, "validate_keys", False):
            from osint_agent.key_validator import print_validation_report, validate_api_keys

            print()
            results = await validate_api_keys(only_configured=True)
            print_validation_report(results)

        # Show cache stats
        if getattr(args, "cache_stats", False):
            from osint_agent.cache import ToolCache

            cache = ToolCache()
            stats = await cache.stats()
            print(f"\n  Cache: {stats['valid']} valid, {stats['expired']} expired entries")
            if stats["by_tool"]:
                for tool, count in sorted(stats["by_tool"].items()):
                    print(f"    {tool}: {count}")
            await cache.close()
        return

    if args.command == "investigations":
        from osint_agent.graph.sqlite_store import SqliteStore

        store = SqliteStore(db_path=getattr(args, "db", None))
        investigations = await store.list_investigations()
        if not investigations:
            print("No investigations found.")
        else:
            print(f"{'ID':>4}  {'Name':<40}  {'Created':<20}")
            print("-" * 68)
            for inv in investigations:
                created = inv["created_at"][:19].replace("T", " ")
                print(f"{inv['id']:>4}  {inv['name']:<40}  {created}")
        if hasattr(store, "close"):
            await store.close()
        return

    if args.command == "search-graph":
        from osint_agent.graph.sqlite_store import SqliteStore

        store = SqliteStore(db_path=getattr(args, "db", None))
        entity_type = getattr(args, "type", None)
        results = await store.search_across_investigations(
            args.query, entity_type=entity_type,
        )

        if not results:
            print(f"No entities matching '{args.query}'")
        else:
            print(f"Found {len(results)} entities matching '{args.query}':\n")
            for entity in results:
                etype = entity.get("entity_type", "")
                label = entity.get("label", "")
                eid = entity.get("id", "")
                sources = entity.get("sources", [])
                tools = sorted({s.get("tool", "") for s in sources}) if sources else []

                print(f"  [{etype}] {label}")
                print(f"    ID: {eid}")
                if tools:
                    print(f"    Sources: {', '.join(tools)}")

                invs = entity.get("investigations", [])
                if invs:
                    for inv in invs:
                        print(f"    Investigation #{inv['id']}: {inv['name']}")
                else:
                    print("    (not linked to any investigation)")
                print()

        if hasattr(store, "close"):
            await store.close()
        return

    if args.command == "prune":
        from osint_agent.graph.sqlite_store import SqliteStore

        store = SqliteStore(db_path=getattr(args, "db", None))
        inv_id = getattr(args, "prune_investigation_id", None)
        dry_run = getattr(args, "dry_run", False)
        seed = getattr(args, "unreachable", None)
        orphans = getattr(args, "orphans", False)
        min_comp = getattr(args, "min_component", None)

        if not orphans and not seed and not min_comp:
            print("Specify --orphans, --unreachable <entity_id>, and/or --min-component <N>")
            await store.close()
            return

        to_remove: set[str] = set()

        if orphans:
            orphan_ids = await store.find_orphan_ids(investigation_id=inv_id)
            print(f"Found {len(orphan_ids)} orphan entities (no relationships)")
            to_remove |= orphan_ids

        if min_comp:
            small_ids = await store.find_small_component_ids(
                min_size=min_comp, investigation_id=inv_id,
            )
            print(f"Found {len(small_ids)} entities in components smaller than {min_comp}")
            to_remove |= small_ids

        if seed:
            unreachable_ids = await store.find_unreachable_ids(
                seed_id=seed, investigation_id=inv_id,
            )
            print(f"Found {len(unreachable_ids)} entities unreachable from {seed}")
            to_remove |= unreachable_ids

        if not to_remove:
            print("Nothing to prune.")
            await store.close()
            return

        # Show breakdown by type
        db = await store._ensure_db()
        if to_remove:
            placeholders = ",".join("?" for _ in to_remove)
            cursor = await db.execute(
                f"""SELECT entity_type, COUNT(*) as cnt FROM entities
                    WHERE id IN ({placeholders}) GROUP BY entity_type""",
                list(to_remove),
            )
            print(f"\nWill remove {len(to_remove)} entities:")
            for row in await cursor.fetchall():
                print(f"  {row['entity_type']}: {row['cnt']}")

        if dry_run:
            print("\n(dry run — no changes made)")
        else:
            deleted = await store.delete_entities(to_remove)
            remaining_e = await store.entity_count()
            remaining_r = await store.relationship_count()
            print(f"\nDeleted {deleted} entities.")
            print(f"Remaining: {remaining_e} entities, {remaining_r} relationships")

        await store.close()
        return

    if args.command == "scope":
        from osint_agent.graph.sqlite_store import SqliteStore

        store = SqliteStore(db_path=getattr(args, "db", None))
        inv_id = args.scope_investigation_id
        seed = getattr(args, "seed", "") or ""
        count = await store.backfill_investigation(inv_id, seed_label=seed)
        print(f"Linked {count} entities to investigation #{inv_id}")

        # Show what we scoped
        nodes = await store.query(f"inv:{inv_id}:all_nodes")
        edges = await store.query(f"inv:{inv_id}:all_edges")
        print(f"Investigation now has {len(nodes)} entities, {len(edges)} edges")
        if hasattr(store, "close"):
            await store.close()
        return

    if args.command == "report":
        from pathlib import Path

        from osint_agent.graph.sqlite_store import SqliteStore
        from osint_agent.report import ReportGenerator

        store = SqliteStore(db_path=getattr(args, "db", None))
        gen = ReportGenerator()
        inv_id = getattr(args, "investigation_id", None)
        inv_name = getattr(args, "investigation_name", None) or ""
        report_md = await gen.generate(
            store, investigation_id=inv_id, investigation_name=inv_name,
        )
        out_path = getattr(args, "output", None)
        if out_path:
            Path(out_path).parent.mkdir(parents=True, exist_ok=True)
            Path(out_path).write_text(report_md)
            print(f"Report written to {out_path}")
        else:
            print(report_md)
        if hasattr(store, "close"):
            await store.close()
        return

    if args.command == "graph":
        from pathlib import Path

        from osint_agent.graph.sqlite_store import SqliteStore
        from osint_agent.graph_export import GraphExporter

        store = SqliteStore(db_path=getattr(args, "db", None))
        exporter = GraphExporter()
        inv_name = getattr(args, "investigation_name", None) or ""
        inv_id = getattr(args, "investigation_id", None)
        html = await exporter.export(
            store, investigation_name=inv_name, investigation_id=inv_id,
        )
        out_path = getattr(args, "output", None)
        if out_path:
            Path(out_path).parent.mkdir(parents=True, exist_ok=True)
            Path(out_path).write_text(html)
            print(f"Graph written to {out_path}")
        else:
            print(html)
        if hasattr(store, "close"):
            await store.close()
        return

    if args.command == "vault":
        from pathlib import Path

        from osint_agent.graph.sqlite_store import SqliteStore
        from osint_agent.vault_export import VaultExporter

        store = SqliteStore(db_path=getattr(args, "db", None))
        exporter = VaultExporter()
        inv_name = getattr(args, "investigation_name", None) or ""
        inv_id = getattr(args, "investigation_id", None)
        out_dir = getattr(args, "output", None) or "reports/vault"
        summary = await exporter.export(
            store,
            output_dir=out_dir,
            investigation_name=inv_name,
            investigation_id=inv_id,
        )
        print(
            f"Vault written to {out_dir}/ — "
            f"{summary['entities']} entities, "
            f"{summary['relationships']} relationships, "
            f"{summary['files']} files"
        )
        if hasattr(store, "close"):
            await store.close()
        return

    if args.command == "timeline":
        from pathlib import Path

        from osint_agent.graph.sqlite_store import SqliteStore
        from osint_agent.timeline import TimelineGenerator

        store = SqliteStore(db_path=getattr(args, "db", None))
        gen = TimelineGenerator()
        inv_id = getattr(args, "investigation_id", None)
        inv_name = getattr(args, "investigation_name", None) or ""
        fmt = getattr(args, "format", "markdown")
        include_activity = getattr(args, "include_activity", False)
        output = await gen.generate(
            store,
            investigation_id=inv_id,
            investigation_name=inv_name,
            fmt=fmt,
            include_activity=include_activity,
        )
        out_path = getattr(args, "output", None)
        if out_path:
            Path(out_path).parent.mkdir(parents=True, exist_ok=True)
            Path(out_path).write_text(output)
            print(f"Timeline written to {out_path}")
        else:
            print(output)
        if hasattr(store, "close"):
            await store.close()
        return

    if args.command == "analyze":
        from pathlib import Path

        from osint_agent.graph.sqlite_store import SqliteStore
        from osint_agent.llm_export import export_investigation, ingest_extraction

        store = SqliteStore(db_path=getattr(args, "db", None))
        inv_id = getattr(args, "investigation_id", None)
        inv_name = getattr(args, "investigation_name", None) or ""

        if getattr(args, "run", False):
            from osint_agent.llm_analyze import analyze_via_api

            result = await analyze_via_api(
                store,
                investigation_id=inv_id,
                investigation_name=inv_name,
                provider=getattr(args, "provider", None),
                model=getattr(args, "model", None),
                base_url=getattr(args, "base_url", None),
            )
            print(
                f"Analysis complete: {result['entities']} entities, "
                f"{result['relationships']} relationships, "
                f"{result['leads']} leads"
            )
            if result["errors"]:
                print(f"  ({result['errors']} items skipped due to validation errors)")
        elif getattr(args, "export", False):
            export_json = await export_investigation(
                store,
                investigation_id=inv_id,
                investigation_name=inv_name,
            )
            out_path = getattr(args, "output", None)
            if out_path:
                Path(out_path).parent.mkdir(parents=True, exist_ok=True)
                Path(out_path).write_text(export_json)
                print(f"Export written to {out_path}")
            else:
                print(export_json)
        elif getattr(args, "ingest", None):
            result = await ingest_extraction(
                store,
                json_path=args.ingest,
                investigation_id=inv_id,
            )
            print(
                f"Ingested {result['entities']} entities, "
                f"{result['relationships']} relationships, "
                f"{result['leads']} leads"
            )
            if result["errors"]:
                print(f"  ({result['errors']} items skipped due to validation errors)")
        else:
            print("Specify --run, --export, or --ingest <file>")

        if hasattr(store, "close"):
            await store.close()
        return

    # Use SQLite by default for persistent graph, --memory for in-memory
    if getattr(args, "memory", False):
        from osint_agent.graph.memory_store import MemoryStore
        store = MemoryStore()
    else:
        from osint_agent.graph.sqlite_store import SqliteStore
        db_path = getattr(args, "db", None)
        store = SqliteStore(db_path=db_path)
    findings: list[Finding] = []

    if args.command == "username":
        uname = _normalize_cli_input("username", args.input)
        finding = await run_tool(registry, "maigret", username=uname)
        findings.append(finding)
    elif args.command == "email":
        email = _normalize_cli_input("email", args.input)
        finding = await run_tool(registry, "holehe", email=email)
        findings.append(finding)
    elif args.command == "email-perms":
        adapter = registry.get("holehe")
        if adapter and adapter.is_available():
            finding = await adapter.run_permutations(
                first_name=args.first_name,
                last_name=args.last_name,
                domain=args.domain,
            )
            findings.append(finding)
        else:
            findings.append(
                Finding(notes="Holehe not available (not installed)")
            )
    elif args.command == "company":
        finding = await run_tool(registry, "edgar", query=args.input, mode="company")
        findings.append(finding)
    elif args.command == "insiders":
        finding = await run_tool(registry, "edgar", query=args.input, mode="insiders")
        findings.append(finding)
    elif args.command == "court":
        adapter = registry.get("courtlistener")
        if adapter and adapter.is_available():
            finding = await adapter.search_party(name=args.input)
            findings.append(finding)
        else:
            findings.append(Finding(notes="CourtListener not available (missing API key)"))
    elif args.command == "donors":
        finding = await run_tool(
            registry,
            "openfec",
            query=args.input,
            mode="contributors",
            employer=args.employer,
            occupation=args.occupation,
        )
        findings.append(finding)
    elif args.command == "domain":
        domain = _normalize_cli_input("domain", args.input)
        finding = await run_tool(registry, "theharvester", domain=domain)
        findings.append(finding)
    elif args.command == "wayback":
        url = _normalize_cli_input("url", args.input)
        finding = await run_tool(registry, "wayback", url=url, mode="snapshots")
        findings.append(finding)
    elif args.command == "exif":
        finding = await run_tool(registry, "exiftool", file_path=args.input)
        findings.append(finding)
    elif args.command == "phone":
        phone = _normalize_cli_input("phone", args.input)
        finding = await run_tool(registry, "phoneinfoga", phone_number=phone)
        findings.append(finding)
    elif args.command == "whois":
        domain = _normalize_cli_input("domain", args.input)
        finding = await run_tool(registry, "whois", domain=domain)
        findings.append(finding)
    elif args.command == "contracts":
        finding = await run_tool(
            registry,
            "usaspending",
            query=args.input,
            mode=args.mode,
            max_results=args.max_results,
        )
        findings.append(finding)
    elif args.command == "patents":
        finding = await run_tool(
            registry,
            "patents",
            query=args.input,
            mode=args.mode,
            max_results=args.max_results,
        )
        findings.append(finding)
    elif args.command == "sbir":
        finding = await run_tool(
            registry,
            "sbir",
            query=args.input,
            mode=args.mode,
            max_results=args.max_results,
        )
        findings.append(finding)
    elif args.command == "commoncrawl":
        finding = await run_tool(
            registry,
            "commoncrawl",
            query=args.input,
            max_results=args.max_results,
        )
        findings.append(finding)
    elif args.command == "people":
        finding = await run_tool(
            registry,
            "peoplesearch",
            query=args.input,
            state=args.state,
            city=args.city,
        )
        findings.append(finding)
    elif args.command == "reddit":
        uname = _normalize_cli_input("username", args.input)
        finding = await run_tool(registry, "reddit", username=uname)
        findings.append(finding)
    elif args.command == "steam":
        uname = _normalize_cli_input("username", args.input)
        finding = await run_tool(registry, "steam", username=uname)
        findings.append(finding)
    elif args.command == "gravatar":
        email = _normalize_cli_input("email", args.input)
        finding = await run_tool(registry, "gravatar", email=email)
        findings.append(finding)
    elif args.command == "ytdlp":
        finding = await run_tool(registry, "yt-dlp", url=args.input)
        findings.append(finding)
    elif args.command == "crtsh":
        domain = _normalize_cli_input("domain", args.input)
        finding = await run_tool(registry, "crtsh", domain=domain)
        findings.append(finding)
    elif args.command == "dnsenum":
        domain = _normalize_cli_input("domain", args.input)
        finding = await run_tool(registry, "dns_enum", domain=domain)
        findings.append(finding)
    elif args.command == "ipwhois":
        ip = _normalize_cli_input("ip", args.input)
        finding = await run_tool(registry, "ip_whois", ip=ip)
        findings.append(finding)
    elif args.command == "crosslinked":
        finding = await run_tool(registry, "crosslinked", company=args.input)
        findings.append(finding)
    elif args.command == "builtwith":
        domain = _normalize_cli_input("domain", args.input)
        finding = await run_tool(registry, "builtwith", domain=domain)
        findings.append(finding)
    elif args.command == "littlesis":
        finding = await run_tool(registry, "littlesis", name=args.input)
        findings.append(finding)
    elif args.command == "policedata":
        finding = await run_tool(
            registry, "openpolicedata",
            agency=args.input,
            state=getattr(args, "state", ""),
            table_type=getattr(args, "table_type", ""),
        )
        findings.append(finding)
    elif args.command == "nonprofit":
        # Detect EIN format (XX-XXXXXXX or 9 digits).
        inp = args.input.strip()
        if inp.replace("-", "").isdigit() and len(inp.replace("-", "")) == 9:
            finding = await run_tool(registry, "propublica_nonprofit", name="", ein=inp)
        else:
            finding = await run_tool(registry, "propublica_nonprofit", name=inp)
        findings.append(finding)
    elif args.command == "waybackga":
        finding = await run_tool(
            registry,
            "wayback_ga",
            url=args.input,
            limit=getattr(args, "limit", 500),
        )
        findings.append(finding)
    elif args.command == "documents":
        finding = await run_tool(
            registry,
            "documentcloud",
            query=args.input,
        )
        findings.append(finding)
    elif args.command == "fara":
        finding = await run_tool(
            registry,
            "fara",
            name=args.input,
        )
        findings.append(finding)
    elif args.command == "muckrock":
        finding = await run_tool(
            registry,
            "muckrock",
            query=args.input,
            mode=getattr(args, "mode", "foia"),
        )
        findings.append(finding)
    elif args.command == "congress":
        finding = await run_tool(
            registry,
            "congress",
            query=args.input,
            mode=getattr(args, "mode", "member"),
        )
        findings.append(finding)
    elif args.command == "search":
        finding = await run_tool(
            registry,
            "ddg_search",
            query=args.input,
            mode=args.mode,
            max_results=args.max_results,
        )
        findings.append(finding)
    elif args.command == "investigate":
        findings = await investigate(registry, args.input)
    elif args.command == "playbook":
        from osint_agent.playbooks.username_to_identity import UsernameToldentity
        from osint_agent.playbooks.name_to_surface import NameToSurface
        from osint_agent.playbooks.org_to_members import OrgToMembers

        playbook_map = {
            "username_to_identity": UsernameToldentity(),
            "name_to_surface": NameToSurface(),
            "org_to_members": OrgToMembers(),
        }
        pb = playbook_map.get(args.playbook_name)
        if not pb:
            print(f"Unknown playbook: {args.playbook_name}")
            print(f"Available: {', '.join(playbook_map.keys())}")
            if hasattr(store, "close"):
                await store.close()
            return

        pb_kwargs = {}
        if hasattr(args, "state") and args.state:
            pb_kwargs["state"] = args.state
        if hasattr(args, "city") and args.city:
            pb_kwargs["city"] = args.city

        if getattr(args, "auto", False):
            from osint_agent.playbooks.loop import LoopConfig, run_investigation_loop

            loop_cfg = LoopConfig(
                max_iterations=getattr(args, "max_iterations", 20),
                max_stale_rounds=getattr(args, "max_stale", 3),
                lead_score_threshold=getattr(args, "min_score", 0.4),
                max_leads_per_round=getattr(args, "leads_per_round", 3),
            )
            result = await run_investigation_loop(
                playbook=pb,
                seed=args.input,
                registry=registry,
                store=store,
                config=loop_cfg,
                investigation_name=getattr(args, "investigation_name", None),
                **pb_kwargs,
            )
        else:
            from osint_agent.playbooks.runner import run_playbook

            result = await run_playbook(
                playbook=pb,
                seed=args.input,
                registry=registry,
                store=store,
                investigation_name=getattr(args, "investigation_name", None),
                follow_leads=not getattr(args, "no_follow", False),
                max_depth=getattr(args, "depth", 1),
                lead_score_threshold=getattr(args, "min_score", 0.5),
                **pb_kwargs,
            )
        # Playbook handles its own ingestion, skip the normal flow
        if hasattr(store, "close"):
            await store.close()
        return

    # Ingest all findings into graph
    inv_id = getattr(args, "investigation_id", None)
    for finding in findings:
        if args.command != "investigate":
            print_finding(finding)
        await store.ingest_finding(finding, investigation_id=inv_id)

    # Run entity resolution when multiple findings exist
    if len(findings) > 1 or args.command == "investigate":
        await _run_entity_resolution(store, findings)

    # Print graph summary
    entity_count = await store.entity_count()
    rel_count = await store.relationship_count()
    if entity_count > 0:
        print(f"\n  Graph: {entity_count} entities, {rel_count} relationships")
        if hasattr(store, "summary_async"):
            print(f"  {await store.summary_async()}")
        else:
            print(f"  {store.summary()}")

    # Close persistent stores
    if hasattr(store, "close"):
        await store.close()


def main():
    parser = argparse.ArgumentParser(
        prog="osint_agent",
        description="OSINT Agent — investigative intelligence toolkit",
    )
    parser.add_argument(
        "--memory",
        action="store_true",
        help="Use in-memory graph store (no persistence)",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Path to SQLite database file (default: data/graph.db)",
    )
    parser.add_argument(
        "--investigation-id",
        type=int,
        default=None,
        help="Associate findings with an existing investigation ID",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Status
    status_parser = subparsers.add_parser("status", help="Show tool availability")
    status_parser.add_argument(
        "--validate-keys", action="store_true", dest="validate_keys",
        help="Test configured API keys against live endpoints",
    )
    status_parser.add_argument(
        "--cache-stats", action="store_true", dest="cache_stats",
        help="Show result cache statistics",
    )
    subparsers.add_parser(
        "investigations", help="List all investigations and their IDs",
    )
    search_graph_sub = subparsers.add_parser(
        "search-graph",
        help="Search entities across all investigations",
    )
    search_graph_sub.add_argument(
        "query", help="Search text (matches against entity labels)",
    )
    search_graph_sub.add_argument(
        "--type", default=None,
        help="Filter by entity type (person, organization, email, etc.)",
    )
    scope_sub = subparsers.add_parser(
        "scope",
        help="Backfill entity→investigation links from leads and graph reachability",
    )
    scope_sub.add_argument(
        "scope_investigation_id", type=int,
        help="Investigation ID to backfill",
    )
    scope_sub.add_argument(
        "--seed", default="",
        help="Label substring to seed entity matching (e.g. 'Bill Beckwith')",
    )

    prune_sub = subparsers.add_parser(
        "prune",
        help="Remove orphan or unreachable entities from the graph",
    )
    prune_sub.add_argument(
        "--orphans", action="store_true",
        help="Remove entities with no relationships",
    )
    prune_sub.add_argument(
        "--unreachable", metavar="ENTITY_ID",
        help="Remove entities not reachable from this seed entity",
    )
    prune_sub.add_argument(
        "--min-component", type=int, metavar="N",
        help="Remove entities in connected components smaller than N nodes",
    )
    prune_sub.add_argument(
        "--investigation-id", type=int, default=None,
        dest="prune_investigation_id",
        help="Scope pruning to entities in this investigation",
    )
    prune_sub.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be removed without deleting",
    )

    # Tool commands
    for cmd, help_text in [
        ("username", "Search for a username across 2500+ platforms"),
        ("email", "Check which platforms an email is registered on"),
        ("company", "Look up a company in SEC EDGAR"),
        ("insiders", "Get insider transactions for a company"),
        ("court", "Search for a person/org in federal court records"),
        ("domain", "Harvest emails and subdomains for a domain"),
        ("wayback", "Find Wayback Machine snapshots for a URL"),
        ("exif", "Extract metadata from an image file"),
        ("phone", "Scan a phone number for carrier/location info"),
        ("whois", "WHOIS domain registration lookup"),
        ("investigate", "Auto-detect input type and run all applicable tools"),
    ]:
        sub = subparsers.add_parser(cmd, help=help_text)
        sub.add_argument("input", help="The search target")

    # Email permutation scan
    email_perms_sub = subparsers.add_parser(
        "email-perms",
        help="Bruteforce email permutations for a person at a domain",
    )
    email_perms_sub.add_argument(
        "first_name", help="First name (e.g. Bill)",
    )
    email_perms_sub.add_argument(
        "last_name", help="Last name (e.g. Beckwith)",
    )
    email_perms_sub.add_argument(
        "domain", help="Email domain (e.g. ois.com)",
    )

    # Donors command with extra filtering options
    donors_sub = subparsers.add_parser(
        "donors", help="Search campaign finance contributions by name",
    )
    donors_sub.add_argument("input", help="Contributor name to search")
    donors_sub.add_argument(
        "--employer",
        default=None,
        help="Filter by employer name",
    )
    donors_sub.add_argument(
        "--occupation",
        default=None,
        help="Filter by occupation",
    )

    # Patents command with extra options
    patents_sub = subparsers.add_parser(
        "patents", help="Search USPTO patents by inventor, assignee, or keyword",
    )
    patents_sub.add_argument("input", help="Inventor name, company name, or keyword")
    patents_sub.add_argument(
        "--mode",
        choices=["inventor", "assignee", "keyword"],
        default="inventor",
        help="Search mode: inventor, assignee, or keyword (default: inventor)",
    )
    patents_sub.add_argument(
        "--max-results",
        type=int,
        default=25,
        help="Maximum number of results (default: 25)",
    )

    # Contracts command with extra options
    contracts_sub = subparsers.add_parser(
        "contracts", help="Search USASpending.gov federal contracts",
    )
    contracts_sub.add_argument("input", help="Company name or keyword")
    contracts_sub.add_argument(
        "--mode",
        choices=["recipient", "keyword"],
        default="recipient",
        help="Search mode: recipient (by name) or keyword (full-text) (default: recipient)",
    )
    contracts_sub.add_argument(
        "--max-results",
        type=int,
        default=25,
        help="Maximum number of results (default: 25)",
    )

    # SBIR command with extra options
    sbir_sub = subparsers.add_parser(
        "sbir", help="Search SBIR.gov for SBIR/STTR awards",
    )
    sbir_sub.add_argument("input", help="Company name, PI name, or keyword")
    sbir_sub.add_argument(
        "--mode",
        choices=["firm", "pi", "keyword"],
        default="firm",
        help="Search mode: firm, pi, or keyword (default: firm)",
    )
    sbir_sub.add_argument(
        "--max-results",
        type=int,
        default=50,
        help="Maximum number of results (default: 50)",
    )

    # Common Crawl command
    cc_sub = subparsers.add_parser(
        "commoncrawl", help="Search the Common Crawl index for a domain or URL",
    )
    cc_sub.add_argument("input", help="Domain (e.g. example.com) or URL pattern")
    cc_sub.add_argument(
        "--max-results",
        type=int,
        default=50,
        help="Maximum number of results (default: 50)",
    )

    # People search command
    people_sub = subparsers.add_parser(
        "people", help="Search people search aggregators for a person",
    )
    people_sub.add_argument("input", help="Person name (e.g. 'Thomas Jacob')")
    people_sub.add_argument(
        "--state",
        default="",
        help="State name or abbreviation (e.g. 'Virginia' or 'VA')",
    )
    people_sub.add_argument(
        "--city",
        default="",
        help="City for narrower results",
    )

    # Reddit profile command
    reddit_sub = subparsers.add_parser(
        "reddit", help="Analyze a Reddit user's profile and post history",
    )
    reddit_sub.add_argument("input", help="Reddit username (without u/ prefix)")

    # Gravatar lookup command
    gravatar_sub = subparsers.add_parser(
        "gravatar", help="Look up Gravatar profile for an email (name, username, linked accounts)",
    )
    gravatar_sub.add_argument("input", help="Email address to look up")

    # Steam profile command
    steam_sub = subparsers.add_parser(
        "steam", help="Look up Steam Community profile by vanity URL",
    )
    steam_sub.add_argument("input", help="Steam vanity URL / custom ID")

    # yt-dlp command
    ytdlp_sub = subparsers.add_parser(
        "ytdlp", help="Extract metadata from YouTube videos/channels (yt-dlp)",
    )
    ytdlp_sub.add_argument("input", help="YouTube video or channel URL")

    # Certificate Transparency command
    crtsh_sub = subparsers.add_parser(
        "crtsh", help="Discover subdomains via Certificate Transparency (crt.sh)",
    )
    crtsh_sub.add_argument("input", help="Base domain (e.g. example.com)")

    # DNS enumeration command
    dnsenum_sub = subparsers.add_parser(
        "dnsenum", help="Enumerate DNS records (A, MX, NS, TXT, SOA) for a domain",
    )
    dnsenum_sub.add_argument("input", help="Domain name")

    # IP WHOIS command
    ipwhois_sub = subparsers.add_parser(
        "ipwhois", help="Look up ASN, organization, and network info for an IP",
    )
    ipwhois_sub.add_argument("input", help="IP address (IPv4 or IPv6)")

    # CrossLinked command
    crosslinked_sub = subparsers.add_parser(
        "crosslinked", help="Find employees at a company via LinkedIn search engine dorks",
    )
    crosslinked_sub.add_argument("input", help="Company or organization name")

    # BuiltWith command
    builtwith_sub = subparsers.add_parser(
        "builtwith", help="Fingerprint website technologies (CMS, frameworks, analytics)",
    )
    builtwith_sub.add_argument("input", help="Domain or URL")

    # LittleSis command
    littlesis_sub = subparsers.add_parser(
        "littlesis", help="Search LittleSis power network database (people, orgs, boards, donations)",
    )
    littlesis_sub.add_argument("input", help="Person or organization name")

    # OpenPoliceData command
    opd_sub = subparsers.add_parser(
        "policedata", help="Query police incident data (use of force, stops, complaints) from US agencies",
    )
    opd_sub.add_argument("input", help="Agency or source name (e.g. 'Norfolk', 'Fairfax County')")
    opd_sub.add_argument(
        "--state", default="", help="State name to narrow search",
    )
    opd_sub.add_argument(
        "--table-type", default="",
        help="Specific data type (e.g. 'USE OF FORCE', 'STOPS'). Omit to see available types.",
    )

    # ProPublica Nonprofit command
    nonprofit_sub = subparsers.add_parser(
        "nonprofit", help="Search ProPublica Nonprofit Explorer (tax returns, executive comp, revenue)",
    )
    nonprofit_sub.add_argument("input", help="Nonprofit name or EIN")

    waybackga_sub = subparsers.add_parser(
        "waybackga", help="Discover Google Analytics/GTM tracking IDs from Wayback Machine snapshots",
    )
    waybackga_sub.add_argument("input", help="Domain or URL to analyze")
    waybackga_sub.add_argument("--limit", type=int, default=500, help="Max snapshots to check (default: 500)")

    documents_sub = subparsers.add_parser(
        "documents", help="Search DocumentCloud for FOIA docs, court filings, leaked memos",
    )
    documents_sub.add_argument("input", help="Search query")

    fara_sub = subparsers.add_parser(
        "fara", help="Search FARA foreign agent registrations (lobbying for foreign governments)",
    )
    fara_sub.add_argument("input", help="Registrant name to search")

    muckrock_sub = subparsers.add_parser(
        "muckrock", help="Search MuckRock for FOIA requests and government agencies",
    )
    muckrock_sub.add_argument("input", help="Agency name or search terms")
    muckrock_sub.add_argument("--mode", choices=["foia", "agency"], default="foia",
                              help="Search FOIA requests (default) or agencies")

    congress_sub = subparsers.add_parser(
        "congress", help="Search Congress.gov for members and bills (requires CONGRESS_API_KEY)",
    )
    congress_sub.add_argument("input", help="Member name or bill keyword")
    congress_sub.add_argument("--mode", choices=["member", "bill"], default="member",
                              help="Search members (default) or bills")

    # Playbook command
    playbook_sub = subparsers.add_parser(
        "playbook",
        help="Run a structured investigation playbook",
    )
    playbook_sub.add_argument(
        "playbook_name",
        choices=["username_to_identity", "name_to_surface", "org_to_members"],
        help="Playbook to run",
    )
    playbook_sub.add_argument("input", help="Seed input for the playbook")
    playbook_sub.add_argument(
        "--state", default="", help="State (for name_to_surface)",
    )
    playbook_sub.add_argument(
        "--city", default="", help="City (for name_to_surface)",
    )
    playbook_sub.add_argument(
        "--no-follow", action="store_true",
        help="Don't follow generated leads",
    )
    playbook_sub.add_argument(
        "--depth", type=int, default=1,
        help="Max lead-following depth (default: 1)",
    )
    playbook_sub.add_argument(
        "--min-score", type=float, default=0.5,
        help="Minimum lead score to follow (default: 0.5)",
    )
    playbook_sub.add_argument(
        "--investigation-name", default=None,
        help="Custom investigation name",
    )
    playbook_sub.add_argument(
        "--auto", action="store_true",
        help="Autonomous mode: keep following leads until done",
    )
    playbook_sub.add_argument(
        "--max-iterations", type=int, default=20,
        help="Max loop iterations in auto mode (default: 20)",
    )
    playbook_sub.add_argument(
        "--max-stale", type=int, default=3,
        help="Stop after N rounds with no new entities (default: 3)",
    )
    playbook_sub.add_argument(
        "--leads-per-round", type=int, default=3,
        help="Max leads to follow per iteration in auto mode (default: 3)",
    )

    # Search command with extra options
    search_sub = subparsers.add_parser("search", help="DuckDuckGo web/news search")
    search_sub.add_argument("input", help="Search query")
    search_sub.add_argument(
        "--mode",
        choices=["text", "news"],
        default="text",
        help="Search mode: text (web) or news (default: text)",
    )
    search_sub.add_argument(
        "--max-results",
        type=int,
        default=20,
        help="Maximum number of results (default: 20)",
    )

    # Report command
    report_sub = subparsers.add_parser(
        "report",
        help="Generate a structured investigation report from graph data",
    )
    report_sub.add_argument(
        "--investigation-id",
        type=int,
        default=None,
        help="Filter by investigation ID (default: all data)",
    )
    report_sub.add_argument(
        "--investigation-name",
        default=None,
        help="Investigation name for the report header",
    )
    report_sub.add_argument(
        "-o", "--output",
        default=None,
        help="Write report to file (default: print to stdout)",
    )

    graph_sub = subparsers.add_parser(
        "graph",
        help="Generate interactive Cytoscape.js graph visualization",
    )
    graph_sub.add_argument(
        "--investigation-id",
        type=int,
        default=None,
        help="Scope graph to a specific investigation ID",
    )
    graph_sub.add_argument(
        "--investigation-name",
        default=None,
        help="Title for the graph visualization",
    )
    graph_sub.add_argument(
        "-o", "--output",
        default=None,
        help="Write HTML to file (default: print to stdout)",
    )

    vault_sub = subparsers.add_parser(
        "vault",
        help="Export investigation as Obsidian vault (folder of Markdown files)",
    )
    vault_sub.add_argument(
        "--investigation-id",
        type=int,
        default=None,
        help="Scope export to a specific investigation ID",
    )
    vault_sub.add_argument(
        "--investigation-name",
        default=None,
        help="Title for the vault index page",
    )
    vault_sub.add_argument(
        "-o", "--output",
        default="reports/vault",
        help="Output directory (default: reports/vault)",
    )

    timeline_sub = subparsers.add_parser(
        "timeline",
        help="Generate chronological timeline from investigation data",
    )
    timeline_sub.add_argument(
        "--investigation-id",
        type=int,
        default=None,
        help="Scope timeline to a specific investigation ID",
    )
    timeline_sub.add_argument(
        "--investigation-name",
        default=None,
        help="Title for the timeline",
    )
    timeline_sub.add_argument(
        "-o", "--output",
        default=None,
        help="Write timeline to file (default: print to stdout)",
    )
    timeline_sub.add_argument(
        "--format",
        choices=["markdown", "html"],
        default="markdown",
        help="Output format (default: markdown)",
    )
    timeline_sub.add_argument(
        "--include-activity",
        action="store_true",
        help="Include investigation activity events (when each tool ran)",
    )

    analyze_sub = subparsers.add_parser(
        "analyze",
        help="LLM analysis: export data, run analysis, or ingest results",
    )
    analyze_sub.add_argument(
        "--export",
        action="store_true",
        help="Export investigation data as LLM-optimized JSON",
    )
    analyze_sub.add_argument(
        "--ingest",
        metavar="JSON_FILE",
        default=None,
        help="Ingest LLM-extracted entities/relationships/leads from a JSON file",
    )
    analyze_sub.add_argument(
        "--run",
        action="store_true",
        help="Run LLM analysis via API (auto-detects provider from env vars)",
    )
    analyze_sub.add_argument(
        "--provider",
        choices=["anthropic", "openai", "openrouter", "local"],
        default=None,
        help="LLM provider (default: auto-detect from env vars)",
    )
    analyze_sub.add_argument(
        "--model",
        default=None,
        help="Model name (default: provider-specific, e.g. claude-sonnet-4-20250514)",
    )
    analyze_sub.add_argument(
        "--base-url",
        default=None,
        help="API base URL (for local/custom providers, e.g. http://localhost:11434/v1)",
    )
    analyze_sub.add_argument(
        "--investigation-id",
        type=int,
        default=None,
        help="Scope to a specific investigation ID",
    )
    analyze_sub.add_argument(
        "--investigation-name",
        default=None,
        help="Investigation name (used in export metadata)",
    )
    analyze_sub.add_argument(
        "-o", "--output",
        default=None,
        help="Write export JSON to file (default: stdout)",
    )

    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
