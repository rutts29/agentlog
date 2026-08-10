from __future__ import annotations

import sqlite3

SQL = """
CREATE TABLE IF NOT EXISTS claims (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    subject TEXT NOT NULL,
    predicate TEXT NOT NULL,
    value_json TEXT NOT NULL,
    scope_type TEXT NOT NULL,
    scope_id TEXT,
    derivation TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'candidate',
    support_status TEXT NOT NULL DEFAULT 'ok',
    sample_size INTEGER NOT NULL DEFAULT 0,
    denominator INTEGER,
    rate REAL,
    observed_at TEXT NOT NULL,
    extractor_name TEXT NOT NULL,
    extractor_version TEXT NOT NULL,
    confidence_basis_json TEXT NOT NULL DEFAULT '{}',
    does_not_prove TEXT NOT NULL DEFAULT '',
    supersedes_id TEXT REFERENCES claims(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_claims_kind ON claims(kind);
CREATE INDEX IF NOT EXISTS idx_claims_status ON claims(status);
CREATE INDEX IF NOT EXISTS idx_claims_scope ON claims(scope_type, scope_id);
CREATE INDEX IF NOT EXISTS idx_claims_subject ON claims(subject, predicate);

CREATE TABLE IF NOT EXISTS claim_evidence (
    id TEXT PRIMARY KEY,
    claim_id TEXT NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    session_id TEXT,
    window_id TEXT,
    message_id TEXT,
    quote TEXT,
    meta_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_claim_evidence_claim ON claim_evidence(claim_id);
CREATE INDEX IF NOT EXISTS idx_claim_evidence_session ON claim_evidence(session_id);

CREATE TABLE IF NOT EXISTS proposals (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    action TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
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
    reviewed_at TEXT,
    review_note TEXT,
    applied_at TEXT,
    backup_path TEXT,
    applied_content_hash TEXT
);

CREATE INDEX IF NOT EXISTS idx_proposals_status ON proposals(status);
CREATE INDEX IF NOT EXISTS idx_proposals_target ON proposals(target_path);

CREATE TABLE IF NOT EXISTS proposal_claims (
    proposal_id TEXT NOT NULL REFERENCES proposals(id) ON DELETE CASCADE,
    claim_id TEXT NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    PRIMARY KEY (proposal_id, claim_id)
);

CREATE TABLE IF NOT EXISTS publication_events (
    id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL REFERENCES proposals(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_publication_events_proposal
ON publication_events(proposal_id);
"""


def apply(conn: sqlite3.Connection) -> None:
    conn.executescript(SQL)
