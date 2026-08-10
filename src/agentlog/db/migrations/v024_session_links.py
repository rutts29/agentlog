"""Store evidence-backed links between physical session records."""

from __future__ import annotations

import sqlite3


def apply(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS session_links (
            id INTEGER PRIMARY KEY,
            source_session_id TEXT NOT NULL
                REFERENCES sessions(id) ON DELETE CASCADE,
            target_session_id TEXT
                REFERENCES sessions(id) ON DELETE SET NULL,
            link_type TEXT NOT NULL,
            target_harness TEXT NOT NULL,
            target_external_id TEXT NOT NULL,
            link_role TEXT NOT NULL DEFAULT 'unknown',
            confidence TEXT NOT NULL DEFAULT 'observed',
            evidence_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE (
                source_session_id, link_type, target_harness,
                target_external_id
            )
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_session_links_source "
        "ON session_links(source_session_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_session_links_target "
        "ON session_links(target_harness, target_external_id)"
    )
