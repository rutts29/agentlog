from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agentlog.db.schema import connect, init_db
from agentlog.mcp_server import tools
from agentlog.mcp_server.db import connect_readonly
from agentlog.mcp_server.server import TOOL_NAMES, create_server


class McpToolsFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "mcp.db"
        self.conn = connect(self.path)
        init_db(self.conn)
        self.conn.execute(
            """
            INSERT INTO artifacts
            (harness, path, size, mtime_ns, content_hash, parsed_offset, parser_version)
            VALUES ('codex', '/tmp/a.jsonl', 1, 1, 'h', 0, '1')
            """
        )
        self.art = int(
            self.conn.execute("SELECT id FROM artifacts").fetchone()["id"]
        )
        self.conn.execute(
            """
            INSERT INTO sessions (
                id, harness, external_id, artifact_id, started_at, ended_at,
                repo, cwd, model
            ) VALUES
              ('codex:s1', 'codex', 's1', ?, '2026-08-08T10:00:00+00:00',
               '2026-08-08T11:00:00+00:00', 'github.com/acme/plugin',
               '/tmp/plugin', 'gpt-5'),
              ('claude:s2', 'claude', 's2', ?, '2026-08-09T09:00:00+00:00',
               '2026-08-09T09:30:00+00:00', NULL, '/tmp/other', 'claude-opus')
            """,
            (self.art, self.art),
        )
        self.conn.execute(
            """
            INSERT INTO messages (id, session_id, seq, role, timestamp, text)
            VALUES
              ('m1', 'codex:s1', 1, 'user', '2026-08-08T10:00:00+00:00',
               'Please refactor the attention inbox'),
              ('m2', 'codex:s1', 2, 'assistant', '2026-08-08T10:05:00+00:00',
               'I will refactor the attention inbox module now.'),
              ('m3', 'claude:s2', 1, 'user', '2026-08-09T09:00:00+00:00',
               'List skill inventory coverage'),
              ('m4', 'claude:s2', 2, 'assistant', '2026-08-09T09:05:00+00:00',
               'Here is the skill inventory.')
            """
        )
        self.conn.execute(
            """
            INSERT INTO skills (
                id, name, source, source_path, description,
                current_content_hash, first_seen_at, last_seen_at, last_indexed_at
            ) VALUES (
                'sk1', 'create-rule', 'cursor', '/tmp/skills/create-rule/SKILL.md',
                'Create Cursor rules', 'abc', '2026-08-01T00:00:00+00:00',
                '2026-08-01T00:00:00+00:00', '2026-08-01T00:00:00+00:00'
            )
            """
        )
        self.conn.execute(
            """
            INSERT INTO skill_exposures (id, session_id, message_id, skill_name, exposure_type)
            VALUES ('se1', 'codex:s1', 'm1', 'create-rule', 'invoked')
            """
        )
        self.conn.execute(
            """
            INSERT INTO exchange_windows (
                id, session_id, request_message_id, response_message_id,
                input_hash, content_hash
            ) VALUES
              ('w1', 'codex:s1', 'm1', 'm2', 'h1', 'w1'),
              ('w2', 'claude:s2', 'm3', 'm4', 'h2', 'w2')
            """
        )
        self.conn.execute(
            """
            INSERT INTO derivation_runs (
                id, kind, extractor_name, extractor_version, started_at, status
            ) VALUES ('run1', 'ux', 'ux_extractor', '1', '2026-08-09T00:00:00+00:00', 'completed')
            """
        )
        self.conn.execute(
            """
            INSERT INTO ux_observations (
                id, window_id, run_id, turn_kinds_json, user_stance, agent_stance,
                prior_outcome, flags_json, spans_json, confidence_json,
                abstain_reasons_json, novel_observations_json, extractor_name,
                extractor_version, model, prompt_hash, created_at
            ) VALUES (
                'ux1', 'w1', 'run1', '["correction"]', 'correcting', 'acknowledging',
                'resolved', '[]', '[]', '{}', '[]', '[]', 'ux_extractor', '1',
                'test-model', 'ph', '2026-08-09T01:00:00+00:00'
            )
            """
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_search_sessions(self) -> None:
        result = tools.search_sessions(self.conn, "attention", limit=10)
        self.assertGreaterEqual(result["total"], 1)
        ids = {s["id"] for s in result["sessions"]}
        self.assertIn("codex:s1", ids)
        self.assertTrue(result["sessions"][0]["title"])

    def test_search_sessions_harness_filter(self) -> None:
        result = tools.search_sessions(
            self.conn, "inventory", harness="claude", limit=5
        )
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["sessions"][0]["harness"], "claude")

    def test_get_session_truncates(self) -> None:
        long = "x" * 500
        self.conn.execute(
            """
            UPDATE messages SET text = ? WHERE id = 'm2'
            """,
            (long,),
        )
        self.conn.commit()
        result = tools.get_session(
            self.conn, "codex:s1", include_messages=True, message_truncate=200
        )
        self.assertEqual(result["session"]["id"], "codex:s1")
        texts = [m["text"] for m in result["messages"]]
        self.assertTrue(any(len(t) <= 200 for t in texts))
        self.assertTrue(any(t.endswith("…") for t in texts))

    def test_usage_stats(self) -> None:
        by_harness = tools.usage_stats(self.conn, "harness")
        keys = {g["key"] for g in by_harness["groups"]}
        self.assertIn("codex", keys)
        self.assertIn("claude", keys)
        by_day = tools.usage_stats(self.conn, "day")
        self.assertGreaterEqual(by_day["total_sessions"], 2)
        by_model = tools.usage_stats(self.conn, "model", since="2026-08-09")
        self.assertEqual(by_model["total_sessions"], 1)

    def test_usage_stats_uses_logical_t3_sessions(self) -> None:
        self.conn.executescript(
            """
            INSERT INTO sessions (
                id, harness, external_id, started_at, ended_at, repo, model,
                model_canonical
            ) VALUES
              ('t3code:root', 't3code', 'root', '2026-08-10T10:00:00+00:00',
               '2026-08-10T11:00:00+00:00', '/tmp/plugin', 'grok-4.6', 'grok-4.6'),
              ('codex:backing', 'codex', 'backing', '2026-08-10T10:00:01+00:00',
               '2026-08-10T10:59:00+00:00', '/tmp/plugin', 'grok-4.6', 'grok-4.6');
            INSERT INTO session_links (
                source_session_id, target_session_id, link_type,
                target_harness, target_external_id, link_role
            ) VALUES ('t3code:root', 'codex:backing', 'provider_backing',
                      'codex', 'backing', 'root');
            INSERT INTO messages (id, session_id, seq, role, text)
            VALUES ('t3-u', 't3code:root', 1, 'user', 'owner stub'),
                   ('cx-u', 'codex:backing', 1, 'user', 'canonical request'),
                   ('cx-a', 'codex:backing', 2, 'assistant', 'canonical response');
            """
        )
        self.conn.commit()

        by_harness = tools.usage_stats(self.conn, "harness")
        groups = {group["key"]: group for group in by_harness["groups"]}
        self.assertEqual(by_harness["total_sessions"], 3)
        self.assertEqual(groups["t3code"]["session_count"], 1)
        self.assertEqual(groups["t3code"]["message_count"], 2)
        self.assertEqual(groups["codex"]["session_count"], 1)
        self.assertEqual(groups["codex"]["message_count"], 2)

    def test_search_and_get_session_project_backing_to_t3_owner(self) -> None:
        self.conn.executescript(
            """
            INSERT INTO sessions (
                id, harness, external_id, started_at, ended_at, repo, model,
                model_canonical
            ) VALUES
              ('t3code:root', 't3code', 'root', '2026-08-10T10:00:00+00:00',
               '2026-08-10T11:00:00+00:00', '/tmp/plugin', 'grok-4.6', 'grok-4.6'),
              ('codex:backing', 'codex', 'backing', '2026-08-10T10:00:01+00:00',
               '2026-08-10T10:59:00+00:00', '/tmp/plugin', 'grok-4.6', 'grok-4.6');
            INSERT INTO session_links (
                source_session_id, target_session_id, link_type,
                target_harness, target_external_id, link_role
            ) VALUES ('t3code:root', 'codex:backing', 'provider_backing',
                      'codex', 'backing', 'root');
            INSERT INTO messages (id, session_id, seq, role, text)
            VALUES ('cx-u', 'codex:backing', 1, 'user', 'unique canonical phrase'),
                   ('cx-a', 'codex:backing', 2, 'assistant', 'canonical response');
            """
        )
        self.conn.commit()

        result = tools.search_sessions(self.conn, "unique canonical phrase")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["sessions"][0]["id"], "t3code:root")
        self.assertEqual(result["sessions"][0]["transcript_session_id"], "codex:backing")

        detail = tools.get_session(self.conn, "codex:backing")
        self.assertEqual(detail["session"]["id"], "t3code:root")
        self.assertEqual(detail["session"]["logical_harness"], "t3code")
        self.assertEqual(detail["session"]["transcript_session_id"], "codex:backing")

    def test_attention_inbox(self) -> None:
        result = tools.attention_inbox(self.conn)
        self.assertIn("items", result)
        self.assertIn("count", result)
        self.assertIsInstance(result["items"], list)

    def test_skill_inventory(self) -> None:
        result = tools.skill_inventory(self.conn)
        self.assertGreaterEqual(result["indexed_count"], 1)
        names = {i["name"] for i in result["items"]}
        self.assertIn("create-rule", names)
        hit = next(i for i in result["items"] if i["name"] == "create-rule")
        self.assertGreaterEqual(hit["exposure_count"], 1)

    def test_agreement_and_extraction_status(self) -> None:
        result = tools.agreement_and_extraction_status(self.conn)
        self.assertEqual(result["ux_observations"], 1)
        self.assertEqual(result["windows_total"], 2)
        self.assertEqual(result["windows_with_observations"], 1)
        self.assertIn("correcting", result["label_distribution"]["user_stance"])
        self.assertIn("correction", result["label_distribution"]["turn_kinds"])

    def test_connect_readonly_rejects_writes(self) -> None:
        ro = connect_readonly(self.path)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                ro.execute(
                    "INSERT INTO sessions (id, harness, external_id) "
                    "VALUES ('x', 'codex', 'x')"
                )
                ro.commit()
        finally:
            ro.close()


class McpServerModuleTests(unittest.TestCase):
    def test_server_imports_and_lists_tools(self) -> None:
        server = create_server()
        listed = asyncio.run(server.list_tools())
        names = {t.name for t in listed}
        self.assertEqual(names, set(TOOL_NAMES))
        for tool in listed:
            ann = tool.annotations
            self.assertIsNotNone(ann)
            self.assertTrue(ann.read_only_hint)
