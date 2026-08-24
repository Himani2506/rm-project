"""SQLite persistence.

The database is the single source of truth for student records and their
active/debarred status, so REST reads, WebSocket broadcasts and CSV exports
cannot drift apart.

Scores live in three generic slots rather than columns named after specific
subjects, because the application accepts arbitrary files. The human-readable
label for each slot is stored in `meta` and travels with every response.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from .cleaning import SUBJECT_SLOTS

DB_PATH = Path(os.environ.get("RM_DB_PATH", Path(__file__).resolve().parents[1] / "data" / "app.db"))

SCHEMA = f"""
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS students (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT,
    gender        TEXT,
    grade         INTEGER,
    {', '.join(f'{slot} INTEGER' for slot in SUBJECT_SLOTS)},
    total         INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'active',
    imputed       INTEGER NOT NULL DEFAULT 0,
    quarantined   INTEGER NOT NULL DEFAULT 0,
    quarantine_reason TEXT DEFAULT '',
    source_row    INTEGER,
    UNIQUE (name, grade, {', '.join(SUBJECT_SLOTS)})
);

CREATE INDEX IF NOT EXISTS idx_students_shortlist ON students (status, quarantined, total);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS cleaning_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id   TEXT NOT NULL,
    category TEXT NOT NULL,
    row_ref  TEXT, before TEXT, after TEXT, detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_log_run ON cleaning_log (run_id);

CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, actor_role TEXT NOT NULL, action TEXT NOT NULL,
    student_id INTEGER, student_name TEXT, from_value TEXT, to_value TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY, ts REAL NOT NULL, filename TEXT,
    rows_in INTEGER, rows_out INTEGER, duplicates INTEGER,
    quarantined INTEGER, duration_ms REAL
);
"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


def reset() -> None:
    with connect() as conn:
        for table in ("students", "cleaning_log", "audit", "runs", "meta"):
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.executescript(SCHEMA)


def _set_meta(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, json.dumps(value)))


def get_meta(key: str, default: Any = None) -> Any:
    with connect() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def subject_labels() -> list[str]:
    """Display names for the populated score slots, e.g. ['Math','Science','English']."""
    return get_meta("subject_labels", []) or []


# --------------------------------------------------------------------------
# writes
# --------------------------------------------------------------------------

def replace_students(df: pd.DataFrame, report, filename: str) -> str:
    run_id = uuid.uuid4().hex[:12]

    # Vectorised: build plain columns once, then zip. Row-wise DataFrame
    # iteration is roughly 5x slower at 100k rows.
    def column(series):
        return series.astype(object).where(series.notna(), None)

    frame = {
        "name": column(df["Name"]),
        "gender": df["Gender"],
        "grade": column(df["Grade"]),
    }
    for slot in SUBJECT_SLOTS:
        frame[slot] = column(df[slot])
    frame.update({
        "total": df["Total"].astype(int),
        "status": "active",
        "imputed": df["imputed"].astype(int),
        "quarantined": df["quarantined"].astype(int),
        "quarantine_reason": df["quarantine_reason"],
        "source_row": df["source_row"].astype(int),
    })
    out = pd.DataFrame(frame)
    rows = list(out.itertuples(index=False, name=None))
    columns = ", ".join(out.columns)
    placeholders = ", ".join("?" * len(out.columns))

    with connect() as conn:
        conn.execute("DELETE FROM students")
        conn.execute("DELETE FROM cleaning_log")
        # The pandas stage already de-duplicates; the UNIQUE constraint and
        # OR IGNORE are the second line of defence.
        conn.executemany(
            f"INSERT OR IGNORE INTO students ({columns}) VALUES ({placeholders})", rows
        )
        conn.executemany(
            "INSERT INTO cleaning_log (run_id, category, row_ref, before, after, detail)"
            " VALUES (?,?,?,?,?,?)",
            [(run_id, e["category"], e["row_ref"], e["before"], e["after"], e["detail"])
             for e in report.entries],
        )
        conn.execute(
            "INSERT INTO runs (run_id, ts, filename, rows_in, rows_out, duplicates,"
            " quarantined, duration_ms) VALUES (?,?,?,?,?,?,?,?)",
            (run_id, time.time(), filename, report.rows_in, report.rows_out,
             report.duplicates_removed, report.quarantined, report.duration_ms),
        )
        _set_meta(conn, "subject_labels", report.subject_labels)
        _set_meta(conn, "mapping", report.mapping)
    return run_id


