"""Cross-harness skill inventory: normalized hashes, frontmatter status, t3 views."""

from __future__ import annotations

import sqlite3
import time

_BUSY_TIMEOUT_MS = 30_000

SQL = """
CREATE TABLE IF NOT EXISTS skill_inventory_views (
    id TEXT PRIMARY KEY,
    viewer TEXT NOT NULL,
    provider TEXT NOT NULL,
    enabled INTEGER,
    installed INTEGER,
    status TEXT,
    skill_count INTEGER NOT NULL DEFAULT 0,
    skill_names_json TEXT NOT NULL DEFAULT '[]',
    source_path TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    note TEXT,
    UNIQUE (viewer, provider)
);

CREATE INDEX IF NOT EXISTS idx_skills_content_hash
    ON skills(current_content_hash);
CREATE INDEX IF NOT EXISTS idx_skill_inventory_views_viewer
    ON skill_inventory_views(viewer);
"""

_NEW_COLUMNS = (
    "normalized_content_hash",
    "frontmatter_status",
    "frontmatter_error",
)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})")}


def _with_busy_retry(fn, *, attempts: int = 10) -> None:
    last: BaseException | None = None
    for i in range(attempts):
        try:
            fn()
            return
        except sqlite3.OperationalError as exc:
            last = exc
            msg = str(exc).lower()
            if ("locked" not in msg and "busy" not in msg) or i >= attempts - 1:
                raise
            time.sleep(0.1 * (2**i))
    assert last is not None
    raise last


def apply(conn: sqlite3.Connection) -> None:
    conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")

    def _schema() -> None:
        tables = {
            str(r[0])
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "skills" in tables:
            existing = _columns(conn, "skills")
            for column in _NEW_COLUMNS:
                if column not in existing:
                    conn.execute(f"ALTER TABLE skills ADD COLUMN {column} TEXT")
        conn.executescript(SQL)
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_skills_normalized_hash
            ON skills(normalized_content_hash)
            """
        )
        conn.commit()

    _with_busy_retry(_schema)
