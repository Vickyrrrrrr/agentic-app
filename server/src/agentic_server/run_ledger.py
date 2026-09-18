"""Run-event ledger: sqlite/WAL, short-lived connections. kill -9 safe."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

DEFAULT_CAP = 10_000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS run_events (
    rowid INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    ts REAL NOT NULL,
    type TEXT,
    label TEXT,
    stage TEXT,
    status TEXT,
    design_name TEXT
);
CREATE INDEX IF NOT EXISTS idx_run_events_run ON run_events(run_id, rowid);
"""

_COLUMNS = ("run_id", "ts", "type", "label", "stage", "status", "design_name")


def db_path_for(state_dir: str | Path) -> Path:
    return Path(state_dir) / "run_ledger.db"


def _connect(db_path: str | Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.executescript(_SCHEMA)
    return conn


def append_event(db_path: str | Path, event: dict[str, Any], cap: int = DEFAULT_CAP) -> None:
    """Append one envelope; prune oldest rows beyond cap."""
    row = {
        "run_id": event.get("run_id"),
        "ts": event.get("timestamp", time.time()),
        "type": event.get("type"),
        "label": event.get("label"),
        "stage": event.get("stage"),
        "status": event.get("status"),
        "design_name": event.get("design_name"),
    }
    conn = _connect(db_path)
    try:
        conn.execute(
            "INSERT INTO run_events (run_id, ts, type, label, stage, status, design_name)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            tuple(row[col] for col in _COLUMNS),
        )
        conn.execute(
            "DELETE FROM run_events WHERE rowid NOT IN"
            " (SELECT rowid FROM run_events ORDER BY rowid DESC LIMIT ?)",
            (max(int(cap), 1),),
        )
        conn.commit()
    finally:
        conn.close()


def read_events(
    db_path: str | Path, limit: int = 200, run_id: str | None = None
) -> list[dict[str, Any]]:
    """Last `limit` events, chronological, optionally filtered by run."""
    conn = _connect(db_path)
    try:
        if run_id:
            rows = conn.execute(
                "SELECT run_id, ts, type, label, stage, status, design_name"
                " FROM run_events WHERE run_id = ? ORDER BY rowid DESC LIMIT ?",
                (run_id, min(max(int(limit), 1), 500)),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT run_id, ts, type, label, stage, status, design_name"
                " FROM run_events ORDER BY rowid DESC LIMIT ?",
                (min(max(int(limit), 1), 500),),
            ).fetchall()
    finally:
        conn.close()
    events: list[dict[str, Any]] = []
    for row in reversed(rows):
        record = {
            "run_id": row[0],
            "timestamp": row[1],
            "type": row[2],
            "label": row[3],
            "stage": row[4],
            "status": row[5],
            "design_name": row[6],
        }
        events.append({k: v for k, v in record.items() if v is not None})
    return events
