"""Read-only SQLite access for the MCP server."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from agentlog.config import DEFAULT_DB_PATH


def resolve_db_path(db_path: Path | str | None = None) -> Path:
    if db_path is not None:
        return Path(db_path).expanduser()
    env = os.environ.get("AGENTLOG_DB")
    if env:
        return Path(env).expanduser()
    return DEFAULT_DB_PATH


def connect_readonly(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open the agentlog database in SQLite ``mode=ro`` (no writes possible)."""
    path = resolve_db_path(db_path)
    if not path.exists():
        raise FileNotFoundError(f"agentlog database not found: {path}")
    uri = path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn
