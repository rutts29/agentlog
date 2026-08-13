"""Store compact Attention signals without retaining source-backed text."""

from __future__ import annotations

import sqlite3


def apply(conn: sqlite3.Connection) -> None:
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(sessions)")}
    additions = {
        "attention_last_substantive_seq": "INTEGER",
        "attention_last_substantive_role": "TEXT",
        "attention_last_substantive_at": "TEXT",
        "attention_final_question": "INTEGER",
        "attention_incomplete_todo": "INTEGER",
        "attention_last_plan_open": "INTEGER",
        "attention_tail_revision": "INTEGER",
    }
    for name, type_name in additions.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE sessions ADD COLUMN {name} {type_name}")
