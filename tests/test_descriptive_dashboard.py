from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from agentlog.api.app import create_app
from agentlog.db.schema import connect, init_db


class DescriptiveDashboardTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "dash.db"
        conn = connect(self.path)
        init_db(conn)
        conn.execute(
            """
            INSERT INTO artifacts
            (harness, path, size, mtime_ns, content_hash, parsed_offset, parser_version)
            VALUES ('codex', '/tmp/parent.jsonl', 10, 1, 'h1', 0, '1')
            """
        )
        conn.execute(
            """
            INSERT INTO artifacts
            (harness, path, size, mtime_ns, content_hash, parsed_offset, parser_version)
            VALUES ('codex', '/tmp/child.jsonl', 10, 1, 'h2', 0, '1')
            """
        )
        arts = list(conn.execute("SELECT id, path FROM artifacts ORDER BY id"))
        parent_art, child_art = int(arts[0]["id"]), int(arts[1]["id"])
        conn.execute(
            """
            INSERT INTO sessions (
                id, harness, external_id, parent_session_id, artifact_id,
                started_at, ended_at, model, effort, branch, cwd
            ) VALUES (
                'codex:parent-1', 'codex', 'parent-1', NULL, ?,
                '2026-07-01T00:00:00+00:00', '2026-07-01T00:10:00+00:00',
                'gpt-5.5', 'high', 'main', '/tmp/Plugin'
            )
            """,
            (parent_art,),
        )
        conn.execute(
            """
            INSERT INTO sessions (
                id, harness, external_id, parent_session_id, artifact_id,
                started_at, ended_at, model, effort, branch, cwd
            ) VALUES (
                'codex:child-1', 'codex', 'child-1', 'parent-1', ?,
                '2026-07-01T00:01:00+00:00', '2026-07-01T00:05:00+00:00',
                'gpt-5.5', 'medium', 'main', '/tmp/Plugin'
            )
            """,
            (child_art,),
        )
        conn.execute(
            """
            INSERT INTO messages (id, session_id, seq, role, timestamp, text)
            VALUES
              ('m1', 'codex:parent-1', 1, 'user', '2026-07-01T00:00:01+00:00', 'please refactor the dashboard'),
              ('m2', 'codex:parent-1', 2, 'assistant', '2026-07-01T00:00:02+00:00', 'working on it'),
              ('m3', 'codex:child-1', 1, 'user', '2026-07-01T00:01:01+00:00', 'worker brief')
            """
        )
        conn.execute(
            """
            INSERT INTO tool_events
            (id, session_id, message_id, seq, tool_name, action, success)
            VALUES ('t1', 'codex:parent-1', 'm2', 1, 'Read', 'call', 1)
            """
        )
        conn.execute(
            """
            INSERT INTO exchange_windows
            (id, session_id, request_message_id, response_message_id,
             input_hash, content_hash)
            VALUES ('w1', 'codex:parent-1', 'm1', 'm2', 'h', 'w1')
            """
        )
        conn.execute(
            """
            INSERT INTO derivation_runs
            (id, kind, extractor_name, extractor_version, started_at, status)
            VALUES ('run1', 'det', 'det', '1', '2026-07-01T00:00:00+00:00', 'ok')
            """
        )
        conn.execute(
            """
            INSERT INTO window_det_classifications
            (id, window_id, run_id, turn_kinds_json, request_kind, route,
             extractor_name, extractor_version, created_at)
            VALUES
            ('d1', 'w1', 'run1', '[]', 'substantive', 'ux', 'det', '1',
             '2026-07-01T00:00:00+00:00')
            """
        )
        # Rebuild FTS content for the inserted messages.
        conn.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
        conn.commit()
        conn.close()
        self.client = TestClient(create_app(self.path))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_summary_exposes_ungated_counts(self) -> None:
        res = self.client.get("/api/summary", params={"range": "all"})
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["kpis"]["sessions"]["value"], 2)
        self.assertEqual(body["kpis"]["messages"]["value"], 3)
        self.assertEqual(body["kpis"]["tool_events"]["value"], 1)
        self.assertEqual(body["kpis"]["windows"]["value"], 1)

    def test_search_returns_snippet(self) -> None:
        res = self.client.get(
            "/api/search", params={"range": "all", "q": "refactor"}
        )
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertGreaterEqual(body["total"], 1)
        self.assertIn("refactor", body["items"][0]["snippet"].lower())

    def test_session_detail_includes_artifact_path(self) -> None:
        res = self.client.get("/api/sessions/codex:parent-1")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["session"]["artifact_path"], "/tmp/parent.jsonl")
        self.assertEqual(body["anatomy"]["message_count"], 2)
        self.assertEqual(len(body["children"]), 1)

    def test_orchestration_resolves_bare_parent_external_id(self) -> None:
        res = self.client.get("/api/orchestration", params={"range": "all"})
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["supervisor_roots"], 1)
        self.assertEqual(body["child_sessions"], 1)
        self.assertEqual(body["items"][0]["child_count"], 1)
        tree = self.client.get("/api/sessions/codex:parent-1/tree")
        self.assertEqual(tree.status_code, 200)
        self.assertEqual(len(tree.json()["tree"]["children"]), 1)

    def test_tools_and_request_kinds_ungated(self) -> None:
        tools = self.client.get("/api/tools", params={"range": "all"}).json()
        self.assertEqual(tools["total"], 1)
        self.assertEqual(tools["items"][0]["tool"], "Read")
        kinds = self.client.get(
            "/api/request-kinds", params={"range": "all"}
        ).json()
        self.assertEqual(kinds["total"], 1)
        self.assertEqual(kinds["items"][0]["request_kind"], "substantive")


if __name__ == "__main__":
    unittest.main()
