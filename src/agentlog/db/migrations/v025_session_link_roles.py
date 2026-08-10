"""Add explicit root/worker roles to provider links."""

from __future__ import annotations

import sqlite3


def apply(conn: sqlite3.Connection) -> None:
    columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(session_links)")
    }
    if "link_role" not in columns:
        conn.execute(
            "ALTER TABLE session_links ADD COLUMN link_role TEXT "
            "NOT NULL DEFAULT 'unknown'"
        )
