"""Remember protected parser upgrades without advancing their checkpoint."""

from __future__ import annotations

import sqlite3


SQL = """
CREATE TABLE IF NOT EXISTS parser_upgrade_freezes (
    artifact_id INTEGER PRIMARY KEY REFERENCES artifacts(id) ON DELETE CASCADE,
    previous_parser_version TEXT NOT NULL,
    target_parser_version TEXT NOT NULL,
    source_size INTEGER NOT NULL,
    source_mtime_ns INTEGER NOT NULL,
    source_content_hash TEXT NOT NULL,
    source_parsed_offset INTEGER NOT NULL,
    reason TEXT NOT NULL,
    frozen_at TEXT NOT NULL
);
"""


def apply(conn: sqlite3.Connection) -> None:
    conn.executescript(SQL)
