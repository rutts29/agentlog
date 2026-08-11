from __future__ import annotations

import sqlite3
import unittest

from agentlog.db.migrations.v027_transcript_storage import apply as apply_v027


OLD_SCHEMA = """
CREATE TABLE artifacts (
    id INTEGER PRIMARY KEY,
    harness TEXT NOT NULL,
    path TEXT NOT NULL UNIQUE,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    parsed_offset INTEGER NOT NULL DEFAULT 0,
    parser_version TEXT NOT NULL
);

CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    harness TEXT NOT NULL,
    external_id TEXT NOT NULL,
    artifact_id INTEGER REFERENCES artifacts(id) ON DELETE CASCADE,
    UNIQUE (harness, external_id)
);

CREATE TABLE messages (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    role TEXT NOT NULL,
    text TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL DEFAULT '',
    UNIQUE (session_id, seq)
);

CREATE VIRTUAL TABLE messages_fts USING fts5(
    text,
    content='messages',
    content_rowid='rowid',
    tokenize='porter unicode61'
);

CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, text) VALUES (new.rowid, new.text);
END;

CREATE TRIGGER messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text)
    VALUES ('delete', old.rowid, old.text);
END;

CREATE TRIGGER messages_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text)
    VALUES ('delete', old.rowid, old.text);
    INSERT INTO messages_fts(rowid, text) VALUES (new.rowid, new.text);
END;

CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
"""


class TranscriptStorageMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(OLD_SCHEMA)
        self.conn.executemany(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            ((version, "2026-08-11T00:00:00+00:00") for version in range(1, 27)),
        )
        self.conn.execute(
            """
            INSERT INTO artifacts
            (harness, path, size, mtime_ns, content_hash, parsed_offset, parser_version)
            VALUES ('codex', '/tmp/legacy.jsonl', 10, 1, 'legacy-hash', 10, '26')
            """
        )
        artifact_id = self.conn.execute(
            "SELECT id FROM artifacts WHERE path = '/tmp/legacy.jsonl'"
        ).fetchone()["id"]
        self.conn.execute(
            """
            INSERT INTO sessions(id, harness, external_id, artifact_id)
            VALUES ('codex:legacy', 'codex', 'legacy', ?)
            """,
            (artifact_id,),
        )
        self.conn.execute(
            """
            INSERT INTO messages(id, session_id, seq, role, text, content_hash)
            VALUES ('legacy-message', 'codex:legacy', 1, 'user',
                    'legacy transcript survives migration', 'legacy-message-hash')
            """
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    def test_upgrade_from_v026_preserves_legacy_text_and_fts(self) -> None:
        self.assertEqual(
            self.conn.execute(
                "SELECT text FROM messages_fts WHERE messages_fts MATCH 'legacy'"
            ).fetchall(),
            [self.conn.execute("SELECT text FROM messages").fetchone()],
        )

        apply_v027(self.conn)

        self.assertEqual(
            self.conn.execute(
                "SELECT transcript_storage FROM artifacts"
            ).fetchone()["transcript_storage"],
            "legacy_materialized",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT transcript_storage FROM sessions"
            ).fetchone()["transcript_storage"],
            "legacy_materialized",
        )
        self.assertEqual(
            self.conn.execute("SELECT text FROM messages").fetchone()["text"],
            "legacy transcript survives migration",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) AS count FROM messages_fts "
                "WHERE messages_fts MATCH 'legacy'"
            ).fetchone()["count"],
            1,
        )

    def test_source_backed_empty_message_does_not_create_fts_posting(self) -> None:
        apply_v027(self.conn)
        artifact_id = self.conn.execute(
            """
            INSERT INTO artifacts
            (harness, path, size, mtime_ns, content_hash, parsed_offset, parser_version,
             transcript_storage)
            VALUES ('codex', '/tmp/source.jsonl', 0, 2, 'source-hash', 0, '27',
                    'source_backed')
            RETURNING id
            """
        ).fetchone()["id"]
        self.conn.execute(
            """
            INSERT INTO sessions
            (id, harness, external_id, artifact_id, transcript_storage)
            VALUES ('codex:source', 'codex', 'source', ?, 'source_backed')
            """,
            (artifact_id,),
        )
        self.conn.execute(
            """
            INSERT INTO messages(id, session_id, seq, role, text, content_hash)
            VALUES ('source-message', 'codex:source', 1, 'user', '', 'source-message-hash')
            """
        )
        self.conn.commit()

        source_rowid = self.conn.execute(
            "SELECT rowid FROM messages WHERE id = 'source-message'"
        ).fetchone()[0]
        legacy_rowid = self.conn.execute(
            "SELECT rowid FROM messages WHERE id = 'legacy-message'"
        ).fetchone()[0]
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM messages_fts_docsize WHERE id = ?",
                (source_rowid,),
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM messages_fts_docsize WHERE id = ?",
                (legacy_rowid,),
            ).fetchone()[0],
            1,
        )

        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) AS count FROM messages_fts "
                "WHERE messages_fts MATCH 'source'"
            ).fetchone()["count"],
            0,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) AS count FROM messages_fts "
                "WHERE messages_fts MATCH 'legacy'"
            ).fetchone()["count"],
            1,
        )

    def test_storage_guards_reject_invalid_modes_and_updates(self) -> None:
        apply_v027(self.conn)
        artifact_id = self.conn.execute(
            "SELECT id FROM artifacts WHERE path = '/tmp/legacy.jsonl'"
        ).fetchone()["id"]

        with self.assertRaisesRegex(sqlite3.IntegrityError, "invalid artifact"):
            self.conn.execute(
                """
                INSERT INTO artifacts
                (harness, path, size, mtime_ns, content_hash, parsed_offset, parser_version,
                 transcript_storage)
                VALUES ('codex', '/tmp/invalid.jsonl', 0, 3, 'invalid', 0, '27', 'invalid')
                """
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "invalid session"):
            self.conn.execute(
                """
                INSERT INTO sessions
                (id, harness, external_id, artifact_id, transcript_storage)
                VALUES ('codex:invalid', 'codex', 'invalid', ?, 'invalid')
                """,
                (artifact_id,),
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "artifact transcript storage"):
            self.conn.execute(
                "UPDATE artifacts SET transcript_storage = 'source_backed' WHERE id = ?",
                (artifact_id,),
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "session transcript storage"):
            self.conn.execute(
                "UPDATE sessions SET transcript_storage = 'source_backed' "
                "WHERE id = 'codex:legacy'"
            )


if __name__ == "__main__":
    unittest.main()
