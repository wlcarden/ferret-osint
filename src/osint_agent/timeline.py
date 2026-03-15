"""Timeline reconstruction — extract temporal events and render chronologically.

Scans entity and relationship properties for known temporal keys (filing dates,
registration dates, account creation timestamps, etc.), normalizes heterogeneous
date formats, and renders as markdown or self-contained HTML.

Supports precision from YEAR down to SUBSECOND (~5 decimal places) for
reconstructing event sequences where timing matters (e.g., incident timelines).
"""

import calendar
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import Enum

from osint_agent.models import Entity, Relationship, RelationType
from osint_agent.report import _reconstruct_entity, _reconstruct_relationship
from osint_agent.theme import type_colors_js


class DatePrecision(Enum):
    """Temporal precision levels, coarsest to finest."""

    YEAR = "year"
    MONTH = "month"
    DAY = "day"
    SECOND = "second"
    SUBSECOND = "subsecond"


# Ordered list for comparison (coarsest → finest).
_PRECISION_ORDER = [
    DatePrecision.YEAR,
    DatePrecision.MONTH,
    DatePrecision.DAY,
    DatePrecision.SECOND,
    DatePrecision.SUBSECOND,
]


@dataclass
class TimelineEvent:
    timestamp: datetime
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
    # OSINT-specific adapter keys
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
    # Generic keys for manual/event-centric data
    "event_time":         "Event",
    "occurred_at":        "Occurred",
    "reported_at":        "Reported",
    "detected_at":        "Detected",
}


# ---------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------

def parse_temporal_value(raw) -> tuple[datetime, DatePrecision] | None:
    """Parse a temporal property value into (datetime, precision).

    Handles ISO datetime, YYYY-MM-DD, YYYY-MM, YYYY, and Unix timestamps.
    Returns None for unparseable values.

    Precision rules:
    - ISO datetime with fractional seconds → SUBSECOND
    - ISO datetime without fractional seconds → SECOND
    - YYYY-MM-DD → DAY
    - YYYY-MM → MONTH
    - YYYY → YEAR
    - Numeric float with fractional part → SUBSECOND
    - Numeric integer → SECOND
    """
    if isinstance(raw, (int, float)):
        try:
            dt = datetime.fromtimestamp(raw, tz=UTC)
            has_frac = isinstance(raw, float) and raw != int(raw)
            prec = DatePrecision.SUBSECOND if has_frac else DatePrecision.SECOND
            return (dt, prec)
        except (OSError, ValueError, OverflowError):
            return None

    if not isinstance(raw, str) or not raw.strip():
        return None

    raw = raw.strip()

    # ISO datetime: 2023-06-15T12:00:00Z or 2023-06-15T01:23:40.12345+00:00
    if "T" in raw:
        try:
            cleaned = raw.replace("Z", "+00:00")
            dt = datetime.fromisoformat(cleaned)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            prec = DatePrecision.SUBSECOND if dt.microsecond > 0 else DatePrecision.SECOND
            return (dt, prec)
        except ValueError:
            pass

    # YYYY-MM-DD
    parts = raw.split("-")
    if len(parts) == 3:
        try:
            d = date.fromisoformat(raw)
            return (datetime(d.year, d.month, d.day, tzinfo=UTC), DatePrecision.DAY)
        except ValueError:
            pass

    # YYYY-MM
    if len(parts) == 2:
        try:
            y, m = int(parts[0]), int(parts[1])
            if 1 <= m <= 12 and 1000 <= y <= 9999:
                return (datetime(y, m, 1, tzinfo=UTC), DatePrecision.MONTH)
        except ValueError:
            pass

    # YYYY (standalone year)
    if len(raw) == 4 and raw.isdigit():
        y = int(raw)
        if 1000 <= y <= 9999:
            return (datetime(y, 1, 1, tzinfo=UTC), DatePrecision.YEAR)

    return None


# ---------------------------------------------------------------
# Event extraction
# ---------------------------------------------------------------

