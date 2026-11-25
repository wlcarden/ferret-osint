"""SQLite storage for case files.

One database file holds every case. Evidence and claims are only ever added. A
source's status and a candidate's judgement keep their latest value, and every
change is also written to the event log, so a case can be audited afterwards.
"""

import json
import sqlite3
import threading
from pathlib import Path

from osint_agent.cases.models import (
    CaseRecord,
    EvidenceRecord,
    Judgement,
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
