"""Dated snapshots of the owner's agent-config instruction files."""

from __future__ import annotations

import sqlite3

SQL = """
CREATE TABLE IF NOT EXISTS config_snapshots (
    id TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    path_kind TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    content_bytes INTEGER NOT NULL DEFAULT 0,
    observed_at TEXT NOT NULL,
    source TEXT NOT NULL
        CHECK (source IN ('live_scan', 'git_history')),
    git_commit TEXT,
    git_committed_at TEXT,
    meta_json TEXT NOT NULL DEFAULT '{}'
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_config_snapshots_identity
ON config_snapshots(
    path,
    content_hash,
    source,
    ifnull(git_commit, '')
);

CREATE INDEX IF NOT EXISTS idx_config_snapshots_path_time
ON config_snapshots(path, observed_at);

CREATE INDEX IF NOT EXISTS idx_config_snapshots_hash
ON config_snapshots(content_hash);
"""


def apply(conn: sqlite3.Connection) -> None:
    conn.executescript(SQL)