def extract_events(
    entities: list[Entity],
    relationships: list[Relationship],
) -> list[TimelineEvent]:
    """Extract timeline events from entity/relationship temporal properties.

    Supports the ``{key}_label`` companion convention: if an entity has both
    ``event_time`` and ``event_time_label``, the label value overrides the
    generic description from TEMPORAL_KEYS.
    """
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
            ts, prec = parsed
            # Companion label override: {key}_label property
            desc = entity.properties.get(f"{key}_label", label)
            events.append(TimelineEvent(
                timestamp=ts,
                precision=prec,
                entity_id=entity.id,
                entity_label=entity.label,
                entity_type=entity.entity_type.value,
                property_key=key,
                event_description=desc,
                source_tool=tool,
            ))

    # Build entity lookup for relationship labeling
    entity_map = {e.id: e for e in entities}

    for rel in relationships:
        tool = rel.sources[0].tool if rel.sources else "unknown"
        src_entity = entity_map.get(rel.source_id)
        tgt_entity = entity_map.get(rel.target_id)
        label = src_entity.label if src_entity else rel.source_id

        for key, generic_desc in TEMPORAL_KEYS.items():
            val = rel.properties.get(key)
            if val is None:
                continue
            parsed = parse_temporal_value(val)
            if parsed is None:
                continue
            ts, prec = parsed
            # Companion label override on relationship properties
            desc = rel.properties.get(f"{key}_label")
            if not desc:
                # Enrich generic description with relationship context
                desc = generic_desc
                if tgt_entity:
                    rel_verb = rel.relation_type.value.replace("_", " ")
                    desc = f"{desc} ({rel_verb} {tgt_entity.label})"
            events.append(TimelineEvent(
                timestamp=ts,
                precision=prec,
                entity_id=rel.source_id,
                entity_label=label,
                entity_type=src_entity.entity_type.value if src_entity else "unknown",
                property_key=key,
                event_description=desc,
                source_tool=tool,
            ))

    # Enrich event descriptions with contextual (non-temporal) relationships.
    # E.g., OCCURRED_AT adds "at Reactor No. 4", PARTICIPATED_IN adds actor names.
    _CONTEXT_RELATIONS = {
        RelationType.OCCURRED_AT: "at",
        RelationType.LOCATED_AT: "at",
        RelationType.PARTICIPATED_IN: None,  # actors listed separately
    }
    # Index: target_id → [(source_label, rel_type)] for PARTICIPATED_IN (actors→event)
    # Index: source_id → [(target_label, rel_type)] for OCCURRED_AT (event→location)
    actors_by_event: dict[str, list[str]] = {}
    location_by_event: dict[str, str] = {}
    for rel in relationships:
        if rel.relation_type == RelationType.PARTICIPATED_IN:
            src = entity_map.get(rel.source_id)
            if src:
                actors_by_event.setdefault(rel.target_id, []).append(src.label)
        elif rel.relation_type in (RelationType.OCCURRED_AT, RelationType.LOCATED_AT):
            tgt = entity_map.get(rel.target_id)
            if tgt:
                location_by_event[rel.source_id] = tgt.label

    for ev in events:
        suffixes = []
        loc = location_by_event.get(ev.entity_id)
        if loc:
            suffixes.append(f"at {loc}")
        actors = actors_by_event.get(ev.entity_id)
        if actors:
            suffixes.append(", ".join(sorted(actors)))
        if suffixes:
            ev.event_description += f" [{'; '.join(suffixes)}]"

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
        ts, prec = parsed
        tool = row.get("tool", "unknown")
        notes = row.get("notes", "")
        # Truncate notes for display
        short = notes[:120] + "..." if len(notes) > 120 else notes
        events.append(TimelineEvent(
            timestamp=ts,
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
# Timestamp formatting
# ---------------------------------------------------------------

def _format_timestamp(
    ts: datetime,
    precision: DatePrecision,
    time_only: bool = False,
) -> str:
    """Format a datetime according to its precision level.

    When ``time_only`` is True and precision is SECOND or SUBSECOND,
    omit the date prefix (used inside day-subgroup headers where the
    date is already shown).
    """
    if precision == DatePrecision.YEAR:
        return str(ts.year)
    if precision == DatePrecision.MONTH:
        return f"{ts.year}-{ts.month:02d}"
    if precision == DatePrecision.DAY:
        return ts.strftime("%Y-%m-%d")
    if precision == DatePrecision.SECOND:
        if time_only:
            return ts.strftime("%H:%M:%S")
        return ts.strftime("%Y-%m-%d %H:%M:%S")
    # SUBSECOND — up to 5 decimal places, trailing zeros stripped
    frac = f"{ts.microsecond:06d}"[:5]
    frac = frac.rstrip("0") or "0"
    if time_only:
        return f"{ts:%H:%M:%S}.{frac}"
    return f"{ts:%Y-%m-%d %H:%M:%S}.{frac}"


# ---------------------------------------------------------------
# Markdown renderer
# ---------------------------------------------------------------

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
        earliest_ev = min(events, key=lambda e: e.timestamp)
        latest_ev = max(events, key=lambda e: e.timestamp)
        earliest_str = _format_timestamp(earliest_ev.timestamp, earliest_ev.precision)
        latest_str = _format_timestamp(latest_ev.timestamp, latest_ev.precision)
        lines.append(
            f"*{len(events)} events spanning {earliest_str} to {latest_str}*",
        )
        lines.append("")

        # Sort events by timestamp ascending, group by year (descending), month
        sorted_events = sorted(events, key=lambda e: e.timestamp)

        # Group by year
        years: dict[int, list[TimelineEvent]] = {}
        for ev in sorted_events:
            years.setdefault(ev.timestamp.year, []).append(ev)

        for year in sorted(years.keys(), reverse=True):
            lines.append(f"## {year}")
            lines.append("")

            # Group by month within year
            months: dict[int, list[TimelineEvent]] = {}
            for ev in years[year]:
                month_key = ev.timestamp.month if ev.precision != DatePrecision.YEAR else 0
                months.setdefault(month_key, []).append(ev)

            for month in sorted(months.keys(), reverse=True):
                if month == 0:
                    lines.append(f"### {year}")
                else:
                    month_name = calendar.month_name[month]
                    lines.append(f"### {month_name} {year}")
                lines.append("")

                month_events = months[month]
                # Add day subheadings when sub-day events span multiple days
                has_subday = any(
                    ev.precision in (DatePrecision.SECOND, DatePrecision.SUBSECOND)
                    for ev in month_events
                )
                distinct_days = {ev.timestamp.date() for ev in month_events}
                use_day_groups = has_subday and len(distinct_days) > 1

                prev_day = None
                for ev in month_events:
                    if use_day_groups:
                        ev_day = ev.timestamp.date()
                        if ev_day != prev_day:
                            if prev_day is not None:
                                lines.append("")
                            lines.append(f"#### {ev_day.isoformat()}")
                            lines.append("")
                            prev_day = ev_day
                    # Inside day subgroups, show time-only for sub-day events
                    ts_str = _format_timestamp(
                        ev.timestamp, ev.precision, time_only=use_day_groups,
                    )
                    lines.append(
                        f"- **{ts_str}** — **{ev.entity_label}** — "
                        f"{ev.event_description} *[{ev.source_tool}]*"
                    )
                lines.append("")

    if activity_events:
        lines.append("---")
        lines.append("## Investigation Activity")
        lines.append("")
        sorted_activity = sorted(
            activity_events, key=lambda e: e.timestamp, reverse=True,
        )
        for ev in sorted_activity:
            ts_str = _format_timestamp(ev.timestamp, ev.precision)
            lines.append(
                f"- **{ts_str}** — {ev.entity_label}: "
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
    for ev in sorted(events, key=lambda e: e.timestamp, reverse=True):
        event_dicts.append({
            "date": ev.timestamp.isoformat(),
            "precision": ev.precision.value,
            "date_display": _format_timestamp(ev.timestamp, ev.precision),
            "entity_id": ev.entity_id,
            "entity_label": ev.entity_label,
            "entity_type": ev.entity_type,
            "event_description": ev.event_description,
            "source_tool": ev.source_tool,
            "is_activity": False,
        })

    for ev in sorted(activity_events, key=lambda e: e.timestamp, reverse=True):
        event_dicts.append({
            "date": ev.timestamp.isoformat(),
            "precision": ev.precision.value,
            "date_display": _format_timestamp(ev.timestamp, ev.precision),
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
    html = html.replace("__TYPE_COLORS__", type_colors_js())
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

.day-header{font-size:13px;font-weight:600;color:#585b70;margin:16px 0 8px;
  padding:2px 8px;border-left:2px solid #45475a;letter-spacing:0.5px}

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
  const typeColors = __TYPE_COLORS__;

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
      html += `<div class="year-group"><div class="year-header"
onclick="this.classList.toggle('collapsed');
this.nextElementSibling.style.display=
this.classList.contains('collapsed')?'none':'block'"
>${year}</div><div class="year-events">`;
      const yEvents = years[year];
      // Detect if day subgroups are needed (sub-day events spanning multiple days)
      const hasSubday = yEvents.some(e => e.precision === "second" || e.precision === "subsecond");
      const days = new Set(yEvents.map(e => e.date.substring(0, 10)));
      const useDayGroups = hasSubday && days.size > 1;
      let prevDay = "";
      yEvents.forEach(e => {
        if (useDayGroups) {
          const day = e.date.substring(0, 10);
          if (day !== prevDay) {
            html += `<div class="day-header">${day}</div>`;
            prevDay = day;
          }
        }
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
