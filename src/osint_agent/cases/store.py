"""SQLite storage for case files.

One database file holds every case. Evidence and claims are only ever added. A
source's status and a candidate's judgement keep their latest value, and every
change is also written to the event log, so a case can be audited afterwards.
"""

import json
import sqlite3
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path

from osint_agent.cases.models import (
    Capture,
    CaseRecord,
    EvidenceRecord,
    Intake,
    Judgement,
    Post,
    SourceRun,
    utc_now,
)

# Default database location: data/cases.db in the project root
DEFAULT_CASES_DB = Path(__file__).resolve().parents[3] / "data" / "cases.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    case_id TEXT PRIMARY KEY,
    body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence (
    case_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    evidence_id TEXT NOT NULL,
    body TEXT NOT NULL,
    PRIMARY KEY (case_id, evidence_id)
);
CREATE TABLE IF NOT EXISTS source_runs (
    case_id TEXT NOT NULL,
    source TEXT NOT NULL,
    body TEXT NOT NULL,
    PRIMARY KEY (case_id, source)
);
CREATE TABLE IF NOT EXISTS judgements (
    case_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    body TEXT NOT NULL,
    PRIMARY KEY (case_id, candidate_id)
);
CREATE TABLE IF NOT EXISTS claims (
    case_id TEXT NOT NULL,
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    evidence_id TEXT NOT NULL,
    text TEXT NOT NULL,
    quote TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dismissals (
    case_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    PRIMARY KEY (case_id, evidence_id)
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT NOT NULL,
    at TEXT NOT NULL,
    operation TEXT NOT NULL,
    detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS posts (
    case_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    post_id TEXT NOT NULL,
    source TEXT NOT NULL,
    native_id TEXT NOT NULL,
    body TEXT NOT NULL,
    PRIMARY KEY (case_id, post_id),
    UNIQUE (case_id, source, native_id)
);
CREATE TABLE IF NOT EXISTS post_reads (
    case_id TEXT NOT NULL,
    post_id TEXT NOT NULL,
    reading TEXT NOT NULL,
    outcome TEXT NOT NULL,
    quote TEXT NOT NULL,
    reader TEXT NOT NULL,
    at TEXT NOT NULL,
    PRIMARY KEY (case_id, post_id, reading)
);
CREATE TABLE IF NOT EXISTS captures (
    case_id TEXT NOT NULL,
    account TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    body TEXT NOT NULL,
    PRIMARY KEY (case_id, account, sha256)
);
CREATE TABLE IF NOT EXISTS intakes (
    case_id TEXT NOT NULL,
    source TEXT NOT NULL,
    account TEXT NOT NULL,
    folder TEXT NOT NULL,
    body TEXT NOT NULL,
    PRIMARY KEY (case_id, source, account, folder)
);
"""


class CaseStore:
    """Reads and writes case files in one SQLite database."""

    def __init__(self, db_path: str | Path | None = None):
        self.path = Path(db_path) if db_path else DEFAULT_CASES_DB
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # One connection for the life of the store, shared under a lock. With
        # write-ahead logging a crash of this program loses nothing, and a commit
        # does not wait for the disk, which was most of the cost of a write.
        self._lock = threading.Lock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA synchronous = NORMAL")
        self._connection.executescript(_SCHEMA)

    def close(self) -> None:
        """Close the database. The store cannot be used afterwards."""
        with self._lock:
            self._connection.close()

    def _write(self, sql: str, parameters: tuple) -> None:
        with self._lock, self._connection:
            self._connection.execute(sql, parameters)

    def _read(self, sql: str, parameters: tuple) -> list[sqlite3.Row]:
        with self._lock:
            return self._connection.execute(sql, parameters).fetchall()

    # Cases

    def add_case(self, case: CaseRecord) -> None:
        self._write(
            "INSERT INTO cases VALUES (?, ?)", (case.case_id, case.model_dump_json())
        )

    def get_case(self, case_id: str) -> CaseRecord | None:
        rows = self._read("SELECT body FROM cases WHERE case_id = ?", (case_id,))
        return CaseRecord.model_validate_json(rows[0]["body"]) if rows else None

    # Evidence

    def next_evidence_id(self, case_id: str) -> str:
        rows = self._read(
            "SELECT COUNT(*) AS n FROM evidence WHERE case_id = ?", (case_id,)
        )
        return f"E{rows[0]['n'] + 1}"

    def add_evidence(self, case_id: str, record: EvidenceRecord) -> None:
        seq = int(record.evidence_id[1:])
        self._write(
            "INSERT INTO evidence VALUES (?, ?, ?, ?)",
            (case_id, seq, record.evidence_id, record.model_dump_json()),
        )

    def evidence(self, case_id: str) -> list[EvidenceRecord]:
        rows = self._read(
            "SELECT body FROM evidence WHERE case_id = ? ORDER BY seq", (case_id,)
        )
        return [EvidenceRecord.model_validate_json(row["body"]) for row in rows]

    # Source runs

    def set_source_run(self, case_id: str, run: SourceRun) -> None:
        self._write(
            "INSERT OR REPLACE INTO source_runs VALUES (?, ?, ?)",
            (case_id, run.source, run.model_dump_json()),
        )

    def source_runs(self, case_id: str) -> dict[str, SourceRun]:
        rows = self._read("SELECT body FROM source_runs WHERE case_id = ?", (case_id,))
        runs = [SourceRun.model_validate_json(row["body"]) for row in rows]
        return {run.source: run for run in runs}

    # Judgements

    def set_judgement(self, case_id: str, judgement: Judgement) -> None:
        self._write(
            "INSERT OR REPLACE INTO judgements VALUES (?, ?, ?)",
            (case_id, judgement.candidate_id, judgement.model_dump_json()),
        )

    def judgements(self, case_id: str) -> dict[str, Judgement]:
        rows = self._read("SELECT body FROM judgements WHERE case_id = ?", (case_id,))
        found = [Judgement.model_validate_json(row["body"]) for row in rows]
        return {judgement.candidate_id: judgement for judgement in found}

    # Claims whose quote was found in the evidence they cite

    def add_claim(self, case_id: str, evidence_id: str, text: str, quote: str) -> None:
        self._write(
            "INSERT INTO claims (case_id, evidence_id, text, quote) VALUES (?, ?, ?, ?)",
            (case_id, evidence_id, text, quote),
        )

    def claims(self, case_id: str) -> list[dict[str, str]]:
        rows = self._read(
            "SELECT evidence_id, text, quote FROM claims WHERE case_id = ? ORDER BY seq",
            (case_id,),
        )
        latest: dict[tuple[str, str], dict[str, str]] = {}
        for row in rows:
            latest[(row["evidence_id"], row["text"])] = dict(row)
        return list(latest.values())

    # Evidence reviewed and set aside

    def set_dismissal(self, case_id: str, evidence_id: str, reason: str) -> None:
        self._write(
            "INSERT OR REPLACE INTO dismissals VALUES (?, ?, ?)",
            (case_id, evidence_id, reason),
        )

    def dismissals(self, case_id: str) -> dict[str, str]:
        rows = self._read(
            "SELECT evidence_id, reason FROM dismissals WHERE case_id = ?", (case_id,)
        )
        return {row["evidence_id"]: row["reason"] for row in rows}

    # Posts collected from listed accounts, and which readings covered each

    def add_posts(self, case_id: str, posts: Sequence[Post]) -> int:
        """Store posts, skipping any already held. Returns how many were new."""
        added = 0
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT COALESCE(MAX(seq), 0) AS n FROM posts WHERE case_id = ?",
                (case_id,),
            ).fetchone()
            seq = row["n"]
            for post in posts:
                held = self._connection.execute(
                    "SELECT 1 FROM posts WHERE case_id = ? AND source = ? AND native_id = ?",
                    (case_id, post.source, post.native_id),
                ).fetchone()
                if held:
                    continue
                seq += 1
                stored = post.model_copy(update={"post_id": f"P{seq}"})
                self._connection.execute(
                    "INSERT INTO posts VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        case_id,
                        seq,
                        stored.post_id,
                        stored.source,
                        stored.native_id,
                        stored.model_dump_json(),
                    ),
                )
                added += 1
        return added

    def posts(self, case_id: str) -> list[Post]:
        rows = self._read(
            "SELECT body FROM posts WHERE case_id = ? ORDER BY seq", (case_id,)
        )
        return [Post.model_validate_json(row["body"]) for row in rows]

    def add_post_evidence(self, case_id: str, post_id: str, record: EvidenceRecord) -> str:
        """Turn a post into evidence in one step, and return the evidence id.

        The id is assigned here, whatever the record carries. A post that is
        already evidence keeps the record it has, so trying again after a
        failure cannot make a second.
        """
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT body FROM posts WHERE case_id = ? AND post_id = ?",
                (case_id, post_id),
            ).fetchone()
            if row is None:
                raise KeyError(f"no post '{post_id}' in case '{case_id}'")
            post = Post.model_validate_json(row["body"])
            if post.evidence_id:
                return post.evidence_id
            last = self._connection.execute(
                "SELECT COALESCE(MAX(seq), 0) AS n FROM evidence WHERE case_id = ?",
                (case_id,),
            ).fetchone()["n"]
            stored = record.model_copy(update={"evidence_id": f"E{last + 1}"})
            self._connection.execute(
                "INSERT INTO evidence VALUES (?, ?, ?, ?)",
                (case_id, last + 1, stored.evidence_id, stored.model_dump_json()),
            )
            post.evidence_id = stored.evidence_id
            self._connection.execute(
                "UPDATE posts SET body = ? WHERE case_id = ? AND post_id = ?",
                (post.model_dump_json(), case_id, post_id),
            )
            return stored.evidence_id

    # Screenshots a reviewer saved

    def add_capture(
        self, case_id: str, capture: Capture, record: EvidenceRecord | None = None
    ) -> Capture:
        """Store what was made of one screenshot, once.

        When the picture needs a person, its evidence record is written in the
        same step, so a crash cannot leave a doubtful picture with nothing
        holding the brief back. A screenshot already held is returned as held.
        """
        with self._lock, self._connection:
            held = self._connection.execute(
                "SELECT body FROM captures "
                "WHERE case_id = ? AND account = ? AND sha256 = ?",
                (case_id, capture.account, capture.sha256),
            ).fetchone()
            if held:
                return Capture.model_validate_json(held["body"])
            if record is not None:
                row = self._connection.execute(
                    "SELECT COALESCE(MAX(seq), 0) AS n FROM evidence WHERE case_id = ?",
                    (case_id,),
                ).fetchone()
                seq = row["n"] + 1
                stored = record.model_copy(update={"evidence_id": f"E{seq}"})
                self._connection.execute(
                    "INSERT INTO evidence VALUES (?, ?, ?, ?)",
                    (case_id, seq, stored.evidence_id, stored.model_dump_json()),
                )
                capture = capture.model_copy(update={"evidence_id": stored.evidence_id})
            self._connection.execute(
                "INSERT INTO captures VALUES (?, ?, ?, ?)",
                (case_id, capture.account, capture.sha256, capture.model_dump_json()),
            )
        return capture

    def captures(self, case_id: str) -> list[Capture]:
        """Screenshots held for a case, in the order they were taken in."""
        rows = self._read(
            "SELECT body FROM captures WHERE case_id = ? ORDER BY rowid", (case_id,)
        )
        return [Capture.model_validate_json(row["body"]) for row in rows]

    def set_intake(self, case_id: str, intake: Intake) -> None:
        self._write(
            "INSERT OR REPLACE INTO intakes VALUES (?, ?, ?, ?, ?)",
            (
                case_id,
                intake.source,
                intake.account,
                intake.folder,
                intake.model_dump_json(),
            ),
        )

    def intakes(self, case_id: str) -> list[Intake]:
        rows = self._read(
            "SELECT body FROM intakes WHERE case_id = ? "
            "ORDER BY source, account, folder",
            (case_id,),
        )
        return [Intake.model_validate_json(row["body"]) for row in rows]

    def add_post_reads(
        self,
        case_id: str,
        reading: str,
        reader: str,
        outcomes: Mapping[str, tuple[str, str]],
    ) -> None:
        """Record one reading's outcome and quote for each post id given.

        A reading that flagged a post is never replaced by one that cleared it,
        so two readings running at once cannot lose a flag or its quote.
        """
        at = utc_now()
        rows = [
            (case_id, post_id, reading, outcome, quote, reader, at)
            for post_id, (outcome, quote) in outcomes.items()
        ]
        with self._lock, self._connection:
            self._connection.executemany(
                "INSERT INTO post_reads VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (case_id, post_id, reading) DO UPDATE SET "
                "outcome = excluded.outcome, quote = excluded.quote, "
                "reader = excluded.reader, at = excluded.at "
                "WHERE post_reads.outcome != 'flagged' OR excluded.outcome = 'flagged'",
                rows,
            )

    def post_reads(self, case_id: str) -> dict[str, dict[str, dict[str, str]]]:
        """Readings by post id, then by reading name."""
        rows = self._read(
            "SELECT post_id, reading, outcome, quote, reader, at FROM post_reads "
            "WHERE case_id = ?",
            (case_id,),
        )
        found: dict[str, dict[str, dict[str, str]]] = {}
        for row in rows:
            found.setdefault(row["post_id"], {})[row["reading"]] = {
                "outcome": row["outcome"],
                "quote": row["quote"],
                "reader": row["reader"],
                "at": row["at"],
            }
        return found

    # Event log

    def log(self, case_id: str, operation: str, detail: dict) -> None:
        self._write(
            "INSERT INTO events (case_id, at, operation, detail) VALUES (?, ?, ?, ?)",
            (case_id, utc_now(), operation, json.dumps(detail, default=str)),
        )

    def events(self, case_id: str) -> list[dict]:
        rows = self._read(
            "SELECT at, operation, detail FROM events WHERE case_id = ? ORDER BY seq",
            (case_id,),
        )
        return [
            {
                "at": row["at"],
                "operation": row["operation"],
                "detail": json.loads(row["detail"]),
            }
            for row in rows
        ]
