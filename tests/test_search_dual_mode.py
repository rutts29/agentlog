from __future__ import annotations

import sqlite3
import json
import tempfile
import unittest
from pathlib import Path

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