def set_status(student_id: int, status: str, actor: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM students WHERE id = ?", (student_id,)).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE students SET status = ? WHERE id = ?", (status, student_id))
        conn.execute(
            "INSERT INTO audit (ts, actor_role, action, student_id, student_name, from_value, to_value)"
            " VALUES (?,?,?,?,?,?,?)",
            (time.time(), actor, "status_change", student_id, row["name"], row["status"], status),
        )
        updated = conn.execute("SELECT * FROM students WHERE id = ?", (student_id,)).fetchone()
    return dict(updated)


def set_status_bulk(ids: Iterable[int], status: str, actor: str) -> list[dict[str, Any]]:
    return [s for s in (set_status(i, status, actor) for i in ids) if s]


# --------------------------------------------------------------------------
# reads
# --------------------------------------------------------------------------

def list_students(min_total: int = 0, shortlist_only: bool = False,
                  search: str = "") -> tuple[list[dict[str, Any]], float]:
    clauses, params = [], []
    if shortlist_only:
        clauses.append("status = 'active' AND quarantined = 0 AND total >= ?")
        params.append(min_total)
    if search:
        clauses.append("LOWER(name) LIKE ?")
        params.append(f"%{search.lower()}%")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    started = time.perf_counter()
    with connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM students {where} ORDER BY total DESC, name ASC", params
        ).fetchall()
    return [dict(r) for r in rows], (time.perf_counter() - started) * 1000


def stats(min_total: int = 0) -> dict[str, Any]:
    labels = subject_labels()
    slots = SUBJECT_SLOTS[:len(labels)]
    averages = ", ".join(f"AVG({slot}) avg_{slot}" for slot in slots) or "NULL"

    started = time.perf_counter()
    with connect() as conn:
        totals = conn.execute(
            "SELECT COUNT(*) c,"
            " SUM(status='debarred') debarred,"
            " SUM(quarantined=1) quarantined FROM students"
        ).fetchone()
        row = conn.execute(
            f"SELECT COUNT(*) matched, AVG(total) avg_total, MAX(total) top, {averages}"
            " FROM students WHERE status='active' AND quarantined=0 AND total >= ?",
            (min_total,),
        ).fetchone()
        histogram = conn.execute(
            "SELECT (total / 20) * 20 AS bucket, COUNT(*) c FROM students"
            " WHERE status='active' AND quarantined=0 GROUP BY bucket ORDER BY bucket"
        ).fetchall()
        ceiling = conn.execute("SELECT MAX(total) m FROM students").fetchone()["m"]
    elapsed = (time.perf_counter() - started) * 1000

    def rounded(value):
        return round(value, 1) if value is not None else None

    total_rows = totals["c"] or 0
    debarred = totals["debarred"] or 0
    quarantined = totals["quarantined"] or 0

    return {
        "total_students": total_rows,
        "eligible_pool": total_rows - debarred - quarantined,
        "debarred": debarred,
        "quarantined": quarantined,
        "matched": row["matched"],
        "avg_total": rounded(row["avg_total"]),
        "top_total": row["top"],
        "max_total": ceiling or 0,
        "subject_labels": labels,
        "subject_averages": [rounded(row[f"avg_{slot}"]) for slot in slots],
        "histogram": [{"bucket": r["bucket"], "count": r["c"]} for r in histogram],
        "query_ms": round(elapsed, 2),
    }


def cleaning_log(limit: int = 500) -> list[dict[str, Any]]:
    with connect() as conn:
        latest = conn.execute("SELECT run_id FROM runs ORDER BY ts DESC LIMIT 1").fetchone()
        if latest is None:
            return []
        rows = conn.execute(
            "SELECT category, row_ref, before, after, detail FROM cleaning_log"
            " WHERE run_id = ? LIMIT ?", (latest["run_id"], limit),
        ).fetchall()
    return [dict(r) for r in rows]


def latest_run() -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM runs ORDER BY ts DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def audit_trail(limit: int = 50) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM audit ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]
