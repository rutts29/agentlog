from __future__ import annotations

import sqlite3
from collections.abc import Generator
from pathlib import Path

from fastapi import Request


def get_db_path(request: Request) -> Path:
    return Path(request.app.state.db_path)


def get_conn(request: Request) -> Generator[sqlite3.Connection, None, None]:
    """Open a read-only SQLite connection for the request."""
    db_path = get_db_path(request)
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()
