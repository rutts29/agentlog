"""Persist bounded operation categories for deterministic tool evidence."""

from __future__ import annotations

import sqlite3


def apply(conn: sqlite3.Connection) -> None:
    columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(tool_events)")
    }
    if "operation_kind" not in columns:
        conn.execute(
            "ALTER TABLE tool_events ADD COLUMN operation_kind TEXT "
            "NOT NULL DEFAULT 'unknown'"
        )

