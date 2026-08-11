"""Make transcript retention an explicit, forward-only storage choice."""

from __future__ import annotations

import sqlite3


LEGACY_MATERIALIZED = "legacy_materialized"


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_storage_column(conn: sqlite3.Connection, table: str) -> None:
    if "transcript_storage" not in _columns(conn, table):
        conn.execute(
            f"ALTER TABLE {table} ADD COLUMN transcript_storage TEXT "
            f"NOT NULL DEFAULT '{LEGACY_MATERIALIZED}'"
        )


def _replace_fts_triggers(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        DROP TRIGGER IF EXISTS messages_ai;
        DROP TRIGGER IF EXISTS messages_ad;
        DROP TRIGGER IF EXISTS messages_au;
        DROP TRIGGER IF EXISTS messages_au_delete;
        DROP TRIGGER IF EXISTS messages_au_insert;

        CREATE TRIGGER messages_ai AFTER INSERT ON messages
        WHEN new.text != '' BEGIN
            INSERT INTO messages_fts(rowid, text) VALUES (new.rowid, new.text);
        END;

        CREATE TRIGGER messages_ad AFTER DELETE ON messages
        WHEN old.text != '' BEGIN
            INSERT INTO messages_fts(messages_fts, rowid, text)
            VALUES ('delete', old.rowid, old.text);
        END;

        CREATE TRIGGER messages_au_delete AFTER UPDATE OF text ON messages
        WHEN old.text != '' BEGIN
            INSERT INTO messages_fts(messages_fts, rowid, text)
            VALUES ('delete', old.rowid, old.text);
        END;

        CREATE TRIGGER messages_au_insert AFTER UPDATE OF text ON messages
        WHEN new.text != '' BEGIN
            INSERT INTO messages_fts(rowid, text) VALUES (new.rowid, new.text);
        END;
        """
    )


def _storage_guards(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TRIGGER IF NOT EXISTS artifacts_storage_insert
        BEFORE INSERT ON artifacts
        WHEN new.transcript_storage NOT IN ('legacy_materialized', 'source_backed')
        BEGIN
            SELECT RAISE(ABORT, 'invalid artifact transcript storage');
        END;

        CREATE TRIGGER IF NOT EXISTS sessions_storage_insert
        BEFORE INSERT ON sessions
        WHEN new.transcript_storage NOT IN ('legacy_materialized', 'source_backed')
        BEGIN
            SELECT RAISE(ABORT, 'invalid session transcript storage');
        END;

        CREATE TRIGGER IF NOT EXISTS artifacts_storage_immutable
        BEFORE UPDATE OF transcript_storage ON artifacts
        WHEN old.transcript_storage != new.transcript_storage
        BEGIN
            SELECT RAISE(ABORT, 'artifact transcript storage is immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS sessions_storage_immutable
        BEFORE UPDATE OF transcript_storage ON sessions
        WHEN old.transcript_storage != new.transcript_storage
        BEGIN
            SELECT RAISE(ABORT, 'session transcript storage is immutable');
        END;
        """
    )


def apply(conn: sqlite3.Connection) -> None:
    _add_storage_column(conn, "artifacts")
    _add_storage_column(conn, "sessions")
    _replace_fts_triggers(conn)
    _storage_guards(conn)
