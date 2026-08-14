"""Durable, content-addressed checkpoints for manual owner-insight runs."""

from __future__ import annotations

import sqlite3


SQL = """
CREATE TABLE IF NOT EXISTS owner_insight_batches (
    id TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL UNIQUE,
    prompt_hash TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    redaction_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('prepared', 'imported', 'blocked')),
    result_hash TEXT,
    prepared_at TEXT NOT NULL,
    imported_at TEXT,
    provenance_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS owner_insight_batch_messages (
    batch_id TEXT NOT NULL REFERENCES owner_insight_batches(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    role TEXT NOT NULL,
    source_snapshot_json TEXT NOT NULL DEFAULT '{}',
    source_role TEXT NOT NULL CHECK (source_role IN ('new', 'context')),
    PRIMARY KEY (batch_id, message_id)
);

CREATE INDEX IF NOT EXISTS idx_owner_insight_messages_session
ON owner_insight_batch_messages(session_id, message_id);

CREATE TABLE IF NOT EXISTS owner_insight_seen_messages (
    session_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    first_batch_id TEXT NOT NULL REFERENCES owner_insight_batches(id) ON DELETE RESTRICT,
    imported_batch_id TEXT REFERENCES owner_insight_batches(id) ON DELETE SET NULL,
    status TEXT NOT NULL CHECK (status IN ('prepared', 'imported', 'blocked')),
    PRIMARY KEY (session_id, message_id, generation)
);

CREATE TABLE IF NOT EXISTS owner_insight_session_state (
    session_id TEXT PRIMARY KEY,
    generation INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'ready' CHECK (status IN ('ready', 'blocked_rewrite')),
    rewrite_reason TEXT,
    checked_at TEXT NOT NULL,
    reset_at TEXT
);
"""


def apply(conn: sqlite3.Connection) -> None:
    conn.executescript(SQL)
