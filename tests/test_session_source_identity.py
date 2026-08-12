from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from agentlog.db.migrations.v028_session_source_identity import apply as apply_v028
from agentlog.db.repository import Repository
from agentlog.db.schema import connect, init_db
from agentlog.normalize.models import Harness, NormalizedSession, ParseResult
from agentlog.session_identity import build_identity_context, logical_projection


class SessionSourceIdentityMigrationTests(unittest.TestCase):
    def test_upgrade_preserves_sessions_and_adds_safe_defaults(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                harness TEXT NOT NULL,
                external_id TEXT NOT NULL
            );
            INSERT INTO sessions (id, harness, external_id)
            VALUES ('codex:legacy', 'codex', 'legacy');
            """
        )

        apply_v028(conn)

        row = conn.execute(
            "SELECT * FROM sessions WHERE id = 'codex:legacy'"
        ).fetchone()
        assert row is not None
        self.assertIsNone(row["originator"])
        self.assertIsNone(row["thread_source"])
        self.assertEqual(row["inherited_message_count"], 0)
        self.assertEqual(row["inherited_record_count"], 0)
        self.assertIsNone(row["fork_context_status"])
        self.assertIsNone(row["fork_context_boundary"])
        conn.close()


class SessionSourceIdentityRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self._tmp.name) / "identity.db")
        init_db(self.conn)
        self.repo = Repository(self.conn)
        self.artifact_id = self.repo.upsert_artifact(
            harness="codex",
            path="/tmp/t3-worker.jsonl",
            size=1,
            mtime_ns=1,
            content_hash="source",
            parsed_offset=1,
            parser_version="28",
            transcript_storage="source_backed",
        )

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def _result(self, *, inherited_messages: int) -> ParseResult:
        return ParseResult(
            session=NormalizedSession(
                harness=Harness.CODEX,
                external_id="worker",
                parent_session_id="parent",
                originator="t3code_desktop",
                thread_source="subagent",
            ),
            extras={
                "inherited_message_count": inherited_messages,
                "inherited_record_count": inherited_messages + 4,
                "fork_context_status": "trimmed",
                "fork_context_boundary": "turn-7",
            },
        )

    def test_save_and_merge_persist_source_and_fork_provenance(self) -> None:
        session_id = self.repo.save_parse_result(
            artifact_id=self.artifact_id,
            result=self._result(inherited_messages=7),
            append=False,
        )
        self.repo.save_parse_result(
            artifact_id=self.artifact_id,
            result=self._result(inherited_messages=9),
            append=True,
        )
        retained = self.conn.execute(
            "SELECT originator, thread_source FROM sessions "
            "WHERE id = 'codex:worker'"
        ).fetchone()
        assert retained is not None
        self.assertEqual(retained["originator"], "t3code_desktop")
        self.assertEqual(retained["thread_source"], "subagent")

        self.repo.save_parse_result(
            artifact_id=self.artifact_id,
            result=self._result(inherited_messages=3),
            append=False,
        )
        self.repo.save_parse_result(
            artifact_id=self.artifact_id,
            result=self._result(inherited_messages=2),
            append=True,
        )
        self.repo.save_parse_result(
            artifact_id=self.artifact_id,
            result=ParseResult(
                session=NormalizedSession(
                    harness=Harness.CODEX,
                    external_id="worker",
                    parent_session_id="parent",
                )
            ),
            append=True,
        )

        row = self.conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        assert row is not None
        self.assertEqual(row["originator"], "t3code_desktop")
        self.assertEqual(row["thread_source"], "subagent")
        self.assertEqual(row["inherited_message_count"], 2)
        self.assertEqual(row["inherited_record_count"], 6)
        self.assertEqual(row["fork_context_status"], "trimmed")
        self.assertEqual(row["fork_context_boundary"], "turn-7")

    def test_authoritative_reparse_can_clear_stale_source_identity(self) -> None:
        self.repo.save_parse_result(
            artifact_id=self.artifact_id,
            result=self._result(inherited_messages=1),
            append=False,
        )
        self.repo.save_parse_result(
            artifact_id=self.artifact_id,
            result=ParseResult(
                session=NormalizedSession(
                    harness=Harness.CODEX,
                    external_id="worker",
                    parent_session_id="parent",
                )
            ),
            append=False,
        )

        row = self.conn.execute(
            "SELECT originator, thread_source FROM sessions "
            "WHERE id = 'codex:worker'"
        ).fetchone()
        assert row is not None
        self.assertIsNone(row["originator"])
        self.assertIsNone(row["thread_source"])


class SessionSourceIdentityProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self._tmp.name) / "projection.db")
        init_db(self.conn)
        self.conn.executescript(
            """
            INSERT INTO sessions
              (id, harness, external_id, parent_session_id, originator,
               thread_source)
            VALUES
              ('codex:t3-root', 'codex', 't3-root', NULL,
               't3code_desktop', NULL),
              ('codex:t3-child', 'codex', 't3-child', 't3-root',
               NULL, 'subagent'),
              ('codex:native', 'codex', 'native', NULL,
               'codex_cli_rs', NULL),
              ('claude:collision', 'claude', 'collision', 't3-root',
               NULL, 'subagent'),
              ('cursor:qualified-collision', 'cursor', 'qualified-collision',
               'codex:t3-root', NULL, 'subagent');
            """
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_t3_origin_is_logical_identity_without_inventing_owner(self) -> None:
        context = build_identity_context(self.conn)
        for session_id in ("codex:t3-root", "codex:t3-child"):
            projection = logical_projection(
                self.conn, session_id, "codex", context=context
            )
            self.assertEqual(projection["logical_harness"], "t3code")
            self.assertEqual(projection["runtime_harness"], "codex")
            self.assertIsNone(projection["orchestrator_session_id"])

        native = logical_projection(
            self.conn, "codex:native", "codex", context=context
        )
        collision = logical_projection(
            self.conn, "claude:collision", "claude", context=context
        )
        qualified_collision = logical_projection(
            self.conn,
            "cursor:qualified-collision",
            "cursor",
            context=context,
        )
        self.assertEqual(native["logical_harness"], "codex")
        self.assertEqual(collision["logical_harness"], "claude")
        self.assertEqual(qualified_collision["logical_harness"], "cursor")

    def test_explicit_link_adds_owner_without_changing_runtime(self) -> None:
        self.conn.executescript(
            """
            INSERT INTO sessions (id, harness, external_id)
            VALUES ('t3code:logical-root', 't3code', 'logical-root');
            INSERT INTO session_links
              (source_session_id, target_session_id, link_type,
               target_harness, target_external_id, link_role,
               confidence, evidence_json)
            VALUES ('t3code:logical-root', 'codex:t3-root',
                    'provider_backing', 'codex', 't3-root', 'root',
                    'observed', '{}');
            """
        )
        self.conn.commit()

        context = build_identity_context(self.conn)
        for session_id in ("codex:t3-root", "codex:t3-child"):
            projection = logical_projection(
                self.conn, session_id, "codex", context=context
            )
            self.assertEqual(projection["logical_harness"], "t3code")
            self.assertEqual(projection["runtime_harness"], "codex")
            self.assertEqual(
                projection["orchestrator_session_id"], "t3code:logical-root"
            )
        qualified_collision = logical_projection(
            self.conn,
            "cursor:qualified-collision",
            "cursor",
            context=context,
        )
        self.assertEqual(qualified_collision["logical_harness"], "cursor")
        self.assertEqual(qualified_collision["runtime_harness"], "cursor")
        self.assertIsNone(
            qualified_collision["orchestrator_session_id"]
        )


if __name__ == "__main__":
    unittest.main()
