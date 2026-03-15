"""Persistent GraphStore backed by SQLite.

Provides the same interface as MemoryStore but persists the entity graph
to a SQLite database file. Supports multi-session, multi-target
investigations — the graph accumulates across CLI invocations.

Uses aiosqlite for async compatibility. Properties and sources are stored
as JSON text columns, matching the flexible dict-based model.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from osint_agent.graph.store import GraphStore
from osint_agent.models import Entity, Finding, Relationship

# Default database location: data/graph.db in the project root
DEFAULT_DB_PATH = Path(__file__).resolve().parents[3] / "data" / "graph.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entities (
    id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL,
    label TEXT NOT NULL,
    properties TEXT NOT NULL DEFAULT '{}',
    sources TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS relationships (
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    properties TEXT NOT NULL DEFAULT '{}',
    sources TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (source_id, target_id, relation_type)
);

CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(entity_type);
CREATE INDEX IF NOT EXISTS idx_rel_source ON relationships(source_id);
CREATE INDEX IF NOT EXISTS idx_rel_target ON relationships(target_id);
CREATE INDEX IF NOT EXISTS idx_rel_type ON relationships(relation_type);

CREATE TABLE IF NOT EXISTS investigations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    notes TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    investigation_id INTEGER,
    entity_id TEXT,
    lead_type TEXT NOT NULL,
    value TEXT NOT NULL,
    score REAL DEFAULT 0.0,
    status TEXT DEFAULT 'pending',
    notes TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (investigation_id) REFERENCES investigations(id)
);

CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);
CREATE INDEX IF NOT EXISTS idx_leads_score ON leads(score);
CREATE INDEX IF NOT EXISTS idx_leads_investigation ON leads(investigation_id);

CREATE TABLE IF NOT EXISTS investigation_entities (
    investigation_id INTEGER NOT NULL,
    entity_id TEXT NOT NULL,
    PRIMARY KEY (investigation_id, entity_id),
    FOREIGN KEY (investigation_id) REFERENCES investigations(id),
    FOREIGN KEY (entity_id) REFERENCES entities(id)
);

CREATE INDEX IF NOT EXISTS idx_inv_ent_inv ON investigation_entities(investigation_id);
CREATE INDEX IF NOT EXISTS idx_inv_ent_ent ON investigation_entities(entity_id);

CREATE TABLE IF NOT EXISTS findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    investigation_id INTEGER,
    tool TEXT NOT NULL DEFAULT 'unknown',
    notes TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (investigation_id) REFERENCES investigations(id)
);
CREATE INDEX IF NOT EXISTS idx_findings_inv ON findings(investigation_id);
"""


