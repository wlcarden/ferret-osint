"""Rich CLI output helpers for the OSINT toolkit.

Thin wrapper around rich.console.Console exposing domain-specific
functions.  Callers never import Rich directly — they call these
functions with domain objects and strings.

All Rich output goes to stderr so that structured outputs (markdown
reports, HTML graphs) written to stdout remain pipeable.
"""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from osint_agent.theme import TYPE_COLORS

_console = Console(stderr=True)

# Map hex colors to Rich color markup for entity types.
_RICH_COLORS: dict[str, str] = {
    k: v for k, v in TYPE_COLORS.items()
}


# ── Primitives ────────────────────────────────────────────────────


def heading(text: str, level: int = 1) -> None:
    """Print a styled section heading.

    level 1: boxed panel (major sections like "Phase 1")
    level 2: bold rule line (sub-sections)
    """
    if level <= 1:
        _console.print(
            Panel(
                Text(text, style="bold"),
                border_style="blue",
                padding=(0, 1),
            ),
        )
    else:
        _console.rule(f"[bold]{text}[/bold]", style="dim")


def status(msg: str, tool: str | None = None) -> None:
    """Print a status/progress line."""
    if tool:
        _console.print(f"  [dim]\\[{tool}][/dim] {msg}")
    else:
        _console.print(f"  {msg}")


def success(msg: str) -> None:
    """Print a success message."""
    _console.print(f"  [green]{msg}[/green]")


def warning(msg: str) -> None:
    """Print a warning message."""
    _console.print(f"  [yellow]WARNING:[/yellow] {msg}")


def error(msg: str, suggestion: str = "") -> None:
    """Print an error with optional remediation hint."""
    _console.print(f"  [red]ERROR:[/red] {msg}")
    if suggestion:
        _console.print(f"    [dim]->[/dim] {suggestion}")


def tool_error(err) -> None:
    """Print a structured ToolError."""
    cat = err.category.value if hasattr(err.category, "value") else err.category
    _console.print(
        f"  [red]ERROR[/red] "
        f"[dim]\\[{cat}][/dim] "
        f"{err.tool}: {err.message}",
    )
    if err.suggestion:
        _console.print(f"    [dim]->[/dim] {err.suggestion}")


def cache_hit(tool_name: str) -> None:
    """Print a cache hit notice."""
    _console.print(f"  [cyan]CACHE HIT[/cyan] {tool_name}")


def normalized(original: str, result: str) -> None:
    """Print an input normalization notice."""
    _console.print(
        f"  [dim]normalized[/dim] '{original}' -> '{result}'",
    )


# ── Findings ──────────────────────────────────────────────────────


def _type_style(entity_type: str) -> str:
    """Return a Rich color string for an entity type."""
    color = _RICH_COLORS.get(entity_type, "white")
    return color


def finding(
    entities: list,
    relationships: list,
    notes: str | None = None,
    error_obj=None,
) -> None:
    """Print a structured finding — replaces print_finding()."""
    if notes:
        _console.print(f"\n  {notes}")

    if not entities and not relationships:
        return

    # Summary line
    type_counts: dict[str, int] = {}
    for e in entities:
        t = e.entity_type.value
        type_counts[t] = type_counts.get(t, 0) + 1
    parts = [f"{count} {t}" for t, count in sorted(type_counts.items())]
    if relationships:
        parts.append(f"{len(relationships)} rel")
    if parts:
        _console.print(f"  [bold]Summary:[/bold] {', '.join(parts)}")

    # Multi-source entities (corroborated across tools)
    multi = [e for e in entities if len(e.sources) > 1]
    if multi:
        _console.print(
            f"\n  [bold yellow]MULTI-SOURCE[/bold yellow] "
            f"({len(multi)} entities from 2+ tools)",
        )
        for e in multi[:10]:
            tools = ", ".join(s.tool for s in e.sources)
            color = _type_style(e.entity_type.value)
            _console.print(
                f"    [{color}]{e.label}[/{color}] "
                f"[dim]\\[{e.entity_type.value}][/dim] ({tools})",
            )
        if len(multi) > 10:
            _console.print(f"    [dim]... and {len(multi) - 10} more[/dim]")

    # Group remaining by type
    multi_ids = {e.id for e in multi}
    by_type: dict[str, list] = {}
    for e in entities:
        if e.id in multi_ids:
            continue
        by_type.setdefault(e.entity_type.value, []).append(e)

    for etype, ents in sorted(by_type.items()):
        color = _type_style(etype)
        _console.print(
            f"\n  [{color}]{etype.upper()}[/{color}] ({len(ents)})",
        )
        for e in ents[:15]:
            detail = _entity_detail(e)
            _console.print(f"    {e.label}{detail}")
        if len(ents) > 15:
            _console.print(
                f"    [dim]... and {len(ents) - 15} more[/dim]",
            )

    if relationships:
        _console.print(
            f"\n  [bold]Relationships:[/bold] {len(relationships)}",
        )
        rel_counts: dict[str, int] = {}
        for r in relationships:
            rt = r.relation_type.value
            rel_counts[rt] = rel_counts.get(rt, 0) + 1
        for rtype, count in sorted(rel_counts.items()):
            _console.print(f"    {rtype}: {count}")


