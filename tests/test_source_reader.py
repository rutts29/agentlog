from __future__ import annotations

import json
import hashlib
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from agentlog import source_reader
from agentlog.api.app import create_app
from agentlog.api.descriptive import session_detail_v2
from agentlog.db.schema import connect, init_db
from agentlog.ingest.base import TranscriptAdapter, content_hash_text, hash_prefix
from agentlog.ingest.sqlite_ro import open_sqlite_readonly
from agentlog.normalize.models import (
    Harness,
    NormalizedMessage,
    NormalizedSession,
    ParseResult,
)
from agentlog.source_reader import (
    CachedSourceTranscriptReader,
    SourceReadResult,
    read_source_transcript,
)


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


class _BlockingMultiSessionHermesAdapter(TranscriptAdapter):
    harness = Harness.HERMES
    supports_byte_append = False
    first_read: threading.Event | None = None
    release_read: threading.Event | None = None
    parse_calls = 0

    def discover(self) -> list[Path]:
        return []

    def parse_chunk(self, path, data, *, start_offset):
        raise NotImplementedError

    def parse_path(self, path, data, *, start_offset):
        type(self).parse_calls += 1
        if self.first_read is not None and not self.first_read.is_set():
            self.first_read.set()
            if self.release_read is not None:
                self.release_read.wait(timeout=2)
        return [
            ParseResult(
                session=NormalizedSession(
                    harness=Harness.HERMES,
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


class _BlockingCapturedJsonlAdapter(TranscriptAdapter):
    harness = Harness.CODEX
    supports_byte_append = True
    first_read: threading.Event | None = None
    release_read: threading.Event | None = None
    parse_calls = 0

    def discover(self) -> list[Path]:
        return []

    def parse_chunk(self, path, data, *, start_offset):
        raise NotImplementedError

    def parse_path(self, path, data, *, start_offset):
        type(self).parse_calls += 1
        if self.first_read is not None and not self.first_read.is_set():
            self.first_read.set()
            if self.release_read is not None:
                self.release_read.wait(timeout=2)
        messages = []
        for seq, line in enumerate(data.splitlines(), start=1):
            payload = json.loads(line)["payload"]
            role = payload["role"]
            text = payload["content"][0]["text"]
            messages.append(
                NormalizedMessage(
                    seq=seq,
                    role=role,
                    text=text,
                    content_hash=content_hash_text(text),
                )
            )
        return [
            ParseResult(
                session=NormalizedSession(
                    harness=Harness.CODEX,
                    external_id="019fbdec-7065-7470-bb1e-dfa6c0d38237",
                ),
                messages=messages,
            )
        ]


class _TargetedSqliteAdapter(TranscriptAdapter):
    harness = Harness.T3CODE
    supports_byte_append = False
    first_read: threading.Event | None = None
    release_read: threading.Event | None = None
    parse_calls = 0

    def discover(self) -> list[Path]:
        return []

    def parse_chunk(self, path, data, *, start_offset):
        raise NotImplementedError

    def parse_path(self, path, data, *, start_offset):
        raise NotImplementedError

    def parse_session(self, path: Path, external_id: str) -> ParseResult | None:
        type(self).parse_calls += 1
        with open_sqlite_readonly(path) as conn:
            conn.execute("BEGIN")
            row = conn.execute(
                "SELECT text FROM source_messages WHERE session_id = ?",
                (external_id,),
            ).fetchone()
            conn.rollback()
        if row is None:
            return None
        if self.first_read is not None and not self.first_read.is_set():
            self.first_read.set()
            if self.release_read is not None:
                self.release_read.wait(timeout=2)
        text = str(row["text"])
        return ParseResult(
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


class _RevisionedTargetedSqliteAdapter(_TargetedSqliteAdapter):
    revision_calls = 0
    parse_with_hash_calls = 0

    def session_revision(self, path: Path, external_id: str) -> str | None:
        type(self).revision_calls += 1
        with open_sqlite_readonly(path) as conn:
            row = conn.execute(
                "SELECT text FROM source_messages WHERE session_id = ?",
                (external_id,),
            ).fetchone()
        return (
            hashlib.sha256(str(row["text"]).encode()).hexdigest()
            if row is not None
            else None
        )

    def parse_session_with_hash(
        self, path: Path, external_id: str
    ) -> tuple[ParseResult | None, str]:
        type(self).parse_with_hash_calls += 1
        result = self.parse_session(path, external_id)
        return result, source_reader._parse_result_hash(result)


class _GlobalDependencyTargetedSqliteAdapter(_RevisionedTargetedSqliteAdapter):
    provider_id = "019febdf-eb13-7ee0-8110-26c0bb81a177"

    def session_revision(self, path: Path, external_id: str) -> str | None:
        type(self).revision_calls += 1
        with open_sqlite_readonly(path) as conn:
            known_ids = [
                str(row[0])
                for row in conn.execute(
                    "SELECT session_id FROM source_messages ORDER BY session_id"
                )
            ]
        return hashlib.sha256(repr((external_id, known_ids)).encode()).hexdigest()

    def parse_session(self, path: Path, external_id: str) -> ParseResult | None:
        result = super().parse_session(path, external_id)
        if result is None:
            return None
        with open_sqlite_readonly(path) as conn:
            known_ids = {
                str(row[0])
                for row in conn.execute("SELECT session_id FROM source_messages")
            }
        result.extras["session_links"] = (
            [] if self.provider_id in known_ids else [{"target_external_id": self.provider_id}]
        )
        return result


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

    def _add_revisioned_t3_session(
        self, external_id: str, text: str, *, revision: int = 1
    ) -> Path:
        sqlite_path = Path(self._tmp.name) / f"{external_id}.sqlite"
        source = sqlite3.connect(sqlite_path)
        source.execute(
            "CREATE TABLE source_messages (session_id TEXT PRIMARY KEY, text TEXT, revision INTEGER)"
        )
        source.execute(
            "INSERT INTO source_messages VALUES (?, ?, ?)",
            (external_id, text, revision),
        )
        source.commit()
        source.close()
        artifact_id = self.conn.execute(
            """
            INSERT INTO artifacts
              (harness, path, size, mtime_ns, content_hash, parsed_offset,
               parser_version, transcript_storage)
            VALUES ('t3code', ?, ?, ?, 'sqlite-fixture', 0, 'source-test',
                    'legacy_materialized')
            RETURNING id
            """,
            (str(sqlite_path), sqlite_path.stat().st_size, sqlite_path.stat().st_mtime_ns),
        ).fetchone()["id"]
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
            INSERT INTO messages (id, session_id, seq, role, text, content_hash)
            VALUES (?, ?, 1, 'user', '', ?)
            """,
            (f"{session_id}:m:1", session_id, content_hash_text(text)),
        )
        self.conn.commit()
        return sqlite_path

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

    def test_parse_retries_after_same_size_tail_rewrite_during_parse(self) -> None:
        original = self.initial + _line("user", "new cached turn")
        rewritten = self.initial + _line("user", "old cached turn")
        self.assertEqual(len(original), len(rewritten))
        self.path.write_bytes(original)
        original_stat = self.path.stat()
        _BlockingCapturedJsonlAdapter.parse_calls = 0
        first_read = threading.Event()
        release_read = threading.Event()
        _BlockingCapturedJsonlAdapter.first_read = first_read
        _BlockingCapturedJsonlAdapter.release_read = release_read
        result: list[SourceReadResult] = []
        failures: list[BaseException] = []
        db_path = Path(self._tmp.name) / "agentlog.db"

        def read() -> None:
            conn = connect(db_path)
            try:
                result.append(read_source_transcript(conn, self.session_id))
            except BaseException as exc:
                failures.append(exc)
            finally:
                conn.close()

        with patch.dict(
            source_reader._ADAPTERS,
            {Harness.CODEX.value: _BlockingCapturedJsonlAdapter},
        ):
            thread = threading.Thread(target=read)
            thread.start()
            self.assertTrue(first_read.wait(timeout=2))
            self.path.write_bytes(rewritten)
            os.utime(
                self.path,
                ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
            )
            release_read.set()
            thread.join(timeout=2)

        self.assertEqual(failures, [])
        self.assertFalse(thread.is_alive())
        self.assertEqual(_BlockingCapturedJsonlAdapter.parse_calls, 2)
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0].ready)
        self.assertEqual(result[0].messages[-1]["text"], "old cached turn")

    def test_checkpoint_is_rechecked_after_a_stable_jsonl_parse(self) -> None:
        original_parse = source_reader._parse_current

        def rewrite_after_parse(adapter: TranscriptAdapter, path: Path):
            parsed = original_parse(adapter, path)
            path.write_bytes(_line("user", "rewritten history"))
            return parsed

        with patch(
            "agentlog.source_reader._parse_current", side_effect=rewrite_after_parse
        ):
            result = read_source_transcript(self.conn, self.session_id)

        self.assertEqual(result.status, "source_changed")
        self.assertEqual(result.messages, [])

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

    def test_t3_unrelated_write_does_not_block_stable_session(self) -> None:
        sqlite_path = Path(self._tmp.name) / "state.sqlite"
        source = sqlite3.connect(sqlite_path)
        source.execute(
            "CREATE TABLE source_messages (session_id TEXT PRIMARY KEY, text TEXT)"
        )
        source.executemany(
            "INSERT INTO source_messages VALUES (?, ?)",
            [("changing", "old A"), ("stable", "unchanged B")],
        )
        source.commit()
        source.close()
        artifact_id = self.conn.execute(
            """
            INSERT INTO artifacts
              (harness, path, size, mtime_ns, content_hash, parsed_offset,
               parser_version, transcript_storage)
            VALUES ('t3code', ?, ?, ?, 'sqlite-fixture', 0, 'source-test',
                    'legacy_materialized')
            RETURNING id
            """,
            (str(sqlite_path), sqlite_path.stat().st_size, sqlite_path.stat().st_mtime_ns),
        ).fetchone()["id"]
        for external_id, text in (("changing", "old A"), ("stable", "unchanged B")):
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
                INSERT INTO messages (id, session_id, seq, role, text, content_hash)
                VALUES (?, ?, 1, 'user', '', ?)
                """,
                (f"{session_id}:m:1", session_id, content_hash_text(text)),
            )
        self.conn.commit()

        _TargetedSqliteAdapter.first_read = None
        _TargetedSqliteAdapter.release_read = None
        with patch.dict(
            source_reader._ADAPTERS,
            {Harness.T3CODE.value: _TargetedSqliteAdapter},
        ):
            cached_reader = CachedSourceTranscriptReader()
            stable_cached = cached_reader(self.conn, "t3code:stable")
            changing_cached = CachedSourceTranscriptReader()(
                self.conn, "t3code:changing"
            )
        self.assertTrue(stable_cached.ready)
        self.assertTrue(changing_cached.ready)
        self.assertEqual([m["text"] for m in stable_cached.messages], ["unchanged B"])
        self.assertEqual([m["text"] for m in changing_cached.messages], ["old A"])
        with patch.dict(
            source_reader._ADAPTERS,
            {Harness.T3CODE.value: _TargetedSqliteAdapter},
        ):
            self.assertTrue(cached_reader.verify_current())

        first_read = threading.Event()
        release_read = threading.Event()
        _TargetedSqliteAdapter.first_read = first_read
        _TargetedSqliteAdapter.release_read = release_read
        writer_done = threading.Event()

        def writer() -> None:
            self.assertTrue(first_read.wait(timeout=2))
            writer_conn = sqlite3.connect(sqlite_path)
            writer_conn.execute(
                "UPDATE source_messages SET text = 'new A' WHERE session_id = 'changing'"
            )
            writer_conn.commit()
            writer_conn.close()
            writer_done.set()
            release_read.set()

        with patch.dict(
            source_reader._ADAPTERS,
            {Harness.T3CODE.value: _TargetedSqliteAdapter},
        ):
            thread = threading.Thread(target=writer)
            thread.start()
            result = read_source_transcript(self.conn, "t3code:stable")
            thread.join(timeout=2)

        self.assertTrue(writer_done.is_set())
        self.assertTrue(result.ready)
        self.assertEqual([m["text"] for m in result.messages], ["unchanged B"])
        with patch.dict(
            source_reader._ADAPTERS,
            {Harness.T3CODE.value: _TargetedSqliteAdapter},
        ):
            reread = cached_reader(self.conn, "t3code:stable")
            self.assertTrue(cached_reader.verify_current())
        self.assertTrue(reread.ready)
        self.assertEqual([m["text"] for m in reread.messages], ["unchanged B"])

    def test_t3_requested_rewrite_still_fails_closed(self) -> None:
        sqlite_path = Path(self._tmp.name) / "state.sqlite"
        source = sqlite3.connect(sqlite_path)
        source.execute(
            "CREATE TABLE source_messages (session_id TEXT PRIMARY KEY, text TEXT)"
        )
        source.execute("INSERT INTO source_messages VALUES ('target', 'old')")
        source.commit()
        source.close()
        artifact_id = self.conn.execute(
            """
            INSERT INTO artifacts
              (harness, path, size, mtime_ns, content_hash, parsed_offset,
               parser_version, transcript_storage)
            VALUES ('t3code', ?, ?, ?, 'sqlite-fixture', 0, 'source-test',
                    'legacy_materialized')
            RETURNING id
            """,
            (str(sqlite_path), sqlite_path.stat().st_size, sqlite_path.stat().st_mtime_ns),
        ).fetchone()["id"]
        self.conn.execute(
            """
            INSERT INTO sessions
              (id, harness, external_id, artifact_id, transcript_storage)
            VALUES ('t3code:target', 't3code', 'target', ?, 'source_backed')
            """,
            (artifact_id,),
        )
        self.conn.execute(
            """
            INSERT INTO messages (id, session_id, seq, role, text, content_hash)
            VALUES ('t3code:target:m:1', 't3code:target', 1, 'user', '', ?)
            """,
            (content_hash_text("old"),),
        )
        self.conn.commit()

        writer_conn = sqlite3.connect(sqlite_path)
        writer_conn.execute(
            "UPDATE source_messages SET text = 'rewritten' WHERE session_id = 'target'"
        )
        writer_conn.commit()
        writer_conn.close()

        with patch.dict(
            source_reader._ADAPTERS,
            {Harness.T3CODE.value: _TargetedSqliteAdapter},
        ):
            result = read_source_transcript(self.conn, "t3code:target")

        self.assertEqual(result.status, "source_changed")
        self.assertEqual(result.messages, [])

    def test_t3_cached_same_thread_rewrite_invalidates_verification(self) -> None:
        sqlite_path = Path(self._tmp.name) / "state.sqlite"
        source = sqlite3.connect(sqlite_path)
        source.execute(
            "CREATE TABLE source_messages (session_id TEXT PRIMARY KEY, text TEXT)"
        )
        source.execute("INSERT INTO source_messages VALUES ('target', 'old')")
        source.commit()
        source.close()
        artifact_id = self.conn.execute(
            """
            INSERT INTO artifacts
              (harness, path, size, mtime_ns, content_hash, parsed_offset,
               parser_version, transcript_storage)
            VALUES ('t3code', ?, ?, ?, 'sqlite-fixture', 0, 'source-test',
                    'legacy_materialized')
            RETURNING id
            """,
            (str(sqlite_path), sqlite_path.stat().st_size, sqlite_path.stat().st_mtime_ns),
        ).fetchone()["id"]
        self.conn.execute(
            """
            INSERT INTO sessions
              (id, harness, external_id, artifact_id, transcript_storage)
            VALUES ('t3code:target', 't3code', 'target', ?, 'source_backed')
            """,
            (artifact_id,),
        )
        self.conn.execute(
            """
            INSERT INTO messages (id, session_id, seq, role, text, content_hash)
            VALUES ('t3code:target:m:1', 't3code:target', 1, 'user', '', ?)
            """,
            (content_hash_text("old"),),
        )
        self.conn.commit()

        with patch.dict(
            source_reader._ADAPTERS,
            {Harness.T3CODE.value: _TargetedSqliteAdapter},
        ):
            reader = CachedSourceTranscriptReader()
            self.assertTrue(reader(self.conn, "t3code:target").ready)
            self.assertTrue(reader.verify_current())
            writer_conn = sqlite3.connect(sqlite_path)
            writer_conn.execute(
                "UPDATE source_messages SET text = 'rewritten' WHERE session_id = 'target'"
            )
            writer_conn.commit()
            writer_conn.close()
            self.assertFalse(reader.verify_current())

    def test_t3_stable_cache_skips_session_revision_rescan(self) -> None:
        self._add_revisioned_t3_session("target", "unchanged")
        _RevisionedTargetedSqliteAdapter.parse_calls = 0
        _RevisionedTargetedSqliteAdapter.revision_calls = 0
        _RevisionedTargetedSqliteAdapter.parse_with_hash_calls = 0
        reader = CachedSourceTranscriptReader(max_entries=2)

        with patch.dict(
            source_reader._ADAPTERS,
            {Harness.T3CODE.value: _RevisionedTargetedSqliteAdapter},
        ):
            first = reader(self.conn, "t3code:target")
            second = reader(self.conn, "t3code:target")

        self.assertTrue(first.ready)
        self.assertTrue(second.ready)
        self.assertEqual(_RevisionedTargetedSqliteAdapter.parse_calls, 1)
        self.assertEqual(_RevisionedTargetedSqliteAdapter.parse_with_hash_calls, 1)
        self.assertEqual(_RevisionedTargetedSqliteAdapter.revision_calls, 2)

    def test_t3_revision_ignores_unrelated_thread_writes(self) -> None:
        sqlite_path = self._add_revisioned_t3_session("target", "unchanged")
        source = sqlite3.connect(sqlite_path)
        source.execute("INSERT INTO source_messages VALUES ('other', 'new', 1)")
        source.commit()
        source.close()
        _RevisionedTargetedSqliteAdapter.parse_calls = 0
        _RevisionedTargetedSqliteAdapter.revision_calls = 0
        reader = CachedSourceTranscriptReader(max_entries=2)

        with patch.dict(
            source_reader._ADAPTERS,
            {Harness.T3CODE.value: _RevisionedTargetedSqliteAdapter},
        ):
            first = reader(self.conn, "t3code:target")
            source = sqlite3.connect(sqlite_path)
            source.execute("UPDATE source_messages SET text = 'later', revision = 2 WHERE session_id = 'other'")
            source.commit()
            source.close()
            second = reader(self.conn, "t3code:target")
            third = reader(self.conn, "t3code:target")

        self.assertTrue(first.ready)
        self.assertTrue(second.ready)
        self.assertTrue(third.ready)
        self.assertEqual(_RevisionedTargetedSqliteAdapter.parse_calls, 1)
        self.assertEqual(_RevisionedTargetedSqliteAdapter.revision_calls, 3)

    def test_t3_cache_rejects_same_length_target_rewrite(self) -> None:
        sqlite_path = self._add_revisioned_t3_session("target", "unchanged")
        _RevisionedTargetedSqliteAdapter.parse_calls = 0
        reader = CachedSourceTranscriptReader(max_entries=2)

        with patch.dict(
            source_reader._ADAPTERS,
            {Harness.T3CODE.value: _RevisionedTargetedSqliteAdapter},
        ):
            self.assertTrue(reader(self.conn, "t3code:target").ready)
            self.assertTrue(reader.verify_current())
            source = sqlite3.connect(sqlite_path)
            source.execute(
                "UPDATE source_messages SET text = 'rewritten' WHERE session_id = 'target'"
            )
            source.commit()
            source.close()

            changed = reader(self.conn, "t3code:target")
            self.assertFalse(reader.verify_current())

        self.assertEqual(changed.status, "source_changed")
        self.assertEqual(changed.messages, [])

    def test_t3_commit_for_one_cached_thread_cannot_be_consumed_by_another(self) -> None:
        sqlite_path = self._add_revisioned_t3_session("target", "unchanged")
        artifact_id = self.conn.execute(
            "SELECT id FROM artifacts WHERE path = ?", (str(sqlite_path),)
        ).fetchone()["id"]
        source = sqlite3.connect(sqlite_path)
        source.execute("INSERT INTO source_messages VALUES ('other', 'other', 1)")
        source.commit()
        source.close()
        self.conn.execute(
            """
            INSERT INTO sessions (id, harness, external_id, artifact_id, transcript_storage)
            VALUES ('t3code:other', 't3code', 'other', ?, 'source_backed')
            """,
            (artifact_id,),
        )
        self.conn.execute(
            """
            INSERT INTO messages (id, session_id, seq, role, text, content_hash)
            VALUES ('t3code:other:m:1', 't3code:other', 1, 'user', '', ?)
            """,
            (content_hash_text("other"),),
        )
        self.conn.commit()
        reader = CachedSourceTranscriptReader(max_entries=2)

        with patch.dict(
            source_reader._ADAPTERS,
            {Harness.T3CODE.value: _RevisionedTargetedSqliteAdapter},
        ):
            self.assertTrue(reader(self.conn, "t3code:target").ready)
            self.assertTrue(reader(self.conn, "t3code:other").ready)
            source = sqlite3.connect(sqlite_path)
            source.execute(
                "UPDATE source_messages SET text = 'rewritten' WHERE session_id = 'target'"
            )
            source.commit()
            source.close()
            self.assertTrue(reader(self.conn, "t3code:other").ready)
            changed = reader(self.conn, "t3code:target")

        self.assertEqual(changed.status, "source_changed")
        self.assertFalse(reader.verify_current())

    def test_t3_cache_rechecks_global_known_thread_dependency(self) -> None:
        sqlite_path = self._add_revisioned_t3_session("target", "unchanged")
        _GlobalDependencyTargetedSqliteAdapter.parse_calls = 0
        _GlobalDependencyTargetedSqliteAdapter.revision_calls = 0
        reader = CachedSourceTranscriptReader(max_entries=2)

        with patch.dict(
            source_reader._ADAPTERS,
            {Harness.T3CODE.value: _GlobalDependencyTargetedSqliteAdapter},
        ):
            first = reader(self.conn, "t3code:target")
            source = sqlite3.connect(sqlite_path)
            source.execute(
                "INSERT INTO source_messages VALUES (?, 'provider task', 1)",
                (_GlobalDependencyTargetedSqliteAdapter.provider_id,),
            )
            source.commit()
            source.close()
            refreshed = reader(self.conn, "t3code:target")

        self.assertTrue(first.ready)
        self.assertTrue(refreshed.ready)
        self.assertNotEqual(first.source_hash, refreshed.source_hash)
        self.assertEqual(_GlobalDependencyTargetedSqliteAdapter.parse_calls, 2)

    def test_t3_replaced_artifact_fails_closed_even_with_reused_data_version(self) -> None:
        sqlite_path = self._add_revisioned_t3_session("target", "unchanged")
        reader = CachedSourceTranscriptReader(max_entries=2)

        with patch.dict(
            source_reader._ADAPTERS,
            {Harness.T3CODE.value: _RevisionedTargetedSqliteAdapter},
        ):
            self.assertTrue(reader(self.conn, "t3code:target").ready)
            replacement = Path(self._tmp.name) / "replacement.sqlite"
            source = sqlite3.connect(replacement)
            source.execute(
                "CREATE TABLE source_messages (session_id TEXT PRIMARY KEY, text TEXT, revision INTEGER)"
            )
            source.execute("INSERT INTO source_messages VALUES ('target', 'rewritten', 1)")
            source.commit()
            source.close()
            os.replace(replacement, sqlite_path)
            changed = reader(self.conn, "t3code:target")

        self.assertEqual(changed.status, "source_changed")
        self.assertFalse(reader.verify_current())

    def test_single_flight_shares_one_cold_t3_read(self) -> None:
        self._add_revisioned_t3_session("target", "unchanged")
        _TargetedSqliteAdapter.parse_calls = 0
        first_read = threading.Event()
        release_read = threading.Event()
        _TargetedSqliteAdapter.first_read = first_read
        _TargetedSqliteAdapter.release_read = release_read
        reader = CachedSourceTranscriptReader(max_entries=2)
        results: list[SourceReadResult] = []
        failures: list[BaseException] = []
        db_path = Path(self._tmp.name) / "agentlog.db"

        def read() -> None:
            conn = connect(db_path)
            try:
                results.append(reader(conn, "t3code:target"))
            except BaseException as exc:
                failures.append(exc)
            finally:
                conn.close()

        with patch.dict(
            source_reader._ADAPTERS,
            {Harness.T3CODE.value: _TargetedSqliteAdapter},
        ):
            first = threading.Thread(target=read)
            first.start()
            self.assertTrue(first_read.wait(timeout=2))
            second = threading.Thread(target=read)
            second.start()
            time.sleep(0.05)
            release_read.set()
            first.join(timeout=2)
            second.join(timeout=2)

        self.assertEqual(failures, [])
        self.assertEqual(len(results), 2)
        self.assertTrue(all(result.ready for result in results))
        self.assertEqual(_TargetedSqliteAdapter.parse_calls, 1)
        self.assertIsNot(results[0].messages, results[1].messages)

    def test_single_flight_shares_one_cold_hermes_artifact_read(self) -> None:
        sqlite_path = Path(self._tmp.name) / "shared-hermes.sqlite"
        source = sqlite3.connect(sqlite_path)
        source.execute("CREATE TABLE source_marker (id INTEGER PRIMARY KEY)")
        source.commit()
        source.close()
        artifact_id = self.conn.execute(
            """
            INSERT INTO artifacts
              (harness, path, size, mtime_ns, content_hash, parsed_offset,
               parser_version, transcript_storage)
            VALUES ('hermes', ?, ?, ?, 'shared', 0, 'source-test',
                    'legacy_materialized')
            RETURNING id
            """,
            (
                str(sqlite_path),
                sqlite_path.stat().st_size,
                sqlite_path.stat().st_mtime_ns,
            ),
        ).fetchone()["id"]
        for external_id, text in (
            ("first", "first shared source message"),
            ("second", "second shared source message"),
        ):
            session_id = f"hermes:{external_id}"
            self.conn.execute(
                """
                INSERT INTO sessions
                  (id, harness, external_id, artifact_id, transcript_storage)
                VALUES (?, 'hermes', ?, ?, 'source_backed')
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

        _BlockingMultiSessionHermesAdapter.parse_calls = 0
        first_read = threading.Event()
        release_read = threading.Event()
        _BlockingMultiSessionHermesAdapter.first_read = first_read
        _BlockingMultiSessionHermesAdapter.release_read = release_read
        reader = CachedSourceTranscriptReader(max_entries=2)
        results: dict[str, SourceReadResult] = {}
        failures: list[BaseException] = []
        db_path = Path(self._tmp.name) / "agentlog.db"

        def read(session_id: str) -> None:
            conn = connect(db_path)
            try:
                results[session_id] = reader(conn, session_id)
            except BaseException as exc:
                failures.append(exc)
            finally:
                conn.close()

        with patch.dict(
            source_reader._ADAPTERS,
            {Harness.HERMES.value: _BlockingMultiSessionHermesAdapter},
        ):
            first = threading.Thread(target=read, args=("hermes:first",))
            first.start()
            self.assertTrue(first_read.wait(timeout=2))
            second = threading.Thread(target=read, args=("hermes:second",))
            second.start()
            time.sleep(0.05)
            release_read.set()
            first.join(timeout=2)
            second.join(timeout=2)

        self.assertEqual(failures, [])
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(_BlockingMultiSessionHermesAdapter.parse_calls, 1)
        self.assertTrue(results["hermes:first"].ready)
        self.assertTrue(results["hermes:second"].ready)
        self.assertEqual(
            [message["text"] for message in results["hermes:first"].messages],
            ["first shared source message"],
        )
        self.assertEqual(
            [message["text"] for message in results["hermes:second"].messages],
            ["second shared source message"],
        )
        self.assertIsNot(
            results["hermes:first"].messages,
            results["hermes:second"].messages,
        )

    def test_operation_cache_parses_shared_sqlite_once_and_revalidates_it(self) -> None:
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
        self.assertEqual(fingerprint.call_count, 2)

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

    def test_cached_hermes_read_rejects_same_size_restored_mtime_rewrite(self) -> None:
        source_path = Path(self._tmp.name) / "cached-hermes.db"
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
        artifact_id = self.conn.execute(
            """
            INSERT INTO artifacts
              (harness, path, size, mtime_ns, content_hash, parsed_offset,
               parser_version, transcript_storage)
            VALUES ('hermes', ?, ?, ?, 'sqlite-fixture', 0, 'source-test',
                    'legacy_materialized')
            RETURNING id
            """,
            (str(source_path), source_path.stat().st_size, source_path.stat().st_mtime_ns),
        ).fetchone()["id"]
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
        reader = CachedSourceTranscriptReader(max_entries=2)
        self.assertTrue(reader(self.conn, session_id).ready)
        original_stat = source_path.stat()
        source = sqlite3.connect(source_path)
        source.execute(
            "UPDATE messages SET content = 'rewritten SQLite request' WHERE id = 'message-1'"
        )
        source.commit()
        source.close()
        self.assertEqual(source_path.stat().st_size, original_stat.st_size)
        os.utime(source_path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

        changed = reader(self.conn, session_id)

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

    def test_read_through_cache_hits_without_database_writes(self) -> None:
        reader = CachedSourceTranscriptReader(max_entries=2)
        before_changes = self.conn.total_changes
        with patch(
            "agentlog.source_reader._parse_current",
            wraps=source_reader._parse_current,
        ) as parse_current:
            first = reader(self.conn, self.session_id)
            second = reader(self.conn, self.session_id)

        self.assertTrue(first.ready)
        self.assertTrue(second.ready)
        self.assertEqual(parse_current.call_count, 1)
        self.assertEqual(reader.size, 1)
        self.assertEqual(self.conn.total_changes, before_changes)

    def test_read_through_cache_evicts_least_recent_session(self) -> None:
        other_path = Path(self._tmp.name) / "other.jsonl"
        other_data = _line("user", "other request")
        other_path.write_bytes(other_data)
        other_id = "codex:other"
        self.conn.execute(
            """
            INSERT INTO artifacts
              (harness, path, size, mtime_ns, content_hash, parsed_offset,
               parser_version, transcript_storage)
            VALUES ('codex', ?, ?, ?, ?, ?, 'source-test', 'legacy_materialized')
            """,
            (
                str(other_path),
                len(other_data),
                other_path.stat().st_mtime_ns,
                hash_prefix(other_path, len(other_data)),
                len(other_data),
            ),
        )
        artifact_id = self.conn.execute("SELECT MAX(id) AS id FROM artifacts").fetchone()["id"]
        self.conn.execute(
            """
            INSERT INTO sessions (id, harness, external_id, artifact_id, transcript_storage)
            VALUES (?, 'codex', 'other', ?, 'source_backed')
            """,
            (other_id, artifact_id),
        )
        self.conn.execute(
            """
            INSERT INTO messages (id, session_id, seq, role, text, content_hash)
            VALUES (?, ?, 1, 'user', '', ?)
            """,
            (f"{other_id}:m:1", other_id, content_hash_text("other request")),
        )
        self.conn.commit()

        reader = CachedSourceTranscriptReader(max_entries=1)
        with patch(
            "agentlog.source_reader._parse_current",
            wraps=source_reader._parse_current,
        ) as parse_current:
            self.assertTrue(reader(self.conn, self.session_id).ready)
            self.assertTrue(reader(self.conn, other_id).ready)
            self.assertTrue(reader(self.conn, self.session_id).ready)

        self.assertEqual(parse_current.call_count, 3)
        self.assertEqual(reader.size, 1)

    def test_cache_refreshes_when_source_appends(self) -> None:
        reader = CachedSourceTranscriptReader(max_entries=2)
        self.assertTrue(reader(self.conn, self.session_id).ready)
        self.path.write_bytes(self.initial + _line("user", "new cached turn"))

        refreshed = reader(self.conn, self.session_id)

        self.assertTrue(refreshed.ready)
        self.assertEqual(refreshed.messages[-1]["text"], "new cached turn")

    def test_cache_hit_revalidates_same_size_rewritten_appended_tail(self) -> None:
        reader = CachedSourceTranscriptReader(max_entries=2)
        original = self.initial + _line("user", "new cached turn")
        self.path.write_bytes(original)
        self.assertTrue(reader(self.conn, self.session_id).ready)
        original_stat = self.path.stat()
        rewritten = self.initial + _line("user", "old cached turn")
        self.assertEqual(len(rewritten), len(original))
        self.path.write_bytes(rewritten)
        os.utime(
            self.path,
            ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
        )

        refreshed = reader(self.conn, self.session_id)

        self.assertTrue(refreshed.ready)
        self.assertEqual(refreshed.messages[-1]["text"], "old cached turn")

    def test_cache_fails_closed_when_checkpointed_source_is_rewritten(self) -> None:
        reader = CachedSourceTranscriptReader(max_entries=2)
        self.assertTrue(reader(self.conn, self.session_id).ready)
        self.path.write_bytes(_line("user", "rewritten history"))

        changed = reader(self.conn, self.session_id)

        self.assertEqual(changed.status, "source_changed")
        self.assertEqual(changed.messages, [])

    def test_oversize_transcript_is_served_but_not_cached(self) -> None:
        reader = CachedSourceTranscriptReader(max_entries=2, max_text_bytes=8)
        with patch(
            "agentlog.source_reader._parse_current",
            wraps=source_reader._parse_current,
        ) as parse_current:
            first = reader(self.conn, self.session_id)
            second = reader(self.conn, self.session_id)

        self.assertTrue(first.ready)
        self.assertTrue(second.ready)
        self.assertEqual(parse_current.call_count, 2)
        self.assertEqual(reader.size, 0)
        self.assertEqual(reader.text_bytes, 0)

    def test_verify_current_tracks_oversize_body_outside_cache(self) -> None:
        reader = CachedSourceTranscriptReader(max_entries=2, max_text_bytes=8)
        self.assertTrue(reader(self.conn, self.session_id).ready)
        self.assertEqual(reader.size, 0)
        self.assertTrue(reader.verify_current())

        self.path.write_bytes(_line("user", "rewritten history"))

        self.assertFalse(reader.verify_current())

    def test_verify_current_tracks_an_lru_evicted_source(self) -> None:
        other_path = Path(self._tmp.name) / "verification-other.jsonl"
        other_data = _line("user", "other request")
        other_path.write_bytes(other_data)
        other_id = "codex:verification-other"
        self.conn.execute(
            """
            INSERT INTO artifacts
              (harness, path, size, mtime_ns, content_hash, parsed_offset,
               parser_version, transcript_storage)
            VALUES ('codex', ?, ?, ?, ?, ?, 'source-test', 'legacy_materialized')
            """,
            (
                str(other_path),
                len(other_data),
                other_path.stat().st_mtime_ns,
                hash_prefix(other_path, len(other_data)),
                len(other_data),
            ),
        )
        artifact_id = self.conn.execute(
            "SELECT MAX(id) AS id FROM artifacts"
        ).fetchone()["id"]
        self.conn.execute(
            """
            INSERT INTO sessions (id, harness, external_id, artifact_id, transcript_storage)
            VALUES (?, 'codex', 'verification-other', ?, 'source_backed')
            """,
            (other_id, artifact_id),
        )
        self.conn.execute(
            """
            INSERT INTO messages (id, session_id, seq, role, text, content_hash)
            VALUES (?, ?, 1, 'user', '', ?)
            """,
            (f"{other_id}:m:1", other_id, content_hash_text("other request")),
        )
        self.conn.commit()
        reader = CachedSourceTranscriptReader(max_entries=1)
        self.assertTrue(reader(self.conn, self.session_id).ready)
        self.assertTrue(reader(self.conn, other_id).ready)
        self.assertEqual(reader.size, 1)
        self.assertTrue(reader.verify_current())

        self.path.write_bytes(_line("user", "rewritten history"))

        self.assertFalse(reader.verify_current())

    def test_byte_budget_evicts_oldest_transcript(self) -> None:
        other_path = Path(self._tmp.name) / "budget-other.jsonl"
        other_data = _line("user", "other request")
        other_path.write_bytes(other_data)
        other_id = "codex:budget-other"
        self.conn.execute(
            """
            INSERT INTO artifacts
              (harness, path, size, mtime_ns, content_hash, parsed_offset,
               parser_version, transcript_storage)
            VALUES ('codex', ?, ?, ?, ?, ?, 'source-test', 'legacy_materialized')
            """,
            (str(other_path), len(other_data), other_path.stat().st_mtime_ns,
             hash_prefix(other_path, len(other_data)), len(other_data)),
        )
        artifact_id = self.conn.execute("SELECT MAX(id) AS id FROM artifacts").fetchone()["id"]
        self.conn.execute(
            """
            INSERT INTO sessions (id, harness, external_id, artifact_id, transcript_storage)
            VALUES (?, 'codex', 'budget-other', ?, 'source_backed')
            """,
            (other_id, artifact_id),
        )
        self.conn.execute(
            """
            INSERT INTO messages (id, session_id, seq, role, text, content_hash)
            VALUES (?, ?, 1, 'user', '', ?)
            """,
            (f"{other_id}:m:1", other_id, content_hash_text("other request")),
        )
        self.conn.commit()

        reader = CachedSourceTranscriptReader(max_entries=4, max_text_bytes=30)
        self.assertTrue(reader(self.conn, self.session_id).ready)
        self.assertTrue(reader(self.conn, other_id).ready)

        self.assertEqual(reader.size, 1)
        self.assertLessEqual(reader.text_bytes, 30)

    def test_prewarm_selects_recent_sessions_only(self) -> None:
        self.conn.execute(
            "UPDATE sessions SET started_at = ? WHERE id = ?",
            ("2026-08-11T12:00:00+00:00", self.session_id),
        )
        self.conn.execute(
            """
            INSERT INTO sessions (id, harness, external_id, started_at, transcript_storage)
            VALUES ('codex:old', 'codex', 'old', '2026-07-01T12:00:00+00:00',
                    'source_backed')
            """
        )
        self.conn.commit()

        reader = CachedSourceTranscriptReader(max_entries=2)
        warmed = reader.prewarm_recent(
            self.conn,
            now=datetime(2026, 8, 12, 12, tzinfo=timezone.utc),
        )

        self.assertEqual(warmed, [self.session_id])
        self.assertEqual(reader.size, 1)

    def test_app_uses_one_lifetime_reader_for_detail_requests(self) -> None:
        app = create_app(Path(self._tmp.name) / "agentlog.db", source_cache_size=2)
        reader = app.state.source_transcript_reader
        with patch(
            "agentlog.source_reader._parse_current",
            wraps=source_reader._parse_current,
        ) as parse_current:
            with TestClient(app) as client:
                detail = client.get(f"/api/sessions/{self.session_id}")
                repeated = client.get(f"/api/sessions/{self.session_id}")

        self.assertIs(app.state.source_transcript_reader, reader)
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(parse_current.call_count, 1)

    def test_board_metadata_routes_never_hydrate_source_transcripts(self) -> None:
        app = create_app(Path(self._tmp.name) / "agentlog.db")
        with patch.object(
            CachedSourceTranscriptReader,
            "__call__",
            side_effect=AssertionError("metadata route read transcript text"),
        ):
            with TestClient(app) as client:
                for path in ("/api/summary", "/api/models", "/api/sessions"):
                    response = client.get(path)
                    self.assertEqual(response.status_code, 200, path)


if __name__ == "__main__":
    unittest.main()
