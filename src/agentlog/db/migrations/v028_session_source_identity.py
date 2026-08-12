"""Persist source-native session identity metadata."""

from __future__ import annotations

import sqlite3


def apply(conn: sqlite3.Connection) -> None:
    columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(sessions)")
    }
    for column in (
        "originator",
        "thread_source",
        "fork_context_status",
        "fork_context_boundary",
    ):
        if column not in columns:
            conn.execute(f"ALTER TABLE sessions ADD COLUMN {column} TEXT")
    for column in ("inherited_message_count", "inherited_record_count"):
        if column not in columns:
            conn.execute(
                f"ALTER TABLE sessions ADD COLUMN {column} "
                "INTEGER NOT NULL DEFAULT 0"
            )
