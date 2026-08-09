from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone

from agentlog.db.migrations.v002_extraction import apply as apply_v002

# Version 1 = base SCHEMA_SQL in schema.py (implicit).
MIGRATIONS: list[tuple[int, Callable[[sqlite3.Connection], None]]] = [
    (2, apply_v002),
]


def _ensure_migrations_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )


def current_version(conn: sqlite3.Connection) -> int:
    _ensure_migrations_table(conn)
    row = conn.execute("SELECT COALESCE(MAX(version), 1) AS v FROM schema_migrations").fetchone()
    if row is None:
        return 1
    # Fresh DB with empty migrations table still has base schema from SCHEMA_SQL.
    count = conn.execute("SELECT COUNT(*) AS c FROM schema_migrations").fetchone()
    if count is not None and int(count["c"]) == 0:
        return 1
    return int(row["v"])


def apply_migrations(conn: sqlite3.Connection) -> list[int]:
    """Apply pending numbered migrations. Returns versions applied."""
    _ensure_migrations_table(conn)
    applied: list[int] = []
    have = {
        int(r["version"])
        for r in conn.execute("SELECT version FROM schema_migrations")
    }
    now = datetime.now(timezone.utc).isoformat()
    for version, fn in MIGRATIONS:
        if version in have:
            continue
        fn(conn)
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (version, now),
        )
        applied.append(version)
    return applied
