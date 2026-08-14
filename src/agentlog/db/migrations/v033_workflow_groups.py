"""Persist parser-declared workflow cohorts without adding synthetic sessions."""

from __future__ import annotations

import sqlite3


def apply(conn: sqlite3.Connection) -> None:
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(sessions)")}
    additions = {
        "workflow_group_id": "TEXT",
        "workflow_group_label": "TEXT",
        "workflow_group_position": "INTEGER",
    }
    for name, type_name in additions.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE sessions ADD COLUMN {name} {type_name}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sessions_workflow_group "
        "ON sessions(parent_session_id, workflow_group_position, workflow_group_id)"
    )
