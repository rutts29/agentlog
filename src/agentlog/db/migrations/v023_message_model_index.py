from __future__ import annotations

import sqlite3


def apply(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_messages_direct_assistant_model_session
        ON messages(session_id)
        WHERE role = 'assistant'
          AND model_canonical IS NOT NULL
          AND model_canonical <> ''
        """
    )
