from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentlog import source_reader
from agentlog.api.descriptive import session_detail_v2
from agentlog.db.schema import connect, init_db
from agentlog.ingest.base import TranscriptAdapter, content_hash_text, hash_prefix
from agentlog.normalize.models import (
    Harness,
    NormalizedMessage,
    NormalizedSession,
    ParseResult,
)
from agentlog.source_reader import CachedSourceTranscriptReader, read_source_transcript


def _line(role: str, text: str) -> bytes:
    return (
        json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": role,
                    "content": [
                        {
                            "type": "input_text" if role == "user" else "output_text",
                            "text": text,
                        }
                    ],
                },
            }
        )
        + "\n"
    ).encode()


class _MultiSessionSqliteAdapter(TranscriptAdapter):
    harness = Harness.T3CODE
    supports_byte_append = False
    parse_calls = 0

    def discover(self) -> list[Path]:
        return []

    def parse_chunk(self, path, data, *, start_offset):
        raise NotImplementedError

    def parse_path(self, path, data, *, start_offset):
        type(self).parse_calls += 1
        return [
            ParseResult(
                session=NormalizedSession(
                    harness=Harness.T3CODE,
                    external_id=external_id,
                ),
                messages=[
                    NormalizedMessage(
                        seq=1,
                        role="user",
                        text=text,
                        content_hash=content_hash_text(text),
                    )
                ],
            )
            for external_id, text in (
                ("first", "first shared source message"),
                ("second", "second shared source message"),
            )
        ]


class SourceTranscriptReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.path = root / (
            "rollout-2026-08-11T10-00-00-019fbdec-7065-7470-bb1e-dfa6c0d38237.jsonl"
        )
        self.initial = _line("user", "initial request") + _line("assistant", "initial reply")
        self.path.write_bytes(self.initial)
        self.conn = connect(root / "agentlog.db")
        init_db(self.conn)
        self.conn.execute(
            """
            INSERT INTO artifacts
              (harness, path, size, mtime_ns, content_hash, parsed_offset,
               parser_version, transcript_storage)
            VALUES (?, ?, ?, ?, ?, ?, 'source-test', 'legacy_materialized')
            """,
            (
                "codex",
                str(self.path),
                len(self.initial),
                self.path.stat().st_mtime_ns,
                hash_prefix(self.path, len(self.initial)),
                len(self.initial),
            ),
        )
        artifact_id = self.conn.execute("SELECT id FROM artifacts").fetchone()["id"]
        self.session_id = "codex:019fbdec-7065-7470-bb1e-dfa6c0d38237"
        self.conn.execute(
            """
            INSERT INTO sessions
              (id, harness, external_id, artifact_id, transcript_storage)
            VALUES (?, 'codex', '019fbdec-7065-7470-bb1e-dfa6c0d38237', ?, 'source_backed')
            """,
            (self.session_id, artifact_id),
        )
        self.conn.execute(
            """
            INSERT INTO messages (id, session_id, seq, role, text, content_hash)
            VALUES (?, ?, ?, ?, '', ?)
            """,
            (
                f"{self.session_id}:m:1",
                self.session_id,
                1,
                "user",
                content_hash_text("initial request"),
            ),
        )
        self.conn.execute(
            """
            INSERT INTO messages (id, session_id, seq, role, text, content_hash)
            VALUES (?, ?, ?, ?, '', ?)
            """,
            (
                f"{self.session_id}:m:2",
                self.session_id,
                2,
                "assistant",
                content_hash_text("initial reply"),
            ),
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_source_backed_session_reads_from_legacy_artifact(self) -> None:
        artifact = self.conn.execute(
            "SELECT transcript_storage FROM artifacts"
        ).fetchone()
        self.assertEqual(artifact["transcript_storage"], "legacy_materialized")
        before_changes = self.conn.total_changes
        result = read_source_transcript(self.conn, self.session_id)

        self.assertTrue(result.ready)
        self.assertEqual([m["text"] for m in result.messages], ["initial request", "initial reply"])
        self.assertEqual(result.source_unit_id, self.session_id)
        assert result.locator is not None
        self.assertEqual(result.locator.artifact_kind, "jsonl")
        self.assertIsNotNone(result.source_identity)
        self.assertIsNotNone(result.source_hash)
        self.assertEqual(self.conn.total_changes, before_changes)
        row = self.conn.execute("SELECT text FROM messages WHERE session_id = ?", (self.session_id,)).fetchone()
        self.assertEqual(row["text"], "")

    def test_append_is_visible_and_rewritten_checkpoint_fails_closed(self) -> None:
        self.path.write_bytes(self.initial + _line("user", "new live turn"))

        ready = read_source_transcript(self.conn, self.session_id)
        self.assertTrue(ready.ready)
        self.assertEqual(ready.messages[-1]["text"], "new live turn")

        self.path.write_bytes(_line("user", "rewritten history"))
        changed = read_source_transcript(self.conn, self.session_id)
        self.assertEqual(changed.status, "source_changed")
        self.assertEqual(changed.messages, [])

    def test_retries_when_source_changes_after_parse_before_hash(self) -> None:
        original_hash = source_reader._current_hash
        calls = 0

        def append_before_hash(path: Path) -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                path.write_bytes(path.read_bytes() + _line("user", "boundary append"))
            return original_hash(path)

        with patch("agentlog.source_reader._current_hash", side_effect=append_before_hash):
            result = read_source_transcript(self.conn, self.session_id)

        self.assertTrue(result.ready)
        self.assertGreaterEqual(calls, 2)
        self.assertEqual(result.messages[-1]["text"], "boundary append")

    def test_checkpoint_blocked_parse_is_not_served_as_ready(self) -> None:
        blocked = ParseResult(
            session=NormalizedSession(
                harness=Harness.CODEX,
                external_id="019fbdec-7065-7470-bb1e-dfa6c0d38237",
            ),
            extras={
                "checkpoint_blocked": True,
                "checkpoint_blocked_reason": "ambiguous fork boundary",
            },
        )
        with patch(
            "agentlog.source_reader._parse_current",
            return_value=([blocked], "blocked-source-hash"),
        ):
            result = read_source_transcript(self.conn, self.session_id)

        self.assertEqual(result.status, "source_changed")
        self.assertEqual(result.messages, [])
        self.assertEqual(result.warning, "ambiguous fork boundary")

    def test_operation_cache_parses_and_fingerprints_shared_sqlite_once(self) -> None:
        sqlite_path = Path(self._tmp.name) / "shared.sqlite"
        source = sqlite3.connect(sqlite_path)
        source.execute("CREATE TABLE source_marker (id INTEGER PRIMARY KEY)")
        source.commit()
        source.close()
        self.conn.execute(
            """
            INSERT INTO artifacts
              (harness, path, size, mtime_ns, content_hash, parsed_offset,
               parser_version, transcript_storage)
            VALUES ('t3code', ?, ?, ?, 'shared', 0, 'source-test',
                    'legacy_materialized')
            """,
            (
                str(sqlite_path),
                sqlite_path.stat().st_size,
                sqlite_path.stat().st_mtime_ns,
            ),
        )
        artifact_id = self.conn.execute(
            "SELECT MAX(id) AS id FROM artifacts"
        ).fetchone()["id"]
        for external_id, text in (
            ("first", "first shared source message"),
            ("second", "second shared source message"),
        ):
            session_id = f"t3code:{external_id}"
            self.conn.execute(
                """
                INSERT INTO sessions
                  (id, harness, external_id, artifact_id, transcript_storage)
                VALUES (?, 't3code', ?, ?, 'source_backed')
                """,
                (session_id, external_id, artifact_id),
            )
            self.conn.execute(
                """
                INSERT INTO messages
                  (id, session_id, seq, role, text, content_hash)
                VALUES (?, ?, 1, 'user', '', ?)
                """,
                (f"{session_id}:m:1", session_id, content_hash_text(text)),
            )
        self.conn.commit()

        _MultiSessionSqliteAdapter.parse_calls = 0
        reader = CachedSourceTranscriptReader()
        with (
            patch.dict(
                source_reader._ADAPTERS,
                {Harness.T3CODE.value: _MultiSessionSqliteAdapter},
            ),
            patch(
                "agentlog.source_reader.sqlite_fingerprint",
                wraps=source_reader.sqlite_fingerprint,
            ) as fingerprint,
        ):
            first = reader(self.conn, "t3code:first")
            second = reader(self.conn, "t3code:second")

        self.assertTrue(first.ready)
        self.assertTrue(second.ready)
        self.assertEqual(_MultiSessionSqliteAdapter.parse_calls, 1)
        self.assertEqual(fingerprint.call_count, 1)

    def test_sqlite_rewrite_with_same_session_id_fails_closed(self) -> None:
        source_path = Path(self._tmp.name) / "hermes.db"
        source = sqlite3.connect(source_path)
        source.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY, parent_session_id TEXT, started_at TEXT,
                ended_at TEXT, model TEXT, cwd TEXT, git_branch TEXT,
                git_repo_root TEXT, title TEXT, model_config TEXT
            );
            CREATE TABLE messages (
                id TEXT PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,
                tool_call_id TEXT, tool_calls TEXT, tool_name TEXT,
                timestamp TEXT, token_count INTEGER, reasoning TEXT,
                reasoning_content TEXT, api_content TEXT, active INTEGER,
                compacted INTEGER
            );
            INSERT INTO sessions VALUES
                ('stable-id', NULL, '2026-08-11T10:00:00+00:00', NULL,
                 NULL, NULL, NULL, NULL, NULL, NULL);
            INSERT INTO messages VALUES
                ('message-1', 'stable-id', 'user', 'original SQLite request',
                 NULL, NULL, NULL, '2026-08-11T10:00:01+00:00', NULL,
                 NULL, NULL, NULL, 1, 0);
            """
        )
        source.commit()
        source.close()
        self.conn.execute(
            """
            INSERT INTO artifacts
              (harness, path, size, mtime_ns, content_hash, parsed_offset,
               parser_version, transcript_storage)
            VALUES ('hermes', ?, ?, ?, 'sqlite-fixture', 0, 'source-test',
                    'legacy_materialized')
            """,
            (str(source_path), source_path.stat().st_size, source_path.stat().st_mtime_ns),
        )
        artifact_id = self.conn.execute("SELECT MAX(id) AS id FROM artifacts").fetchone()["id"]
        session_id = "hermes:stable-id"
        self.conn.execute(
            """
            INSERT INTO sessions
              (id, harness, external_id, artifact_id, transcript_storage)
            VALUES (?, 'hermes', 'stable-id', ?, 'source_backed')
            """,
            (session_id, artifact_id),
        )
        self.conn.execute(
            """
            INSERT INTO messages (id, session_id, seq, role, text, content_hash)
            VALUES (?, ?, 1, 'user', '', ?)
            """,
            (f"{session_id}:m:1", session_id, content_hash_text("original SQLite request")),
        )
        self.conn.commit()

        self.assertTrue(read_source_transcript(self.conn, session_id).ready)
        source = sqlite3.connect(source_path)
        source.execute("UPDATE messages SET content = 'rewritten SQLite request' WHERE id = 'message-1'")
        source.commit()
        source.close()

        changed = read_source_transcript(self.conn, session_id)
        self.assertEqual(changed.status, "source_changed")
        self.assertEqual(changed.messages, [])

    def test_detail_uses_source_text_and_legacy_keeps_database_text(self) -> None:
        detail = session_detail_v2(self.conn, self.session_id)
        assert detail is not None
        self.assertEqual(detail["messages"][1]["text"], "initial reply")
        self.assertEqual(detail["transcript"]["source"]["status"], "ready")

        legacy_id = "codex:legacy"
        self.conn.execute(
            """
            INSERT INTO sessions (id, harness, external_id, transcript_storage)
            VALUES (?, 'codex', 'legacy', 'legacy_materialized')
            """,
            (legacy_id,),
        )
        self.conn.execute(
            """
            INSERT INTO messages (id, session_id, seq, role, text, content_hash)
            VALUES (?, ?, 1, 'user', 'durable legacy text', 'legacy')
            """,
            (f"{legacy_id}:m:1", legacy_id),
        )
        self.conn.commit()
        legacy = session_detail_v2(self.conn, legacy_id)
        assert legacy is not None
        self.assertEqual(legacy["messages"][0]["text"], "durable legacy text")
        self.assertNotIn("source", legacy["transcript"])


if __name__ == "__main__":
    unittest.main()
