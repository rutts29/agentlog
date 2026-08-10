from __future__ import annotations

import sqlite3

SQL = """
CREATE INDEX IF NOT EXISTS idx_token_usage_granularity_session_seq
    ON token_usage(granularity, session_id, seq);
CREATE INDEX IF NOT EXISTS idx_messages_session_seq
    ON messages(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_messages_session_plumbing_seq
    ON messages(session_id, is_tool_plumbing, seq);
CREATE INDEX IF NOT EXISTS idx_tool_events_session_seq
    ON tool_events(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_sessions_parent
    ON sessions(parent_session_id);
"""


def apply(conn: sqlite3.Connection) -> None:
    conn.executescript(SQL)
