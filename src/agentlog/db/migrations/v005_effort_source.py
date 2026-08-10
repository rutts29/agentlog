from __future__ import annotations

import sqlite3


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})")}


def apply(conn: sqlite3.Connection) -> None:
    if "effort_source" not in _columns(conn, "sessions"):
        conn.execute("ALTER TABLE sessions ADD COLUMN effort_source TEXT")
    if "effort_source" not in _columns(conn, "messages"):
        conn.execute("ALTER TABLE messages ADD COLUMN effort_source TEXT")
