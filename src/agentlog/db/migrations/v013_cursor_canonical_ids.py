from __future__ import annotations

import sqlite3

from agentlog.ingest.cursor_merge import merge_cursor_duplicates


def apply(conn: sqlite3.Connection) -> None:
    """Collapse Cursor path-prefixed duplicate sessions onto composer UUIDs."""
    merge_cursor_duplicates(conn)
