from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

# Expensive human/LLM labels: never CASCADE-delete with windows.
DURABLE_LABEL_TABLES = (
    "ux_observations",
    "adjudications",
    "auto_review_observations",
    "worker_task_observations",
    "skill_compliance_observations",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def mark_orphans(conn: sqlite3.Connection) -> dict[str, int]:
    """Mark durable labels whose window_id no longer resolves as orphaned."""
    now = _utc_now()
    counts: dict[str, int] = {}
    for table in DURABLE_LABEL_TABLES:
        if not _table_exists(conn, table):
            continue
        if not _column_exists(conn, table, "link_status"):
            continue
        cur = conn.execute(
            f"""
            UPDATE {table}
            SET link_status = 'orphaned', orphaned_at = COALESCE(orphaned_at, ?)
            WHERE link_status = 'linked'
              AND (
                    window_id IS NULL
                    OR window_id NOT IN (SELECT id FROM exchange_windows)
                  )
            """,
            (now,),
        )
        counts[table] = int(cur.rowcount)
    return counts


def relink_by_content_hash(conn: sqlite3.Connection) -> dict[str, int]:
    """Re-attach orphaned labels when a window with matching content_hash exists."""
    counts: dict[str, int] = {}
    for table in DURABLE_LABEL_TABLES:
        if not _table_exists(conn, table):
            continue
        if not _column_exists(conn, table, "content_hash"):
            continue
        cur = conn.execute(
            f"""
            UPDATE {table}
            SET window_id = (
                    SELECT w.id FROM exchange_windows w
                    WHERE w.content_hash = {table}.content_hash
                    LIMIT 1
                ),
                link_status = 'linked',
                orphaned_at = NULL
            WHERE content_hash != ''
              AND content_hash IN (SELECT content_hash FROM exchange_windows)
              AND (
                    link_status = 'orphaned'
                    OR window_id IS NULL
                    OR window_id NOT IN (SELECT id FROM exchange_windows)
                  )
            """
        )
        counts[table] = int(cur.rowcount)
    return counts


def refresh_label_links(conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
    """Orphan then re-link durable labels after a window rewrite."""
    orphans = mark_orphans(conn)
    relinked = relink_by_content_hash(conn)
    # Labels whose window_id already matches a live window stay/become linked.
    for table in DURABLE_LABEL_TABLES:
        if not _table_exists(conn, table) or not _column_exists(conn, table, "link_status"):
            continue
        conn.execute(
            f"""
            UPDATE {table}
            SET link_status = 'linked', orphaned_at = NULL
            WHERE window_id IN (SELECT id FROM exchange_windows)
            """
        )
    return {"orphaned": orphans, "relinked": relinked}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(str(r[1]) == column for r in rows)
