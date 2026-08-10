from __future__ import annotations

import sqlite3

PROPOSALS_V017 = """
CREATE TABLE proposals_v017 (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    action TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'accepted', 'rejected', 'deferred')),
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

COPY_V017 = """
INSERT INTO proposals_v017 (
    id, title, action, status, target_path, target_kind, scope_type, scope_id,
    base_content_hash, unified_diff, proposed_content, rationale,
    derivation_summary, does_not_prove, sample_size, created_at, updated_at,
    decided_at, decision_note
)
SELECT
    id, title, action,
    CASE
        WHEN status IN ('approved', 'applied', 'accepted') THEN 'accepted'
        WHEN status = 'rejected' THEN 'rejected'
        WHEN status = 'deferred' THEN 'deferred'
        ELSE 'pending'
    END,
    target_path, target_kind, scope_type, scope_id,
    base_content_hash, unified_diff, proposed_content, rationale,
    derivation_summary, does_not_prove, sample_size, created_at, updated_at,
    reviewed_at, review_note
FROM proposals
"""

INDEXES_V017 = """
CREATE INDEX IF NOT EXISTS idx_proposals_status ON proposals(status);
CREATE INDEX IF NOT EXISTS idx_proposals_target ON proposals(target_path);
CREATE INDEX IF NOT EXISTS idx_proposals_status_updated
ON proposals(status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_proposal_events_proposal
ON proposal_events(proposal_id);
"""


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
            (name,),
        ).fetchone()
        is not None
    )


def _rebuild_proposals(conn: sqlite3.Connection) -> None:
    """Drop apply-era bookkeeping; decisions are the only recorded outcome."""
    if not _has_table(conn, "proposals"):
        return
    conn.execute(PROPOSALS_V017)
    conn.execute(COPY_V017)
    conn.execute("DROP TABLE proposals")
    conn.execute("ALTER TABLE proposals_v017 RENAME TO proposals")


def _rename_events(conn: sqlite3.Connection) -> None:
    if _has_table(conn, "proposal_events") or not _has_table(
        conn, "publication_events"
    ):
        return
    conn.execute("DROP INDEX IF EXISTS idx_publication_events_proposal")
    conn.execute("ALTER TABLE publication_events RENAME TO proposal_events")


def apply(conn: sqlite3.Connection) -> None:
    from agentlog.db.migrations.fk import run_without_foreign_keys

    def _body() -> None:
        _rebuild_proposals(conn)
        _rename_events(conn)
        if _has_table(conn, "proposal_events"):
            conn.executescript(INDEXES_V017)

    run_without_foreign_keys(conn, _body)
