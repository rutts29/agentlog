"""Distinguish system-pruned proposals from owner rejections.

Also repairs self-referential claim.supersedes_id rows and installs a
trigger so a claim can never supersede itself.
"""

from __future__ import annotations

import sqlite3

PROPOSALS_V020 = """
CREATE TABLE proposals_v020 (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    action TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN (
            'pending', 'accepted', 'rejected', 'deferred', 'superseded'
        )),
    target_path TEXT NOT NULL,
    target_kind TEXT NOT NULL,
    scope_type TEXT NOT NULL,
    scope_id TEXT,
    base_content_hash TEXT,
    unified_diff TEXT NOT NULL,
    proposed_content TEXT,
    rationale TEXT NOT NULL,
    derivation_summary TEXT NOT NULL DEFAULT '',
    does_not_prove TEXT NOT NULL DEFAULT '',
    sample_size INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    decided_at TEXT,
    decision_note TEXT
)
"""

COPY_PROPOSALS = """
INSERT INTO proposals_v020 (
    id, title, action, status, target_path, target_kind, scope_type, scope_id,
    base_content_hash, unified_diff, proposed_content, rationale,
    derivation_summary, does_not_prove, sample_size, created_at, updated_at,
    decided_at, decision_note
)
SELECT
    id, title, action,
    CASE
        WHEN status = 'rejected'
             AND decision_note LIKE 'auto-rejected:%'
        THEN 'superseded'
        ELSE status
    END,
    target_path, target_kind, scope_type, scope_id,
    base_content_hash, unified_diff, proposed_content, rationale,
    derivation_summary, does_not_prove, sample_size, created_at, updated_at,
    decided_at,
    CASE
        WHEN status = 'rejected'
             AND decision_note LIKE 'auto-rejected:%'
        THEN replace(decision_note, 'auto-rejected:', 'system-superseded:')
        ELSE decision_note
    END
FROM proposals
"""

TRIGGER_SQL = """
CREATE TRIGGER IF NOT EXISTS claims_no_self_supersede_insert
BEFORE INSERT ON claims
FOR EACH ROW
WHEN NEW.supersedes_id IS NOT NULL AND NEW.supersedes_id = NEW.id
BEGIN
    SELECT RAISE(ABORT, 'claim cannot supersede itself');
END;

CREATE TRIGGER IF NOT EXISTS claims_no_self_supersede_update
BEFORE UPDATE OF supersedes_id ON claims
FOR EACH ROW
WHEN NEW.supersedes_id IS NOT NULL AND NEW.supersedes_id = NEW.id
BEGIN
    SELECT RAISE(ABORT, 'claim cannot supersede itself');
END;
"""


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
            (name,),
        ).fetchone()
        is not None
    )


def _rebuild_proposals(conn: sqlite3.Connection) -> int:
    """Extend status CHECK and migrate auto-rejected rows to superseded."""
    if not _has_table(conn, "proposals"):
        return 0
    before = conn.execute(
        """
        SELECT COUNT(*) AS c FROM proposals
        WHERE status = 'rejected'
          AND decision_note LIKE 'auto-rejected:%'
        """
    ).fetchone()
    migrated = int(before[0]) if before is not None else 0
    conn.execute(PROPOSALS_V020)
    conn.execute(COPY_PROPOSALS)
    conn.execute("DROP TABLE proposals")
    conn.execute("ALTER TABLE proposals_v020 RENAME TO proposals")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_proposals_status ON proposals(status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_proposals_target ON proposals(target_path)"
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_proposals_status_updated
        ON proposals(status, updated_at DESC)
        """
    )
    return migrated


def _repair_self_supersession(conn: sqlite3.Connection) -> int:
    if not _has_table(conn, "claims"):
        return 0
    cur = conn.execute(
        """
        UPDATE claims
        SET supersedes_id = NULL,
            updated_at = COALESCE(updated_at, created_at)
        WHERE supersedes_id IS NOT NULL AND supersedes_id = id
        """
    )
    return int(cur.rowcount or 0)


def apply(conn: sqlite3.Connection) -> None:
    from agentlog.db.migrations.fk import run_without_foreign_keys

    def _body() -> None:
        _rebuild_proposals(conn)
        _repair_self_supersession(conn)
        if _has_table(conn, "claims"):
            conn.executescript(TRIGGER_SQL)

    run_without_foreign_keys(conn, _body)
