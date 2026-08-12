"""Permit a verified legacy session to become source-backed."""

from __future__ import annotations

import sqlite3


def apply(conn: sqlite3.Connection) -> None:
    columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(sessions)")
    }
    for column in (
        "source_sync_status",
        "source_sync_warning",
        "source_sync_checked_at",
    ):
        if column not in columns:
            conn.execute(f"ALTER TABLE sessions ADD COLUMN {column} TEXT")
    conn.executescript(
        """
        DROP TRIGGER IF EXISTS sessions_storage_immutable;

        CREATE TRIGGER sessions_storage_monotonic
        BEFORE UPDATE OF transcript_storage ON sessions
        WHEN old.transcript_storage != new.transcript_storage
          AND NOT (
              old.transcript_storage = 'legacy_materialized'
              AND new.transcript_storage = 'source_backed'
              AND new.artifact_id IS NOT NULL
              AND EXISTS (
                  SELECT 1 FROM artifacts a
                  WHERE a.id = new.artifact_id AND a.harness = new.harness
              )
              AND NOT EXISTS (
                  SELECT 1 FROM messages m
                  WHERE m.session_id = old.id AND m.text != ''
              )
          )
        BEGIN
            SELECT RAISE(ABORT, 'session transcript storage cannot move backward');
        END;
        """
    )
