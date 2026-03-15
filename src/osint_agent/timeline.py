"""Timeline reconstruction — extract temporal events and render chronologically.

Scans entity and relationship properties for known temporal keys (filing dates,
registration dates, account creation timestamps, etc.), normalizes heterogeneous
date formats, and renders as markdown or self-contained HTML.
"""

import calendar
import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import Enum

from osint_agent.models import Entity, Relationship
from osint_agent.report import _reconstruct_entity, _reconstruct_relationship


class DatePrecision(Enum):
    DAY = "day"
    MONTH = "month"
    YEAR = "year"


@dataclass
class TimelineEvent:
    date: date
    precision: DatePrecision
    entity_id: str
    entity_label: str
    entity_type: str
    property_key: str
    event_description: str
    source_tool: str


# Map property key -> human-readable event label.
# Only keys listed here are recognized as temporal.
TEMPORAL_KEYS: dict[str, str] = {
    "filing_date":        "SEC filing",
    "latest_action_date": "Congressional action",
    "patent_date":        "Patent issued",
    "registration_date":  "Registration",
    "stamped_date":       "FARA document stamped",
    "date_filed":         "Court filing",
    "most_recent_date":   "FEC donation",
    "datetime_submitted": "FOIA request submitted",
    "datetime_done":      "FOIA request completed",
    "start_date":         "Started",
    "end_date":           "Ended",
    "created":            "Account created",
    "created_at":         "Created",
    "timestamp":          "Archived",
    "award_year":         "SBIR award",
}


# ---------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------

def parse_temporal_value(raw) -> tuple[date, DatePrecision] | None:
    """Parse a temporal property value into (date, precision).

    Handles ISO datetime, YYYY-MM-DD, YYYY-MM, YYYY, and Unix timestamps.
    Returns None for unparseable values.
    """
    if isinstance(raw, (int, float)):
        try:
            dt = datetime.fromtimestamp(raw, tz=UTC)
            return (dt.date(), DatePrecision.DAY)
        except (OSError, ValueError, OverflowError):
            return None

    if not isinstance(raw, str) or not raw.strip():
        return None

    raw = raw.strip()

    # ISO datetime: 2023-06-15T12:00:00Z or 2023-06-15T12:00:00+00:00
    if "T" in raw:
        try:
            cleaned = raw.replace("Z", "+00:00")
            dt = datetime.fromisoformat(cleaned)
            return (dt.date(), DatePrecision.DAY)
        except ValueError:
            pass

    # YYYY-MM-DD
    parts = raw.split("-")
    if len(parts) == 3:
        try:
            return (date.fromisoformat(raw), DatePrecision.DAY)
        except ValueError:
            pass

    # YYYY-MM
    if len(parts) == 2:
        try:
            y, m = int(parts[0]), int(parts[1])
            if 1 <= m <= 12 and 1000 <= y <= 9999:
                return (date(y, m, 1), DatePrecision.MONTH)
        except ValueError:
            pass

    # YYYY (standalone year)
    if len(raw) == 4 and raw.isdigit():
        y = int(raw)
        if 1000 <= y <= 9999:
            return (date(y, 1, 1), DatePrecision.YEAR)

    return None


# ---------------------------------------------------------------
# Event extraction
# ---------------------------------------------------------------

def extract_events(
    entities: list[Entity],
    relationships: list[Relationship],
) -> list[TimelineEvent]:
    """Extract timeline events from entity/relationship temporal properties."""
    events: list[TimelineEvent] = []

    for entity in entities:
        tool = entity.sources[0].tool if entity.sources else "unknown"
        for key, label in TEMPORAL_KEYS.items():
            val = entity.properties.get(key)
            if val is None:
                continue
            parsed = parse_temporal_value(val)
            if parsed is None:
                continue
            d, prec = parsed
            events.append(TimelineEvent(
                date=d,
                precision=prec,
                entity_id=entity.id,
                entity_label=entity.label,
                entity_type=entity.entity_type.value,
                property_key=key,
                event_description=label,
                source_tool=tool,
            ))

    # Build entity lookup for relationship labeling
    entity_map = {e.id: e for e in entities}

    for rel in relationships:
        tool = rel.sources[0].tool if rel.sources else "unknown"
        src_entity = entity_map.get(rel.source_id)
        label = src_entity.label if src_entity else rel.source_id

        for key, desc in TEMPORAL_KEYS.items():
            val = rel.properties.get(key)
            if val is None:
                continue
            parsed = parse_temporal_value(val)
            if parsed is None:
                continue
            d, prec = parsed
            events.append(TimelineEvent(
                date=d,
                precision=prec,
                entity_id=rel.source_id,
                entity_label=label,
                entity_type=src_entity.entity_type.value if src_entity else "unknown",
                property_key=key,
                event_description=desc,
                source_tool=tool,
            ))

    return events


