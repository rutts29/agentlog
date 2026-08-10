from __future__ import annotations

import sqlite3

SQL = """
CREATE TABLE IF NOT EXISTS ingest_events (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    harness TEXT NOT NULL,
    sessions_added INTEGER NOT NULL DEFAULT 0,
    sessions_updated INTEGER NOT NULL DEFAULT 0,
    messages_added INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_ingest_events_ts ON ingest_events(ts);
CREATE INDEX IF NOT EXISTS idx_ingest_events_harness ON ingest_events(harness);
"""


def apply(conn: sqlite3.Connection) -> None:
    conn.executescript(SQL)
