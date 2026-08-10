from __future__ import annotations

import sqlite3

SQL = """
CREATE TABLE IF NOT EXISTS token_usage (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    message_id TEXT REFERENCES messages(id) ON DELETE SET NULL,
    seq INTEGER NOT NULL,
    granularity TEXT NOT NULL CHECK (
        granularity IN ('message', 'turn', 'session_cumulative')
    ),
    usage_source TEXT NOT NULL,
    model TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_creation_input_tokens INTEGER,
    cache_read_input_tokens INTEGER,
    cached_input_tokens INTEGER,
    cache_write_input_tokens INTEGER,
    reasoning_output_tokens INTEGER,
    total_tokens INTEGER,
    timestamp TEXT,
    extras_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE (session_id, usage_source, seq)
);

CREATE INDEX IF NOT EXISTS idx_token_usage_session
ON token_usage(session_id);
CREATE INDEX IF NOT EXISTS idx_token_usage_granularity
ON token_usage(granularity);
CREATE INDEX IF NOT EXISTS idx_token_usage_source
ON token_usage(usage_source);
"""


def apply(conn: sqlite3.Connection) -> None:
    conn.executescript(SQL)
