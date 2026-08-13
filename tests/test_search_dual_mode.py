from __future__ import annotations

import sqlite3
import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest import mock

from agentlog.api import search as search_api
from agentlog.api.descriptive import search_messages
from agentlog.api.ranges import parse_range
from agentlog.db.schema import connect, init_db
from agentlog.db.repository import Repository
from agentlog.ingest.base import content_hash_text, hash_prefix
from agentlog.mcp_server import tools as mcp_tools
from agentlog.session_identity import INTERNAL_APPROVAL_GUARDIAN_THREAD_SOURCE


class DualSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = connect(":memory:")
        init_db(self.conn)
        self.conn.executescript(
            """
            INSERT INTO sessions
              (id, harness, external_id, started_at, model, model_canonical, repo,
               transcript_storage)
            VALUES
              ('codex:legacy', 'codex', 'legacy', '2026-08-10T00:00:00+00:00',
               'gpt-5.5', 'gpt-5.5', '/tmp/Plugin', 'legacy_materialized'),
              ('codex:source', 'codex', 'source', '2026-08-10T00:01:00+00:00',
               'gpt-5.5', 'gpt-5.5', '/tmp/Plugin', 'source_backed');
            INSERT INTO messages (id, session_id, seq, role, text, content_hash)
            VALUES
              ('legacy-m1', 'codex:legacy', 1, 'user', 'legacy materialized result', 'h1'),
              ('source-m1', 'codex:source', 1, 'user', 'source materialized leak', 'h2');
            INSERT INTO messages_fts(messages_fts) VALUES ('rebuild');
            """
        )
        self.conn.commit()
        self.tr = parse_range("all")

    def tearDown(self) -> None:
        self.conn.close()

    def test_source_sessions_are_never_read_from_message_fts(self) -> None:
        result = search_messages(self.conn, self.tr, q="leak")
        self.assertEqual(result["total"], 0)

    def test_internal_approval_guardian_is_hidden_from_search(self) -> None:
        self.conn.execute(
            """
            INSERT INTO sessions
              (id, harness, external_id, started_at, thread_source,
               transcript_storage)
            VALUES (?, 'codex', 'guardian', '2026-08-10T00:02:00+00:00',
                    ?, 'legacy_materialized')
            """,
            ("codex:guardian", INTERNAL_APPROVAL_GUARDIAN_THREAD_SOURCE),
        )
        self.conn.execute(
            """
            INSERT INTO messages (id, session_id, seq, role, text, content_hash)
            VALUES ('guardian-m1', 'codex:guardian', 1, 'user',
                    'approval assessment transcript', 'guardian-hash')
            """
        )
        self.conn.execute("INSERT INTO messages_fts(messages_fts) VALUES ('rebuild')")
        self.conn.commit()

        result = search_messages(self.conn, self.tr, q="assessment")

        self.assertEqual(result["total"], 0)

    def test_source_scan_rejects_stale_payload_when_status_is_not_ready(self) -> None:
        def reader(conn: sqlite3.Connection, session_id: str) -> dict:
            return {
                "status": "source_changed",
                "warning": "checkpoint mismatch",
                "messages": [
                    {
                        "id": "stale-locator",
                        "seq": 1,
                        "role": "user",
                        "text": "stale source payload",
                    }
                ],
            }

        result = search_messages(
            self.conn,
            self.tr,
            q="payload",
            source_reader=reader,
        )

        self.assertEqual(result["total"], 0)
        self.assertEqual(result["items"], [])
        self.assertEqual(result["source_warnings"], ["checkpoint mismatch"])

    def test_source_scan_has_provenance_and_deduplicates_locator(self) -> None:
        calls: list[str] = []

        def reader(conn: sqlite3.Connection, session_id: str) -> dict:
            calls.append(session_id)
            text = "prefix " * 100 + "source payload" + " suffix" * 100
            return {
                "status": "ready",
                "source_identity": "source-v1",
                "source_hash": "hash-v1",
                "messages": [
                    {"id": "source-locator-1", "seq": 1, "role": "user", "text": text},
                    {"id": "source-locator-1", "seq": 1, "role": "user", "text": text},
                ],
            }

        result = search_messages(
            self.conn,
            self.tr,
            q="payload",
            source_reader=reader,
        )
        self.assertEqual(calls, ["codex:source"])
        self.assertEqual(result["total"], 1)
        item = result["items"][0]
        self.assertEqual(item["provenance"]["mode"], "source_scan")
        self.assertEqual(item["provenance"]["source_identity"], "source-v1")
        self.assertEqual(item["message_locator"], "source-locator-1")
        self.assertLess(len(item["snippet"]), 500)
        self.assertIn("«payload»", item["snippet"])

    def test_legacy_fts_result_keeps_materialized_provenance(self) -> None:
        calls: list[str] = []

        def reader(conn: sqlite3.Connection, session_id: str) -> dict:
            calls.append(session_id)
            return {"status": "ready", "messages": []}

        result = search_messages(
            self.conn,
            self.tr,
            q="legacy",
            source_reader=reader,
        )
        self.assertEqual(calls, ["codex:source"])
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["provenance"]["mode"], "materialized_fts")

    def test_source_scan_respects_the_explicit_bound_and_reports_it(self) -> None:
        for index in range(40):
            session_id = f"codex:source-{index}"
            self.conn.execute(
                """
                INSERT INTO sessions
                  (id, harness, external_id, started_at, transcript_storage)
                VALUES (?, 'codex', ?, '2026-08-10T00:03:00+00:00', 'source_backed')
                """,
                (session_id, f"source-{index}"),
            )
        self.conn.commit()
        calls: list[str] = []

        def reader(conn: sqlite3.Connection, session_id: str) -> dict:
            calls.append(session_id)
            return {"status": "ready", "messages": []}

        result = search_messages(
            self.conn,
            self.tr,
            q="payload",
            source_reader=reader,
            source_scan_limit=40,
        )

        self.assertEqual(len(calls), 40)
        self.assertEqual(len(set(calls)), 40)
        self.assertTrue(result["truncated"])

    def test_jsonl_prefilter_skips_canonical_reads_without_the_query(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            matching = root / "matching.jsonl"
            missing = root / "missing.jsonl"
            matching.write_text('{"text":"needle payload"}\n', encoding="utf-8")
            missing.write_text('{"text":"unrelated payload"}\n', encoding="utf-8")
            for index, path in enumerate((matching, missing)):
                artifact_id = self.conn.execute(
                    """
                    INSERT INTO artifacts
                      (harness, path, size, mtime_ns, content_hash, parsed_offset,
                       parser_version, transcript_storage)
                    VALUES ('codex', ?, ?, ?, 'hash', 0, 'test', 'source_backed')
                    RETURNING id
                    """,
                    (str(path), path.stat().st_size, path.stat().st_mtime_ns),
                ).fetchone()["id"]
                self.conn.execute(
                    """
                    INSERT INTO sessions
                      (id, harness, external_id, artifact_id, started_at, repo, transcript_storage)
                    VALUES (?, 'codex', ?, ?, '2026-08-10T00:03:00+00:00',
                            '/tmp/prefilter', 'source_backed')
                    """,
                    (f"codex:prefilter-{index}", f"prefilter-{index}", artifact_id),
                )
            self.conn.commit()
            calls: list[str] = []

            def reader(conn: sqlite3.Connection, session_id: str) -> dict:
                calls.append(session_id)
                return {"status": "ready", "messages": []}

            search_messages(
                self.conn,
                self.tr,
                q="needle",
                project=["prefilter"],
                source_reader=reader,
            )

        self.assertEqual(calls, ["codex:prefilter-0"])

    def test_jsonl_prefilter_finds_a_long_token_split_across_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "split.jsonl"
            token = "needle-token-which-is-longer-than-twenty-four-bytes"
            path.write_bytes(b"x" * (1024 * 1024 - 8) + token.encode())

            result = search_api._jsonl_might_contain_tokens(
                path, [token], cancelled=None
            )

        self.assertTrue(result)

    def test_jsonl_prefilter_keeps_no_overlap_for_single_byte_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "single-byte.jsonl"
            path.write_bytes(b"x" * (1024 * 1024 * 3))
            reads: list[int] = []

            class CountingFile:
                def __init__(self, source) -> None:
                    self.source = source

                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    self.source.close()

                def read(self, size: int) -> bytes:
                    reads.append(size)
                    return self.source.read(size)

            original_open = Path.open
            with mock.patch.object(
                Path,
                "open",
                lambda target, mode: CountingFile(original_open(target, mode)),
            ):
                result = search_api._jsonl_might_contain_tokens(path, ["a"], cancelled=None)

        self.assertFalse(result)
        self.assertEqual(reads, [1024 * 1024] * 4)

    def test_jsonl_prefilter_fails_open_for_unicode_and_changed_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "unicode.jsonl"
            path.write_text('{"text":"unrelated"}\n', encoding="utf-8")
            self.assertTrue(
                search_api._jsonl_might_contain_tokens(path, ["café"], cancelled=None)
            )

        class ChangedPath:
            suffix = ".jsonl"

            def __init__(self) -> None:
                self._stats = iter((
                    SimpleNamespace(st_dev=1, st_ino=1, st_size=9, st_mtime_ns=1, st_ctime_ns=1),
                    SimpleNamespace(st_dev=1, st_ino=2, st_size=9, st_mtime_ns=1, st_ctime_ns=2),
                ))

            def stat(self):
                return next(self._stats)

            def open(self, _mode: str):
                return BytesIO(b"unrelated")

        self.assertTrue(
            search_api._jsonl_might_contain_tokens(ChangedPath(), ["needle"], cancelled=None)
        )

    def test_jsonl_prefilter_fails_open_for_json_escaped_punctuation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "escaped.jsonl"
            path.write_text(
                '{"text":"foo\\/bar foo\\u002bbar foo\\u002dbar"}\n', encoding="utf-8"
            )

            self.assertTrue(
                search_api._jsonl_might_contain_tokens(path, ["foo/bar"], cancelled=None)
            )
            self.assertTrue(
                search_api._jsonl_might_contain_tokens(path, ["foo+bar"], cancelled=None)
            )
            self.assertTrue(
                search_api._jsonl_might_contain_tokens(path, ["foo-bar"], cancelled=None)
            )

    def test_jsonl_prefilter_fails_open_for_json_escaped_alphanumeric(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "escaped-alphanumeric.jsonl"
            path.write_text('{"text":"\\u0061"}\n', encoding="utf-8")

            self.assertTrue(
                search_api._jsonl_might_contain_tokens(path, ["a"], cancelled=None)
            )

    def test_jsonl_prefilter_detects_unicode_escape_across_chunk_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "split-unicode-escape.jsonl"
            path.write_bytes(b"x" * (1024 * 1024 - 1) + b"\\u0061")

            self.assertTrue(
                search_api._jsonl_might_contain_tokens(path, ["a"], cancelled=None)
            )

    def test_cancelled_source_search_stops_before_the_next_source_read(self) -> None:
        self.conn.execute(
            """
            INSERT INTO sessions
              (id, harness, external_id, started_at, transcript_storage)
            VALUES ('codex:second-source', 'codex', 'second-source',
                    '2026-08-10T00:03:00+00:00', 'source_backed')
            """
        )
        self.conn.commit()
        cancelled = Event()
        calls: list[str] = []

        def reader(conn: sqlite3.Connection, session_id: str) -> dict:
            calls.append(session_id)
            cancelled.set()
            return {"status": "ready", "messages": []}

        result = search_messages(
            self.conn,
            self.tr,
            q="payload",
            source_reader=reader,
            cancelled=cancelled,
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(result, {"cancelled": True})

    def test_cancelled_source_search_discards_partial_hits_inside_a_session(self) -> None:
        cancelled = Event()

        class TriggerText:
            def __str__(self) -> str:
                cancelled.set()
                return "payload"

        class DangerousText:
            def __str__(self) -> str:
                raise AssertionError("search processed a message after cancellation")

        def reader(conn: sqlite3.Connection, session_id: str) -> dict:
            return {
                "status": "ready",
                "messages": [
                    {"id": "first", "seq": 1, "role": "user", "text": TriggerText()},
                    {"id": "second", "seq": 2, "role": "user", "text": DangerousText()},
                ],
            }

        result = search_messages(
            self.conn,
            self.tr,
            q="payload",
            source_reader=reader,
            cancelled=cancelled,
        )

        self.assertEqual(result, {"cancelled": True})

    def test_source_scan_orders_metadata_matches_newest_first(self) -> None:
        for external_id, started_at in (
            ("target-old", "2026-08-10T00:03:00+00:00"),
            ("target-new", "2026-08-10T00:05:00+00:00"),
            ("other-new", "2026-08-10T00:06:00+00:00"),
        ):
            self.conn.execute(
                """
                INSERT INTO sessions
                  (id, harness, external_id, started_at, repo, transcript_storage)
                VALUES (?, 'codex', ?, ?, '/tmp/order', 'source_backed')
                """,
                (f"codex:{external_id}", external_id, started_at),
            )
        self.conn.commit()
        calls: list[str] = []

        def reader(conn: sqlite3.Connection, session_id: str) -> dict:
            calls.append(session_id)
            return {"status": "ready", "messages": []}

        search_messages(
            self.conn,
            self.tr,
            q="target",
            project=["order"],
            source_reader=reader,
        )

        self.assertEqual(
            calls,
            ["codex:target-new", "codex:target-old", "codex:other-new"],
        )

    def test_default_reader_finds_current_source_without_fts_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            external_id = "019fbdec-7065-7470-bb1e-dfa6c0d38237"
            path = root / f"rollout-2026-08-11T10-00-00-{external_id}.jsonl"
            data = (
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "on demand payload"}],
                        },
                    }
                )
                + "\n"
            ).encode()
            path.write_bytes(data)
            self.conn.execute(
                """
                INSERT INTO artifacts
                  (harness, path, size, mtime_ns, content_hash, parsed_offset,
                   parser_version, transcript_storage)
                VALUES ('codex', ?, ?, ?, ?, ?, 'dual-search', 'source_backed')
                """,
                (str(path), len(data), path.stat().st_mtime_ns, hash_prefix(path, len(data)), len(data)),
            )
            artifact_id = self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            source_id = f"codex:{external_id}"
            self.conn.execute(
                """
                INSERT INTO sessions
                  (id, harness, external_id, artifact_id, started_at, transcript_storage)
                VALUES (?, 'codex', ?, ?, '2026-08-10T00:02:00+00:00', 'source_backed')
                """,
                (source_id, external_id, artifact_id),
            )
            self.conn.execute(
                """
                INSERT INTO messages (id, session_id, seq, role, text, content_hash)
                VALUES (?, ?, 1, 'user', '', ?)
                """,
                (f"{source_id}:m:1", source_id, content_hash_text("on demand payload")),
            )
            self.conn.commit()

            result = search_messages(self.conn, self.tr, q="payload")
            repository_result = Repository(self.conn).search_messages("payload", limit=10)
            mcp_result = mcp_tools.search_sessions(self.conn, "payload", limit=10)
            mcp_detail = mcp_tools.get_session(self.conn, source_id)

        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["session_id"], source_id)
        self.assertEqual(result["items"][0]["provenance"]["mode"], "source_scan")
        self.assertEqual(len(repository_result), 1)
        self.assertEqual(repository_result[0]["session_id"], source_id)
        self.assertEqual([item["id"] for item in mcp_result["sessions"]], [source_id])
        self.assertEqual(mcp_result["sessions"][0]["title"], "on demand payload")
        self.assertEqual(mcp_detail["messages"][0]["text"], "on demand payload")

    def test_mcp_search_collapses_provider_backing_to_logical_t3_session(self) -> None:
        conn = connect(":memory:")
        init_db(conn)
        conn.executescript(
            """
            INSERT INTO sessions
              (id, harness, external_id, started_at, transcript_storage)
            VALUES
              ('t3code:root', 't3code', 'root', '2026-08-10T00:00:00+00:00', 'source_backed'),
              ('codex:backing', 'codex', 'backing', '2026-08-10T00:00:01+00:00', 'legacy_materialized');
            INSERT INTO messages (id, session_id, seq, role, text, content_hash)
            VALUES ('backing-hit', 'codex:backing', 1, 'user', 'same logical payload', 'h');
            INSERT INTO session_links
              (source_session_id, target_session_id, link_type, target_harness,
               target_external_id, link_role, confidence, evidence_json)
            VALUES
              ('t3code:root', 'codex:backing', 'provider_backing', 'codex',
               'backing', 'root', 'observed', '{}');
            INSERT INTO messages_fts(messages_fts) VALUES ('rebuild');
            """
        )
        conn.commit()
        try:
            api_result = search_messages(conn, parse_range("all"), q="payload")
            self.assertEqual([item["session_id"] for item in api_result["items"]], ["t3code:root"])
            result = mcp_tools.search_sessions(conn, "payload", limit=10)
            self.assertEqual([item["id"] for item in result["sessions"]], ["t3code:root"])
        finally:
            conn.close()

    def test_source_backed_t3_root_reads_and_projects_source_backing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            external_id = "019fbdec-7065-7470-bb1e-dfa6c0d38237"
            path = root / f"rollout-2026-08-11T10-00-00-{external_id}.jsonl"
            text = "canonical backing payload"
            data = (
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": text}],
                        },
                    }
                )
                + "\n"
            ).encode()
            path.write_bytes(data)
            conn = connect(":memory:")
            init_db(conn)
            try:
                conn.execute(
                    """
                    INSERT INTO artifacts
                      (harness, path, size, mtime_ns, content_hash, parsed_offset,
                       parser_version, transcript_storage)
                    VALUES ('codex', ?, ?, ?, ?, ?, 'dual-t3', 'source_backed')
                    """,
                    (str(path), len(data), path.stat().st_mtime_ns, hash_prefix(path, len(data)), len(data)),
                )
                artifact_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                conn.executescript(
                    f"""
                    INSERT INTO sessions
                      (id, harness, external_id, started_at, transcript_storage)
                    VALUES
                      ('t3code:root', 't3code', 'root', '2026-08-10T00:00:00+00:00', 'legacy_materialized');
                    INSERT INTO sessions
                      (id, harness, external_id, artifact_id, started_at, transcript_storage)
                    VALUES
                      ('codex:backing', 'codex', '{external_id}', {artifact_id},
                       '2026-08-10T00:00:01+00:00', 'source_backed');
                    INSERT INTO messages (id, session_id, seq, role, text, content_hash)
                    VALUES ('codex:backing:m:1', 'codex:backing', 1, 'user', '', '{content_hash_text(text)}');
                    INSERT INTO session_links
                      (source_session_id, target_session_id, link_type, target_harness,
                       target_external_id, link_role, confidence, evidence_json)
                    VALUES
                      ('t3code:root', 'codex:backing', 'provider_backing', 'codex',
                       '{external_id}', 'root', 'observed', '{{}}');
                    """
                )
                conn.commit()
                result = search_messages(conn, parse_range("all"), q="payload")
                self.assertEqual(result["total"], 1)
                self.assertEqual(result["items"][0]["session_id"], "t3code:root")
                self.assertEqual(result["items"][0]["physical_session_id"], "codex:backing")
                mcp_search = mcp_tools.search_sessions(conn, "payload", limit=10)
                self.assertEqual([item["id"] for item in mcp_search["sessions"]], ["t3code:root"])
                detail = mcp_tools.get_session(conn, "t3code:root")
                self.assertEqual(detail["messages"][0]["text"], text)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
