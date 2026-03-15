"""Result cache for OSINT tool outputs.

Caches Finding objects by (tool_name, input_hash) with configurable TTL.
Backed by SQLite for persistence across sessions. Prevents redundant API
calls when re-running investigations or following leads that overlap with
previous queries.
"""

import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite

from osint_agent.models import Finding

logger = logging.getLogger(__name__)

DEFAULT_CACHE_PATH = Path(__file__).resolve().parents[2] / "data" / "cache.db"

# Default TTL per tool (hours). Tools with volatile data get shorter TTLs.
_DEFAULT_TTLS: dict[str, int] = {
    "ddg_search": 6,
    "reddit": 12,
    "maigret": 168,       # 7 days — platform presence changes slowly
    "holehe": 168,
    "gravatar": 168,
    "steam": 168,
    "whois": 168,
    "crtsh": 72,
    "dns_enum": 24,
    "builtwith": 168,
    "wayback": 168,
    "wayback_ga": 168,
    "commoncrawl": 168,
    "exiftool": 8760,     # 1 year — EXIF data doesn't change
    "ip_whois": 72,
    "edgar": 24,
    "usaspending": 24,
    "sbir": 72,
    "patents": 72,
    "openfec": 24,
    "courtlistener": 24,
    "peoplesearch": 72,
    "theharvester": 48,
    "phoneinfoga": 72,
    "crosslinked": 72,
    "littlesis": 72,
    "openpolicedata": 168,
    "propublica_nonprofit": 72,
    "documentcloud": 24,
    "fara": 72,
    "muckrock": 24,
    "congress": 24,
    "yt-dlp": 72,
}

_FALLBACK_TTL_HOURS = 24

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tool_cache (
    cache_key TEXT PRIMARY KEY,
    tool_name TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cache_tool ON tool_cache(tool_name);
CREATE INDEX IF NOT EXISTS idx_cache_expires ON tool_cache(expires_at);
"""


def _make_input_hash(kwargs: dict) -> str:
    """Deterministic hash of tool input kwargs."""
    # Sort keys for stability, use json for determinism
    canonical = json.dumps(kwargs, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def _cache_key(tool_name: str, input_hash: str) -> str:
    return f"{tool_name}:{input_hash}"


class ToolCache:
    """SQLite-backed cache for tool results."""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = str(db_path or DEFAULT_CACHE_PATH)
        self._db: aiosqlite.Connection | None = None

    async def _ensure_db(self) -> aiosqlite.Connection:
        if self._db is None:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            self._db = await aiosqlite.connect(self.db_path)
            self._db.row_factory = aiosqlite.Row
            await self._db.executescript(_SCHEMA)
            await self._db.execute("PRAGMA journal_mode=WAL")
            await self._db.commit()
        return self._db

    async def close(self):
        if self._db:
            await self._db.close()
            self._db = None

    async def get(self, tool_name: str, kwargs: dict) -> Finding | None:
        """Look up a cached result. Returns None on miss or expiry."""
        db = await self._ensure_db()
        input_hash = _make_input_hash(kwargs)
        key = _cache_key(tool_name, input_hash)
        now = datetime.now(UTC).isoformat()

        cursor = await db.execute(
            "SELECT result_json FROM tool_cache WHERE cache_key = ? AND expires_at > ?",
            (key, now),
        )
        row = await cursor.fetchone()
        if row is None:
            return None

        try:
            data = json.loads(row["result_json"])
            finding = Finding.model_validate(data)
            logger.debug("Cache HIT: %s(%s)", tool_name, input_hash)
            return finding
        except Exception:
            # Corrupted cache entry — treat as miss
            await db.execute("DELETE FROM tool_cache WHERE cache_key = ?", (key,))
            await db.commit()
            return None

    async def set(
        self,
        tool_name: str,
        kwargs: dict,
        finding: Finding,
        ttl_hours: int | None = None,
    ) -> None:
        """Store a result in the cache."""
        db = await self._ensure_db()
        input_hash = _make_input_hash(kwargs)
        key = _cache_key(tool_name, input_hash)
        now = datetime.now(UTC)

        if ttl_hours is None:
            ttl_hours = _DEFAULT_TTLS.get(tool_name, _FALLBACK_TTL_HOURS)
        expires = now + timedelta(hours=ttl_hours)

        result_json = finding.model_dump_json()

        await db.execute(
            """INSERT OR REPLACE INTO tool_cache
               (cache_key, tool_name, input_hash, result_json, created_at, expires_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (key, tool_name, input_hash, result_json, now.isoformat(), expires.isoformat()),
        )
        await db.commit()
        logger.debug("Cache SET: %s(%s) ttl=%dh", tool_name, input_hash, ttl_hours)

    async def invalidate(self, tool_name: str, kwargs: dict) -> None:
        """Remove a specific cache entry."""
        db = await self._ensure_db()
        input_hash = _make_input_hash(kwargs)
        key = _cache_key(tool_name, input_hash)
        await db.execute("DELETE FROM tool_cache WHERE cache_key = ?", (key,))
        await db.commit()

    async def clear_expired(self) -> int:
        """Remove all expired entries. Returns count deleted."""
        db = await self._ensure_db()
        now = datetime.now(UTC).isoformat()
        cursor = await db.execute(
            "DELETE FROM tool_cache WHERE expires_at <= ?", (now,),
        )
        await db.commit()
        return cursor.rowcount

    async def clear_all(self) -> int:
        """Remove all cache entries. Returns count deleted."""
        db = await self._ensure_db()
        cursor = await db.execute("DELETE FROM tool_cache")
        await db.commit()
        return cursor.rowcount

    async def stats(self) -> dict:
        """Return cache statistics."""
        db = await self._ensure_db()
        now = datetime.now(UTC).isoformat()

        cursor = await db.execute("SELECT COUNT(*) FROM tool_cache")
        total = (await cursor.fetchone())[0]

        cursor = await db.execute(
            "SELECT COUNT(*) FROM tool_cache WHERE expires_at > ?", (now,),
        )
        valid = (await cursor.fetchone())[0]

        cursor = await db.execute(
            "SELECT tool_name, COUNT(*) as cnt FROM tool_cache GROUP BY tool_name ORDER BY cnt DESC",
        )
        by_tool = {row["tool_name"]: row["cnt"] for row in await cursor.fetchall()}

        return {"total": total, "valid": valid, "expired": total - valid, "by_tool": by_tool}