def _entity_detail(e) -> str:
    """Extract the most relevant detail string for an entity."""
    p = e.properties
    if p.get("url"):
        return f" -> {p['url']}"
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


# ── Tables ────────────────────────────────────────────────────────


def investigation_table(investigations: list[dict]) -> None:
    """Print investigation listing as a Rich table."""
    table = Table(show_header=True, header_style="bold")
    table.add_column("ID", justify="right", style="cyan", width=5)
    table.add_column("Name", min_width=30)
    table.add_column("Created", style="dim")

    for inv in investigations:
        created = inv["created_at"][:19].replace("T", " ")
        table.add_row(str(inv["id"]), inv["name"], created)

    _console.print(table)


def entity_resolution_table(aka_rels: list) -> None:
    """Print entity resolution results."""
    _console.print(
        f"\n  [bold]Entity Resolution:[/bold] "
        f"{len(aka_rels)} cross-source links found",
    )
    for rel in aka_rels:
        conf = rel.properties.get("confidence", 0)
        src = rel.properties.get("source_label", rel.source_id)
        tgt = rel.properties.get("target_label", rel.target_id)
        level = (
            "HIGH" if conf >= 0.8
            else "MEDIUM" if conf >= 0.6
            else "LOW"
        )
        style = (
            "green" if conf >= 0.8
            else "yellow" if conf >= 0.6
            else "red"
        )
        _console.print(
            f"    {src} <-> {tgt} "
            f"[{style}]\\[{level} {conf:.0%}][/{style}]",
        )


def graph_summary(entity_count: int, rel_count: int) -> None:
    """Print graph entity/relationship counts."""
    _console.print(
        f"\n  [bold]Graph:[/bold] "
        f"{entity_count} entities, {rel_count} relationships",
    )


# ── Key validation ────────────────────────────────────────────────


def key_status(
    name: str,
    ok: bool,
    detail: str = "",
) -> None:
    """Print API key validation result."""
    if ok:
        badge = "[green]  OK[/green]"
    else:
        badge = "[red]FAIL[/red]"
    _console.print(f"    \\[{badge}] {name}: {detail}")


def validation_report(
    results: list[tuple[str, bool, str]],
) -> None:
    """Print a formatted API key validation report."""
    if not results:
        _console.print("  No API keys configured to validate.")
        return

    _console.print("  [bold]API Key Validation:[/bold]")
    for name, valid, msg in sorted(
        results, key=lambda r: (r[1], r[0]),
    ):
        key_status(name, valid, msg)

    valid_count = sum(1 for _, v, _ in results if v)
    total = len(results)
    if valid_count < total:
        invalid = total - valid_count
        warning(
            f"{invalid} key(s) failed validation "
            "-- affected tools may error during investigation",
        )


# ── Playbook runner ───────────────────────────────────────────────


def phase_heading(text: str) -> None:
    """Print a phase divider for playbook runners."""
    _console.rule(f"[bold]{text}[/bold]", style="blue")


def step_status(
    action: str,
    description: str,
    extra: str = "",
) -> None:
    """Print a playbook step status (RUN, SKIP, CACHE, etc.)."""
    styles = {
        "RUN": "green",
        "SKIP": "yellow",
        "CACHE": "cyan",
        "ERROR": "red",
    }
    style = styles.get(action, "white")
    line = f"  [{style}]{action:>5}[/{style}]  {description}"
    if extra:
        line += f" [dim]({extra})[/dim]"
    _console.print(line)