def extract_activity_events(
    finding_notes: list[dict],
) -> list[TimelineEvent]:
    """Extract investigation activity events from findings table rows."""
    events: list[TimelineEvent] = []
    for row in finding_notes:
        created = row.get("created_at", "")
        parsed = parse_temporal_value(created)
        if parsed is None:
            continue
        d, prec = parsed
        tool = row.get("tool", "unknown")
        notes = row.get("notes", "")
        # Truncate notes for display
        short = notes[:120] + "..." if len(notes) > 120 else notes
        events.append(TimelineEvent(
            date=d,
            precision=prec,
            entity_id="",
            entity_label=f"{tool} tool",
            entity_type="investigation",
            property_key="created_at",
            event_description=short,
            source_tool=tool,
        ))
    return events


# ---------------------------------------------------------------
# Markdown renderer
# ---------------------------------------------------------------

def _format_date(d: date, precision: DatePrecision) -> str:
    if precision == DatePrecision.YEAR:
        return str(d.year)
    if precision == DatePrecision.MONTH:
        return f"{d.year}-{d.month:02d}"
    return d.isoformat()


def _render_markdown(
    events: list[TimelineEvent],
    activity_events: list[TimelineEvent],
    title: str,
) -> str:
    if not events and not activity_events:
        return f"# Timeline: {title}\n\nNo timeline events found.\n"

    lines: list[str] = []
    lines.append(f"# Timeline: {title}")

    if events:
        earliest = min(e.date for e in events)
        latest = max(e.date for e in events)
        lines.append(f"*{len(events)} events spanning {earliest} to {latest}*")
        lines.append("")

        # Sort events by date ascending, group by year (descending), month
        sorted_events = sorted(events, key=lambda e: e.date)

        # Group by year
        years: dict[int, list[TimelineEvent]] = {}
        for ev in sorted_events:
            years.setdefault(ev.date.year, []).append(ev)

        for year in sorted(years.keys(), reverse=True):
            lines.append(f"## {year}")
            lines.append("")

            # Group by month within year
            months: dict[int, list[TimelineEvent]] = {}
            for ev in years[year]:
                month_key = ev.date.month if ev.precision != DatePrecision.YEAR else 0
                months.setdefault(month_key, []).append(ev)

            for month in sorted(months.keys(), reverse=True):
                if month == 0:
                    lines.append(f"### {year}")
                else:
                    month_name = calendar.month_name[month]
                    lines.append(f"### {month_name} {year}")
                lines.append("")

                for ev in months[month]:
                    date_str = _format_date(ev.date, ev.precision)
                    lines.append(
                        f"- **{date_str}** — **{ev.entity_label}** — "
                        f"{ev.event_description} *[{ev.source_tool}]*"
                    )
                lines.append("")

    if activity_events:
        lines.append("---")
        lines.append("## Investigation Activity")
        lines.append("")
        sorted_activity = sorted(activity_events, key=lambda e: e.date, reverse=True)
        for ev in sorted_activity:
            date_str = _format_date(ev.date, ev.precision)
            lines.append(
                f"- **{date_str}** — {ev.entity_label}: "
                f"\"{ev.event_description}\" *[{ev.source_tool}]*"
            )
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------
# HTML renderer
# ---------------------------------------------------------------

