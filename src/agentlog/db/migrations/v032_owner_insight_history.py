"""Retain enough owner-review history to reject non-append changes."""

from __future__ import annotations

import sqlite3


def apply(conn: sqlite3.Connection) -> None:
    columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(owner_insight_seen_messages)")
    }
    if "seq" not in columns:
        conn.execute(
            "ALTER TABLE owner_insight_seen_messages ADD COLUMN seq INTEGER NOT NULL DEFAULT 0"
        )
    if "role" not in columns:
        conn.execute(
            "ALTER TABLE owner_insight_seen_messages ADD COLUMN role TEXT NOT NULL DEFAULT ''"
        )
