"""Bind manual owner proposals to locally enumerated configuration targets."""

from __future__ import annotations

import sqlite3


SQL = """
CREATE TABLE IF NOT EXISTS owner_insight_targets (
    id TEXT PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    target_kind TEXT NOT NULL CHECK (target_kind IN ('instruction_file', 'skill_file')),
    scope_type TEXT NOT NULL,
    scope_id TEXT,
    base_content_hash TEXT NOT NULL,
    exported_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_owner_insight_targets_hash
ON owner_insight_targets(base_content_hash);
"""


def apply(conn: sqlite3.Connection) -> None:
    conn.executescript(SQL)