def _escape_html(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _render_html(
    events: list[TimelineEvent],
    activity_events: list[TimelineEvent],
    title: str,
) -> str:
    event_dicts = []
    for ev in sorted(events, key=lambda e: e.date, reverse=True):
        event_dicts.append({
            "date": ev.date.isoformat(),
            "precision": ev.precision.value,
            "date_display": _format_date(ev.date, ev.precision),
            "entity_id": ev.entity_id,
            "entity_label": ev.entity_label,
            "entity_type": ev.entity_type,
            "event_description": ev.event_description,
            "source_tool": ev.source_tool,
            "is_activity": False,
        })

    for ev in sorted(activity_events, key=lambda e: e.date, reverse=True):
        event_dicts.append({
            "date": ev.date.isoformat(),
            "precision": ev.precision.value,
            "date_display": _format_date(ev.date, ev.precision),
            "entity_id": "",
            "entity_label": ev.entity_label,
            "entity_type": "investigation",
            "event_description": ev.event_description,
            "source_tool": ev.source_tool,
            "is_activity": True,
        })

    entity_types = sorted({e["entity_type"] for e in event_dicts if not e["is_activity"]})
    source_tools = sorted({e["source_tool"] for e in event_dicts})

    html = _HTML_TEMPLATE
    html = html.replace("__EVENTS_JSON__", json.dumps(event_dicts, separators=(",", ":")))
    html = html.replace("__ENTITY_TYPES__", json.dumps(entity_types))
    html = html.replace("__SOURCE_TOOLS__", json.dumps(source_tools))
    html = html.replace("__TITLE__", _escape_html(title or "Investigation Timeline"))
    html = html.replace("__EVENT_COUNT__", str(len(events)))
    return html


# ---------------------------------------------------------------
# Generator class
# ---------------------------------------------------------------

class TimelineGenerator:
    """Generate timeline visualizations from investigation data."""

    async def generate(
        self,
        store,
        investigation_id: int | None = None,
        investigation_name: str = "",
        fmt: str = "markdown",
        include_activity: bool = False,
    ) -> str:
        if investigation_id is not None:
            entity_rows = await store.query(f"inv:{investigation_id}:all_nodes")
            rel_rows = await store.query(f"inv:{investigation_id}:all_edges")
        else:
            entity_rows = await store.query("all_nodes")
            rel_rows = await store.query("all_edges")

        entities = [_reconstruct_entity(r) for r in entity_rows]
        relationships = [_reconstruct_relationship(r) for r in rel_rows]

        finding_notes = None
        if include_activity:
            finding_notes = await store.get_finding_notes(
                investigation_id=investigation_id,
            )

        return self.generate_from_data(
            entities=entities,
            relationships=relationships,
            finding_notes=finding_notes,
            investigation_name=investigation_name,
            fmt=fmt,
            include_activity=include_activity,
        )

    def generate_from_data(
        self,
        entities: list[Entity],
        relationships: list[Relationship],
        finding_notes: list[dict] | None = None,
        investigation_name: str = "",
        fmt: str = "markdown",
        include_activity: bool = False,
    ) -> str:
        events = extract_events(entities, relationships)
        activity = []
        if include_activity and finding_notes:
            activity = extract_activity_events(finding_notes)

        title = investigation_name or "Investigation"

        if fmt == "html":
            return _render_html(events, activity, title)
        return _render_markdown(events, activity, title)


# ---------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------

_TYPE_COLORS = {
    "person": "#89b4fa",
    "organization": "#f5c2e7",
    "email": "#a6e3a1",
    "domain": "#cba6f7",
    "phone": "#f9e2af",
    "address": "#94e2d5",
    "account": "#fab387",
    "document": "#74c7ec",
    "fec_committee": "#f38ba8",
    "vehicle": "#b4befe",
    "ip_address": "#eba0ac",
    "username": "#f2cdcd",
    "url": "#89dceb",
    "investigation": "#6c7086",
}

_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,monospace;
  background:#1e1e2e;color:#cdd6f4;line-height:1.6;display:flex;min-height:100vh}
a{color:#89b4fa;text-decoration:none}

/* Sidebar */
.sidebar{width:240px;background:#181825;padding:20px;border-right:1px solid #313244;
  overflow-y:auto;flex-shrink:0}
.sidebar h2{font-size:14px;color:#a6adc8;text-transform:uppercase;letter-spacing:1px;
  margin:16px 0 8px}
.sidebar label{display:flex;align-items:center;gap:6px;padding:3px 0;
  font-size:13px;cursor:pointer;color:#bac2de}
.sidebar label:hover{color:#cdd6f4}
.sidebar input[type=checkbox]{accent-color:#89b4fa}

/* Main content */
.main{flex:1;padding:40px;max-width:900px;margin:0 auto}
.header{margin-bottom:32px;border-bottom:1px solid #313244;padding-bottom:16px}
.header h1{font-size:24px;color:#cdd6f4;font-weight:600}
.header .meta{font-size:13px;color:#6c7086;margin-top:4px}

/* Timeline */
.timeline{position:relative;padding:20px 0;padding-left:40px}
.timeline::before{content:"";position:absolute;left:16px;top:0;bottom:0;
  width:2px;background:#313244}

.year-group{margin-bottom:24px}
.year-header{font-size:18px;font-weight:700;color:#a6adc8;margin:24px 0 12px;
  cursor:pointer;user-select:none;position:relative;padding-left:0}
.year-header::before{content:"▾";margin-right:8px;font-size:12px;
  display:inline-block;transition:transform 0.2s}
.year-header.collapsed::before{transform:rotate(-90deg)}

.event{position:relative;margin-bottom:16px;padding:12px 16px;
  background:#181825;border:1px solid #313244;border-radius:8px;
  transition:opacity 0.2s, border-color 0.2s}
.event:hover{border-color:#45475a}
.event.hidden{display:none}
.event.activity{opacity:0.5;border-style:dashed}

.event-dot{position:absolute;left:-32px;top:16px;width:12px;height:12px;
  border-radius:50%;border:2px solid #1e1e2e}
.event.activity .event-dot{border-radius:2px}

.event-date{font-size:12px;color:#6c7086;font-weight:600;letter-spacing:0.5px}
.event-label{font-size:15px;font-weight:600;margin:2px 0}
.event-desc{font-size:13px;color:#a6adc8}
.event-tool{display:inline-block;font-size:11px;padding:1px 6px;border-radius:4px;
  background:#313244;color:#a6adc8;margin-top:4px}
.type-badge{display:inline-block;font-size:10px;padding:1px 5px;border-radius:3px;
  margin-left:6px;text-transform:uppercase;letter-spacing:0.5px}

.empty-msg{text-align:center;padding:60px 20px;color:#6c7086;font-size:16px}
</style>
</head>
<body>
<div class="sidebar">
  <h1 style="font-size:16px;margin-bottom:8px">Filters</h1>
  <h2>Entity Types</h2>
  <div id="type-filters"></div>
  <h2>Source Tools</h2>
  <div id="tool-filters"></div>
  <h2 style="margin-top:24px">Options</h2>
  <label><input type="checkbox" id="show-activity"> Show activity events</label>
</div>
<div class="main">
  <div class="header">
    <h1>__TITLE__</h1>
    <div class="meta">__EVENT_COUNT__ events</div>
  </div>
  <div id="timeline" class="timeline"></div>
</div>
<script>
(function(){
  const events = __EVENTS_JSON__;
  const entityTypes = __ENTITY_TYPES__;
  const sourceTools = __SOURCE_TOOLS__;
  const typeColors = {
    "person":"#89b4fa","organization":"#f5c2e7","email":"#a6e3a1",
    "domain":"#cba6f7","phone":"#f9e2af","address":"#94e2d5",
    "account":"#fab387","document":"#74c7ec","fec_committee":"#f38ba8",
    "vehicle":"#b4befe","ip_address":"#eba0ac","username":"#f2cdcd",
    "url":"#89dceb","investigation":"#6c7086"
  };

  // Build filter checkboxes
  const typeDiv = document.getElementById("type-filters");
  entityTypes.forEach(t => {
    const c = typeColors[t] || "#cdd6f4";
    const lbl = document.createElement("label");
    lbl.innerHTML = `<input type="checkbox" checked data-type="${t}">
      <span style="color:${c}">${t}</span>`;
    typeDiv.appendChild(lbl);
  });

  const toolDiv = document.getElementById("tool-filters");
  sourceTools.forEach(t => {
    const lbl = document.createElement("label");
    lbl.innerHTML = `<input type="checkbox" checked data-tool="${t}"> ${t}`;
    toolDiv.appendChild(lbl);
  });

  function render() {
    const tl = document.getElementById("timeline");
    const checkedTypes = new Set(
      [...document.querySelectorAll("[data-type]:checked")].map(c => c.dataset.type)
    );
    const checkedTools = new Set(
      [...document.querySelectorAll("[data-tool]:checked")].map(c => c.dataset.tool)
    );
    const showActivity = document.getElementById("show-activity").checked;

    const visible = events.filter(e => {
      if (e.is_activity) return showActivity;
      return checkedTypes.has(e.entity_type) && checkedTools.has(e.source_tool);
    });

    if (visible.length === 0) {
      tl.innerHTML = '<div class="empty-msg">No timeline events match current filters.</div>';
      return;
    }

    // Group by year
    const years = {};
    visible.forEach(e => {
      const y = e.date.substring(0, 4);
      if (!years[y]) years[y] = [];
      years[y].push(e);
    });

    let html = "";
    Object.keys(years).sort().reverse().forEach(year => {
      html += `<div class="year-group"><div class="year-header" onclick="this.classList.toggle('collapsed');this.nextElementSibling.style.display=this.classList.contains('collapsed')?'none':'block'">${year}</div><div class="year-events">`;
      years[year].forEach(e => {
        const c = typeColors[e.entity_type] || "#cdd6f4";
        const cls = e.is_activity ? "event activity" : "event";
        html += `<div class="${cls}">
          <div class="event-dot" style="background:${c}"></div>
          <div class="event-date">${e.date_display}</div>
          <div class="event-label" style="color:${c}">${esc(e.entity_label)}
            <span class="type-badge" style="background:${c}22;color:${c}">${e.entity_type}</span>
          </div>
          <div class="event-desc">${esc(e.event_description)}</div>
          <span class="event-tool">${esc(e.source_tool)}</span>
        </div>`;
      });
      html += "</div></div>";
    });
    tl.innerHTML = html;
  }

  function esc(s) {
    const d = document.createElement("div");
    d.textContent = s;
    return d.innerHTML;
  }

  document.querySelectorAll(".sidebar input").forEach(cb => cb.addEventListener("change", render));
  render();
})();
</script>
</body>
</html>"""