class SqliteStore(GraphStore):
    """Graph store backed by SQLite for persistent, multi-session investigations.

    Data persists in a single .db file (default: data/graph.db).
    Supports all MemoryStore operations plus investigation and lead tracking.
    """

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = str(db_path or DEFAULT_DB_PATH)
        self._db: aiosqlite.Connection | None = None

    async def _ensure_db(self) -> aiosqlite.Connection:
        """Lazily open database and ensure schema exists."""
        if self._db is None:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            self._db = await aiosqlite.connect(self.db_path)
            self._db.row_factory = aiosqlite.Row
            await self._db.executescript(_SCHEMA)
            await self._db.execute("PRAGMA journal_mode=WAL")
            await self._db.execute("PRAGMA foreign_keys=ON")
            await self._db.commit()
        return self._db

    async def close(self):
        """Close the database connection."""
        if self._db:
            await self._db.close()
            self._db = None

    async def link_entity_to_investigation(
        self, entity_id: str, investigation_id: int,
    ) -> None:
        """Associate an entity with an investigation (idempotent)."""
        db = await self._ensure_db()
        await db.execute(
            """INSERT OR IGNORE INTO investigation_entities
               (investigation_id, entity_id) VALUES (?, ?)""",
            (investigation_id, entity_id),
        )
        await db.commit()

    async def ingest_finding(
        self, finding: Finding, investigation_id: int | None = None,
    ) -> None:
        """Ingest a Finding, optionally linking entities to an investigation."""
        for entity in finding.entities:
            await self.merge_entity(entity)
            if investigation_id is not None:
                await self.link_entity_to_investigation(
                    entity.id, investigation_id,
                )
        for rel in finding.relationships:
            await self.merge_relationship(rel)

        # Persist finding notes if present
        if finding.notes:
            db = await self._ensure_db()
            now = datetime.now(UTC).isoformat()
            tool = _infer_tool_name(finding)
            await db.execute(
                """INSERT INTO findings
                   (investigation_id, tool, notes, created_at)
                   VALUES (?, ?, ?, ?)""",
                (investigation_id, tool, finding.notes, now),
            )
            await db.commit()

    async def merge_entity(self, entity: Entity) -> None:
        """Insert or update an entity. Merges sources on conflict."""
        db = await self._ensure_db()
        now = datetime.now(UTC).isoformat()
        sources_json = json.dumps(
            [s.model_dump(mode="json") for s in entity.sources],
        )
        props_json = json.dumps(entity.properties)

        # Check if entity exists to merge sources
        existing = await db.execute(
            "SELECT sources FROM entities WHERE id = ?",
            (entity.id,),
        )
        row = await existing.fetchone()

        if row:
            # Merge sources: combine existing + new, deduplicate by source_url
            existing_sources = json.loads(row["sources"])
            new_sources = json.loads(sources_json)
            merged = _merge_sources(existing_sources, new_sources)
            await db.execute(
                """UPDATE entities
                   SET label = ?, entity_type = ?, properties = ?,
                       sources = ?, updated_at = ?
                   WHERE id = ?""",
                (
                    entity.label,
                    entity.entity_type.value,
                    props_json,
                    json.dumps(merged),
                    now,
                    entity.id,
                ),
            )
        else:
            await db.execute(
                """INSERT INTO entities
                   (id, entity_type, label, properties,
                    sources, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    entity.id,
                    entity.entity_type.value,
                    entity.label,
                    props_json,
                    sources_json,
                    now,
                    now,
                ),
            )
        await db.commit()

    async def merge_relationship(self, rel: Relationship) -> None:
        """Insert or update a relationship. Merges sources on conflict."""
        db = await self._ensure_db()
        now = datetime.now(UTC).isoformat()
        sources_json = json.dumps(
            [s.model_dump(mode="json") for s in rel.sources],
        )
        props_json = json.dumps(rel.properties)

        existing = await db.execute(
            """SELECT sources FROM relationships
               WHERE source_id = ? AND target_id = ? AND relation_type = ?""",
            (rel.source_id, rel.target_id, rel.relation_type.value),
        )
        row = await existing.fetchone()

        if row:
            existing_sources = json.loads(row["sources"])
            new_sources = json.loads(sources_json)
            merged = _merge_sources(existing_sources, new_sources)
            await db.execute(
                """UPDATE relationships
                   SET properties = ?, sources = ?, updated_at = ?
                   WHERE source_id = ? AND target_id = ? AND relation_type = ?""",
                (
                    props_json,
                    json.dumps(merged),
                    now,
                    rel.source_id,
                    rel.target_id,
                    rel.relation_type.value,
                ),
            )
        else:
            await db.execute(
                """INSERT INTO relationships
                   (source_id, target_id, relation_type,
                    properties, sources,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    rel.source_id,
                    rel.target_id,
                    rel.relation_type.value,
                    props_json,
                    sources_json,
                    now,
                    now,
                ),
            )
        await db.commit()

    async def query(self, cypher_or_filter: str, params: dict | None = None) -> list[dict]:
        """Query the graph.

        Supports the same filters as MemoryStore:
          "all_nodes" — all entities
          "all_edges" — all relationships
          "neighbors:<entity_id>" — neighbors of an entity

        Plus additional queries for persistent investigations:
          "entity:<entity_id>" — single entity by ID
          "type:<entity_type>" — all entities of a type
          "search:<text>" — full-text search across labels
        """
        db = await self._ensure_db()

        if cypher_or_filter == "all_nodes":
            cursor = await db.execute("SELECT * FROM entities")
            rows = await cursor.fetchall()
            return [_row_to_entity_dict(r) for r in rows]

        elif cypher_or_filter == "all_edges":
            cursor = await db.execute("SELECT * FROM relationships")
            rows = await cursor.fetchall()
            return [_row_to_rel_dict(r) for r in rows]

        elif cypher_or_filter.startswith("neighbors:"):
            entity_id = cypher_or_filter.split(":", 1)[1]
            results = []

            # Outgoing edges
            cursor = await db.execute(
                "SELECT * FROM relationships WHERE source_id = ?",
                (entity_id,),
            )
            for row in await cursor.fetchall():
                d = _row_to_rel_dict(row)
                d["id"] = d["target"]
                d["direction"] = "outgoing"
                results.append(d)

            # Incoming edges
            cursor = await db.execute(
                "SELECT * FROM relationships WHERE target_id = ?",
                (entity_id,),
            )
            for row in await cursor.fetchall():
                d = _row_to_rel_dict(row)
                d["id"] = d["source"]
                d["direction"] = "incoming"
                results.append(d)

            return results

        elif cypher_or_filter.startswith("entity:"):
            entity_id = cypher_or_filter.split(":", 1)[1]
            cursor = await db.execute(
                "SELECT * FROM entities WHERE id = ?",
                (entity_id,),
            )
            row = await cursor.fetchone()
            return [_row_to_entity_dict(row)] if row else []

        elif cypher_or_filter.startswith("type:"):
            entity_type = cypher_or_filter.split(":", 1)[1]
            cursor = await db.execute(
                "SELECT * FROM entities WHERE entity_type = ?",
                (entity_type,),
            )
            rows = await cursor.fetchall()
            return [_row_to_entity_dict(r) for r in rows]

        elif cypher_or_filter.startswith("search:"):
            text = cypher_or_filter.split(":", 1)[1]
            cursor = await db.execute(
                "SELECT * FROM entities WHERE label LIKE ?",
                (f"%{text}%",),
            )
            rows = await cursor.fetchall()
            return [_row_to_entity_dict(r) for r in rows]

        # Investigation-scoped queries: "inv:<id>:all_nodes", "inv:<id>:all_edges"
        elif cypher_or_filter.startswith("inv:"):
            parts = cypher_or_filter.split(":", 2)
            if len(parts) < 3:
                return []
            inv_id = int(parts[1])
            sub = parts[2]

            if sub == "all_nodes":
                cursor = await db.execute(
                    """SELECT e.* FROM entities e
                       JOIN investigation_entities ie
                         ON e.id = ie.entity_id
                       WHERE ie.investigation_id = ?""",
                    (inv_id,),
                )
                rows = await cursor.fetchall()
                return [_row_to_entity_dict(r) for r in rows]

            elif sub == "all_edges":
                cursor = await db.execute(
                    """SELECT r.* FROM relationships r
                       JOIN investigation_entities ie1
                         ON r.source_id = ie1.entity_id
                       JOIN investigation_entities ie2
                         ON r.target_id = ie2.entity_id
                       WHERE ie1.investigation_id = ?
                         AND ie2.investigation_id = ?""",
                    (inv_id, inv_id),
                )
                rows = await cursor.fetchall()
                return [_row_to_rel_dict(r) for r in rows]

        return []

    # ------------------------------------------------------------------
    # Deletion and pruning
    # ------------------------------------------------------------------

    async def delete_entities(self, entity_ids: set[str]) -> int:
        """Delete entities by ID, plus their relationships and junction links.

        Returns the number of entities deleted.
        """
        if not entity_ids:
            return 0
        db = await self._ensure_db()
        placeholders = ",".join("?" for _ in entity_ids)
        ids = list(entity_ids)

        # Delete relationships where either endpoint is being removed
        await db.execute(
            f"DELETE FROM relationships WHERE source_id IN ({placeholders})"
            f" OR target_id IN ({placeholders})",
            ids + ids,
        )
        # Delete junction table links
        await db.execute(
            f"DELETE FROM investigation_entities WHERE entity_id IN ({placeholders})",
            ids,
        )
        # Delete lead references
        await db.execute(
            f"UPDATE leads SET entity_id = NULL WHERE entity_id IN ({placeholders})",
            ids,
        )
        # Delete entities
        cursor = await db.execute(
            f"DELETE FROM entities WHERE id IN ({placeholders})",
            ids,
        )
        await db.commit()
        return cursor.rowcount

    async def find_orphan_ids(
        self, investigation_id: int | None = None,
    ) -> set[str]:
        """Find entity IDs with zero relationships.

        If investigation_id is given, only considers entities scoped to
        that investigation.
        """
        db = await self._ensure_db()

        if investigation_id is not None:
            cursor = await db.execute(
                """SELECT ie.entity_id FROM investigation_entities ie
                   WHERE ie.investigation_id = ?
                     AND ie.entity_id NOT IN (
                       SELECT source_id FROM relationships
                       UNION
                       SELECT target_id FROM relationships
                     )""",
                (investigation_id,),
            )
        else:
            cursor = await db.execute(
                """SELECT id FROM entities
                   WHERE id NOT IN (
                     SELECT source_id FROM relationships
                     UNION
                     SELECT target_id FROM relationships
                   )""",
            )
        return {row[0] for row in await cursor.fetchall()}

    async def find_unreachable_ids(
        self,
        seed_id: str,
        investigation_id: int | None = None,
    ) -> set[str]:
        """Find entity IDs not reachable from seed_id via relationships.

        Returns entity IDs that should be pruned (everything NOT in the
        connected component containing seed_id).
        """
        db = await self._ensure_db()

        # Get the universe of entity IDs
        if investigation_id is not None:
            cursor = await db.execute(
                "SELECT entity_id FROM investigation_entities WHERE investigation_id = ?",
                (investigation_id,),
            )
        else:
            cursor = await db.execute("SELECT id FROM entities")
        all_ids = {row[0] for row in await cursor.fetchall()}

        if seed_id not in all_ids:
            return all_ids  # seed doesn't exist — everything is unreachable

        # BFS from seed
        visited = {seed_id}
        frontier = {seed_id}

        while frontier:
            placeholders = ",".join("?" for _ in frontier)
            cursor = await db.execute(
                f"""SELECT DISTINCT target_id FROM relationships
                    WHERE source_id IN ({placeholders})
                    UNION
                    SELECT DISTINCT source_id FROM relationships
                    WHERE target_id IN ({placeholders})""",
                (*frontier, *frontier),
            )
            neighbors = {row[0] for row in await cursor.fetchall()}
            # Only consider neighbors within our universe
            frontier = (neighbors & all_ids) - visited
            visited |= frontier

        return all_ids - visited

    async def find_small_component_ids(
        self,
        min_size: int = 3,
        investigation_id: int | None = None,
    ) -> set[str]:
        """Find entity IDs in connected components smaller than min_size.

        Returns IDs of entities that belong to components with fewer than
        min_size nodes — these are likely noise or false positives.
        """
        db = await self._ensure_db()

        # Get universe
        if investigation_id is not None:
            cursor = await db.execute(
                "SELECT entity_id FROM investigation_entities WHERE investigation_id = ?",
                (investigation_id,),
            )
        else:
            cursor = await db.execute("SELECT id FROM entities")
        all_ids = {row[0] for row in await cursor.fetchall()}

        # Build adjacency from relationships
        adj: dict[str, set[str]] = {eid: set() for eid in all_ids}
        cursor = await db.execute("SELECT source_id, target_id FROM relationships")
        for row in await cursor.fetchall():
            src, tgt = row[0], row[1]
            if src in all_ids and tgt in all_ids:
                adj[src].add(tgt)
                adj[tgt].add(src)

        # Find connected components via BFS
        visited: set[str] = set()
        small_ids: set[str] = set()

        for start in all_ids:
            if start in visited:
                continue
            # BFS to find component
            component: set[str] = set()
            queue = [start]
            while queue:
                node = queue.pop()
                if node in component:
                    continue
                component.add(node)
                queue.extend(adj[node] - component)
            visited |= component
            if len(component) < min_size:
                small_ids |= component

        return small_ids

    async def entity_count(self) -> int:
        db = await self._ensure_db()
        cursor = await db.execute("SELECT COUNT(*) FROM entities")
        row = await cursor.fetchone()
        return row[0]

    async def relationship_count(self) -> int:
        db = await self._ensure_db()
        cursor = await db.execute("SELECT COUNT(*) FROM relationships")
        row = await cursor.fetchone()
        return row[0]

    def summary(self) -> str:
        """Synchronous summary — reads from last known state.

        For async contexts, use summary_async() instead.
        """
        # Can't do async in a sync method, so return a placeholder
        # that the CLI can replace with summary_async()
        return f"SQLite store: {self.db_path}"

    async def summary_async(self) -> str:
        """Async human-readable summary of graph contents."""
        db = await self._ensure_db()

        cursor = await db.execute(
            "SELECT entity_type, COUNT(*) as cnt FROM entities GROUP BY entity_type",
        )
        type_counts = {row["entity_type"]: row["cnt"] for row in await cursor.fetchall()}

        cursor = await db.execute(
            "SELECT relation_type, COUNT(*) as cnt FROM relationships GROUP BY relation_type",
        )
        rel_counts = {row["relation_type"]: row["cnt"] for row in await cursor.fetchall()}

        total_e = sum(type_counts.values())
        total_r = sum(rel_counts.values())

        lines = [f"Graph: {total_e} entities, {total_r} relationships"]
        if type_counts:
            lines.append("Entity types:")
            for t, count in sorted(type_counts.items()):
                lines.append(f"  {t}: {count}")
        if rel_counts:
            lines.append("Relationship types:")
            for t, count in sorted(rel_counts.items()):
                lines.append(f"  {t}: {count}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Investigation tracking
    # ------------------------------------------------------------------

    async def create_investigation(self, name: str, notes: str = "") -> int:
        """Create a new investigation and return its ID."""
        db = await self._ensure_db()
        now = datetime.now(UTC).isoformat()
        cursor = await db.execute(
            "INSERT INTO investigations (name, created_at, updated_at, notes) VALUES (?, ?, ?, ?)",
            (name, now, now, notes),
        )
        await db.commit()
        return cursor.lastrowid

    async def list_investigations(self) -> list[dict]:
        """List all investigations."""
        db = await self._ensure_db()
        cursor = await db.execute(
            "SELECT * FROM investigations ORDER BY updated_at DESC",
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def search_across_investigations(
        self, text: str, entity_type: str | None = None,
    ) -> list[dict]:
        """Search entities by label across all investigations.

        Returns entity dicts augmented with an 'investigations' list showing
        which investigations each entity belongs to (id + name).
        """
        db = await self._ensure_db()

        # Find matching entities
        conditions = ["e.label LIKE ?"]
        params: list = [f"%{text}%"]
        if entity_type:
            conditions.append("e.entity_type = ?")
            params.append(entity_type)

        where = " AND ".join(conditions)
        cursor = await db.execute(
            f"SELECT * FROM entities e WHERE {where}", params,
        )
        rows = await cursor.fetchall()

        results = []
        for row in rows:
            entity = _row_to_entity_dict(row)

            # Look up which investigations this entity belongs to
            inv_cursor = await db.execute(
                """SELECT i.id, i.name FROM investigations i
                   JOIN investigation_entities ie ON i.id = ie.investigation_id
                   WHERE ie.entity_id = ?""",
                (entity["id"],),
            )
            inv_rows = await inv_cursor.fetchall()
            entity["investigations"] = [
                {"id": r["id"], "name": r["name"]} for r in inv_rows
            ]
            results.append(entity)

        return results

    # ------------------------------------------------------------------
    # Lead queue
    # ------------------------------------------------------------------

    async def add_lead(
        self,
        lead_type: str,
        value: str,
        score: float = 0.0,
        investigation_id: int | None = None,
        entity_id: str | None = None,
        notes: str = "",
    ) -> int:
        """Add a lead to the queue. Returns lead ID."""
        db = await self._ensure_db()
        now = datetime.now(UTC).isoformat()
        cursor = await db.execute(
            """INSERT INTO leads
               (investigation_id, entity_id, lead_type,
                value, score, status, notes,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)""",
            (investigation_id, entity_id, lead_type, value, score, notes, now, now),
        )
        await db.commit()
        return cursor.lastrowid

    async def get_leads(
        self,
        status: str | None = None,
        investigation_id: int | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Get leads, optionally filtered by status and investigation."""
        db = await self._ensure_db()
        conditions = []
        params = []

        if status:
            conditions.append("status = ?")
            params.append(status)
        if investigation_id is not None:
            conditions.append("investigation_id = ?")
            params.append(investigation_id)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        cursor = await db.execute(
            f"SELECT * FROM leads {where} ORDER BY score DESC, created_at ASC LIMIT ?",
            (*params, limit),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def update_lead(self, lead_id: int, status: str, notes: str = "") -> None:
        """Update a lead's status."""
        db = await self._ensure_db()
        now = datetime.now(UTC).isoformat()
        await db.execute(
            "UPDATE leads SET status = ?, notes = ?, updated_at = ? WHERE id = ?",
            (status, notes, now, lead_id),
        )
        await db.commit()

    async def backfill_investigation(
        self, investigation_id: int, seed_label: str = "",
    ) -> int:
        """Backfill entity→investigation links using leads and graph reachability.

        Starts from entity_ids referenced by leads for this investigation,
        plus any entities whose label matches seed_label. Walks outward
        along relationships to capture connected entities.
        Returns the number of entities linked.
        """
        db = await self._ensure_db()

        # Seed: entities directly referenced by leads for this investigation
        cursor = await db.execute(
            """SELECT DISTINCT entity_id FROM leads
               WHERE investigation_id = ? AND entity_id IS NOT NULL""",
            (investigation_id,),
        )
        seed_ids = {row["entity_id"] for row in await cursor.fetchall()}

        # Also seed from lead values that match entity IDs or labels
        cursor = await db.execute(
            """SELECT DISTINCT e.id FROM entities e
               JOIN leads l ON (e.id LIKE '%' || l.value || '%'
                                OR e.label LIKE '%' || l.value || '%')
               WHERE l.investigation_id = ?""",
            (investigation_id,),
        )
        for row in await cursor.fetchall():
            seed_ids.add(row["id"])

        # Seed from explicit label match (for investigations with no leads)
        if seed_label:
            cursor = await db.execute(
                "SELECT id FROM entities WHERE label LIKE ?",
                (f"%{seed_label}%",),
            )
            for row in await cursor.fetchall():
                seed_ids.add(row["id"])

        if not seed_ids:
            return 0

        # Walk outward: include entities reachable via relationships
        visited = set(seed_ids)
        frontier = set(seed_ids)

        while frontier:
            placeholders = ",".join("?" for _ in frontier)
            cursor = await db.execute(
                f"""SELECT DISTINCT target_id FROM relationships
                    WHERE source_id IN ({placeholders})
                    UNION
                    SELECT DISTINCT source_id FROM relationships
                    WHERE target_id IN ({placeholders})""",
                (*frontier, *frontier),
            )
            neighbors = {row[0] for row in await cursor.fetchall()}
            frontier = neighbors - visited
            visited |= frontier

        # Insert links
        for entity_id in visited:
            await db.execute(
                """INSERT OR IGNORE INTO investigation_entities
                   (investigation_id, entity_id) VALUES (?, ?)""",
                (investigation_id, entity_id),
            )
        await db.commit()
        return len(visited)

    # ------------------------------------------------------------------
    # Finding notes
    # ------------------------------------------------------------------

    async def get_finding_notes(
        self, investigation_id: int | None = None,
    ) -> list[dict]:
        """Get finding notes, optionally scoped to an investigation."""
        db = await self._ensure_db()
        if investigation_id is not None:
            cursor = await db.execute(
                """SELECT tool, notes, created_at FROM findings
                   WHERE investigation_id = ?
                   ORDER BY created_at""",
                (investigation_id,),
            )
        else:
            cursor = await db.execute(
                "SELECT tool, notes, created_at FROM findings ORDER BY created_at",
            )
        return [dict(row) for row in await cursor.fetchall()]

    async def pending_lead_count(self) -> int:
        """Count pending leads."""
        db = await self._ensure_db()
        cursor = await db.execute(
            "SELECT COUNT(*) FROM leads WHERE status = 'pending'",
        )
        row = await cursor.fetchone()
        return row[0]


def _infer_tool_name(finding: Finding) -> str:
    """Infer the tool name from a Finding's entities or error."""
    if finding.entities:
        first = finding.entities[0]
        if first.sources:
            return first.sources[0].tool
    if finding.error and hasattr(finding.error, "tool"):
        return finding.error.tool or "unknown"
    return "unknown"


def _merge_sources(existing: list[dict], new: list[dict]) -> list[dict]:
    """Merge source lists, deduplicating by (tool, source_url) pair."""
    seen = set()
    merged = []
    for source in existing + new:
        key = (source.get("tool", ""), source.get("source_url", ""))
        if key not in seen:
            seen.add(key)
            merged.append(source)
    return merged


def _row_to_entity_dict(row) -> dict:
    """Convert a SQLite Row to a dict matching MemoryStore's output format."""
    d = {"id": row["id"]}
    d["entity_type"] = row["entity_type"]
    d["label"] = row["label"]
    d["sources"] = json.loads(row["sources"])
    props = json.loads(row["properties"])
    d.update(props)
    return d


def _row_to_rel_dict(row) -> dict:
    """Convert a SQLite Row to a dict matching MemoryStore's output format."""
    d = {
        "source": row["source_id"],
        "target": row["target_id"],
        "relation_type": row["relation_type"],
        "sources": json.loads(row["sources"]),
    }
    props = json.loads(row["properties"])
    d.update(props)
    return d
