from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agentlog.db.migrations.v036_grok_bootstrap_only import apply as apply_v036
from agentlog.db.schema import connect, init_db
from agentlog.ingest.grok import GrokAdapter
from agentlog.session_identity import GROK_BOOTSTRAP_ONLY_THREAD_SOURCE


class GrokBootstrapOnlyMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self._tmp.name) / "agentlog.db")
        init_db(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def _add_source(
        self, session_id: str, *, bootstrap: bool, ancillary: bool = False
    ) -> int:
        root = Path(self._tmp.name) / "sources" / session_id.removeprefix("grok:")
        path = root / "chat_history.jsonl"
        root.mkdir(parents=True)
        rows = [
            {
                "type": "system",
                "content": "You are Grok 4.6 released by xAI. You are an interactive CLI tool.",
            },
            {
                "type": "user",
                "synthetic_reason": "system_reminder" if bootstrap else None,
                "content": "<system-reminder>The following skills are available for use:",
            },
        ]
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        (root / "summary.json").write_text(
            json.dumps(
                {
                    "agent_name": "grok-build-plan",
                    "num_messages": 0 if bootstrap else 1,
                    "num_chat_messages": 2 if bootstrap else 3,
                }
            ),
            encoding="utf-8",
        )
        if ancillary:
            compaction = root / "compaction_requests"
            compaction.mkdir()
            (compaction / "request.json").write_text("{}", encoding="utf-8")
        snapshot = GrokAdapter().capture_source(path)
        cursor = self.conn.execute(
            """
            INSERT INTO artifacts
              (harness, path, size, mtime_ns, content_hash, parsed_offset,
               parser_version, transcript_storage)
            VALUES ('grok', ?, ?, ?, ?, ?, '16', 'source_backed')
            """,
            (
                str(path),
                snapshot.revision[0],
                snapshot.revision[1],
                snapshot.content_hash,
                path.stat().st_size,
            ),
        )
        return int(cursor.lastrowid)

    def _add_session(
        self,
        session_id: str,
        *,
        message_count: int = 2,
        bootstrap_source: bool = True,
        ancillary_source: bool = False,
    ) -> None:
        artifact_id = self._add_source(
            session_id, bootstrap=bootstrap_source, ancillary=ancillary_source
        )
        self.conn.execute(
            """
            INSERT INTO sessions
              (id, harness, external_id, artifact_id, agent_profile, transcript_storage)
            VALUES (?, 'grok', ?, ?, 'grok-build-plan', 'source_backed')
            """,
            (session_id, session_id.removeprefix("grok:"), artifact_id),
        )
        rows = [
            (f"{session_id}:m:1", session_id, 1, "system"),
            (f"{session_id}:m:2", session_id, 2, "user"),
        ]
        if message_count > 2:
            rows.extend(
                (f"{session_id}:m:{seq}", session_id, seq, "assistant")
                for seq in range(3, message_count + 1)
            )
        self.conn.executemany(
            """
            INSERT INTO messages
              (id, session_id, seq, role, text, content_hash,
               is_tool_plumbing, authored_by_agent)
            VALUES (?, ?, ?, ?, '', ?, 1, 1)
            """,
            [(*row, f"hash-{row[2]}") for row in rows],
        )

    def test_clears_verified_bootstrap_shape_and_is_idempotent(self) -> None:
        self._add_session("grok:bootstrap")
        self._add_session("grok:db-near-miss", bootstrap_source=False)
        self._add_session("grok:ancillary", ancillary_source=True)
        self._add_session("grok:real", message_count=8)
        self.conn.commit()

        apply_v036(self.conn)
        apply_v036(self.conn)
        self.conn.commit()

        bootstrap = self.conn.execute(
            "SELECT thread_source FROM sessions WHERE id = 'grok:bootstrap'"
        ).fetchone()
        real = self.conn.execute(
            "SELECT thread_source FROM sessions WHERE id = 'grok:real'"
        ).fetchone()
        near_miss = self.conn.execute(
            "SELECT thread_source FROM sessions WHERE id = 'grok:db-near-miss'"
        ).fetchone()
        ancillary = self.conn.execute(
            "SELECT thread_source FROM sessions WHERE id = 'grok:ancillary'"
        ).fetchone()
        assert bootstrap is not None
        assert real is not None
        assert near_miss is not None
        assert ancillary is not None
        self.assertEqual(bootstrap["thread_source"], GROK_BOOTSTRAP_ONLY_THREAD_SOURCE)
        self.assertEqual(real["thread_source"], None)
        self.assertEqual(near_miss["thread_source"], None)
        self.assertEqual(ancillary["thread_source"], None)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM messages WHERE session_id = 'grok:bootstrap'"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM messages WHERE session_id = 'grok:real'"
            ).fetchone()[0],
            8,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM messages WHERE session_id = 'grok:db-near-miss'"
            ).fetchone()[0],
            2,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM messages WHERE session_id = 'grok:ancillary'"
            ).fetchone()[0],
            2,
        )
        self.assertEqual(self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_preserves_bootstrap_shape_with_relationship_or_cluster(self) -> None:
        self._add_session("grok:linked")
        self._add_session("grok:parented")
        self._add_session("grok:clustered")
        self.conn.execute(
            """
            INSERT INTO sessions (id, harness, external_id, parent_session_id)
            VALUES ('grok:child', 'grok', 'child', 'parented')
            """
        )
        self.conn.execute(
            """
            INSERT INTO session_links
              (source_session_id, link_type, target_harness, target_external_id)
            VALUES ('grok:linked', 'provider_backing', 'grok', 'other')
            """
        )
        self.conn.execute(
            """
            INSERT INTO task_clusters
              (id, root_session_id, cluster_kind)
            VALUES ('clustered-root', 'grok:clustered', 'root')
            """
        )
        self.conn.commit()

        apply_v036(self.conn)
        self.conn.commit()

        for session_id in ("grok:linked", "grok:parented", "grok:clustered"):
            with self.subTest(session_id=session_id):
                self.assertIsNone(
                    self.conn.execute(
                        "SELECT thread_source FROM sessions WHERE id = ?", (session_id,)
                    ).fetchone()["thread_source"]
                )
                self.assertEqual(
                    self.conn.execute(
                        "SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,)
                    ).fetchone()[0],
                    2,
                )

    def test_foreign_key_message_lookups_use_permanent_indexes(self) -> None:
        lookups = {
            "tool_events": ("message_id", "idx_tool_events_message"),
            "skill_exposures": ("message_id", "idx_skill_exposures_message"),
            "token_usage": ("message_id", "idx_token_usage_message"),
            "exchange_windows": ("request_message_id", "idx_exchange_windows_request"),
            "exchange_windows_response": (
                "response_message_id",
                "idx_exchange_windows_response",
            ),
            "task_clusters": (
                "segment_start_message_id",
                "idx_task_clusters_segment_start",
            ),
            "task_clusters_end": (
                "segment_end_message_id",
                "idx_task_clusters_segment_end",
            ),
        }
        for key, (column, index) in lookups.items():
            table = "exchange_windows" if key == "exchange_windows_response" else (
                "task_clusters" if key == "task_clusters_end" else key
            )
            with self.subTest(index=index):
                plan = self.conn.execute(
                    f"EXPLAIN QUERY PLAN SELECT 1 FROM {table} WHERE {column} = ?",
                    ("message",),
                ).fetchall()
                self.assertTrue(any(index in row[3] for row in plan), plan)

    def test_failure_preserves_the_callers_outer_transaction(self) -> None:
        self._add_session("grok:bootstrap")
        self.conn.execute("DROP INDEX idx_tool_events_message")
        self.conn.commit()
        self.conn.execute(
            "INSERT INTO sessions (id, harness, external_id) VALUES ('outer', 'grok', 'outer')"
        )

        with mock.patch(
            "agentlog.db.migrations.v036_grok_bootstrap_only.is_bootstrap_only_artifact",
            side_effect=RuntimeError("injected verification failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "injected verification failure"):
                apply_v036(self.conn)

        self.assertTrue(self.conn.in_transaction)
        self.assertIsNotNone(
            self.conn.execute("SELECT id FROM sessions WHERE id = 'outer'").fetchone()
        )
        self.assertIsNone(
            self.conn.execute(
                "SELECT name FROM sqlite_master WHERE name = 'idx_tool_events_message'"
            ).fetchone()
        )
        self.conn.rollback()
        self.assertIsNone(
            self.conn.execute("SELECT id FROM sessions WHERE id = 'outer'").fetchone()
        )
