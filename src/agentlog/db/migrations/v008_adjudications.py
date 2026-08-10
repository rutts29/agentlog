from __future__ import annotations

import sqlite3

SQL = """
CREATE TABLE IF NOT EXISTS adjudications (
    window_id TEXT PRIMARY KEY REFERENCES exchange_windows(id) ON DELETE CASCADE,
    adjudicated_at TEXT NOT NULL,
    turn_kind TEXT NOT NULL,
    user_stance TEXT,
    agent_stance TEXT,
    prior_outcome TEXT,
    notes TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL CHECK (source IN ('audit_pack', 'ad_hoc'))
);

CREATE INDEX IF NOT EXISTS idx_adjudications_at ON adjudications(adjudicated_at);
CREATE INDEX IF NOT EXISTS idx_adjudications_source ON adjudications(source);
"""


def apply(conn: sqlite3.Connection) -> None:
    conn.executescript(SQL)
