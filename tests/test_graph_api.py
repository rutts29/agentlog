from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from agentlog.api.app import create_app
from agentlog.db.schema import connect, init_db
from agentlog.normalize.model_identity import backfill_model_identity


class GraphApiTests(unittest.TestCase):
    """Fixture: a supervisor with two children (one linked by bare
    external_id, one by harness:external_id) plus an unrelated session in a
    second repo."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "graph.db"
        conn = connect(self.path)
        init_db(conn)
        sessions = [
            # id, harness, external_id, parent_session_id, cwd
            ("codex:sup-1", "codex", "sup-1", None, "/tmp/alpha"),
            ("codex:kid-1", "codex", "kid-1", "sup-1", "/tmp/alpha"),
            ("codex:kid-2", "codex", "kid-2", "codex:sup-1", "/tmp/alpha"),
            ("claude:solo-1", "claude", "solo-1", None, "/tmp/beta"),
        ]
        for sid, harness, ext, parent, cwd in sessions:
            conn.execute(
                """
                INSERT INTO sessions
                    (id, harness, external_id, parent_session_id,
                     started_at, ended_at, cwd, model)
                VALUES (?, ?, ?, ?, '2026-07-01T00:00:00+00:00',
                        '2026-07-01T00:05:00+00:00', ?, 'm1')
                """,
                (sid, harness, ext, parent, cwd),
            )
        conn.execute(
            """
            INSERT INTO messages (id, session_id, seq, role, text, model, effort)
            VALUES
              ('g1', 'codex:sup-1', 1, 'user', 'hello', 'm-a', 'high'),
              ('g2', 'codex:sup-1', 2, 'assistant', 'hi', 'm-b', NULL),
              ('g3', 'codex:kid-1', 1, 'user', 'brief', 'm-a', 'medium')
            """
        )
        backfill_model_identity(conn)
        conn.commit()
        conn.close()
        self.client = TestClient(create_app(self.path))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _get(self) -> dict:
        res = self.client.get("/api/graph", params={"range": "all"})
        self.assertEqual(res.status_code, 200)
        return res.json()

    def test_nodes_sessions_and_repo_anchors(self) -> None:
        body = self._get()
        sessions = [n for n in body["nodes"] if n["kind"] == "session"]
        repos = [n for n in body["nodes"] if n["kind"] == "repo"]
        self.assertEqual(len(sessions), 4)
        self.assertEqual(
            {r["id"] for r in repos}, {"repo:alpha", "repo:beta"}
        )
        by_id = {n["id"]: n for n in sessions}
        self.assertEqual(by_id["codex:sup-1"]["messages"], 2)
        self.assertEqual(by_id["codex:sup-1"]["children"], 2)
        self.assertEqual(by_id["codex:sup-1"]["repo"], "alpha")
        self.assertEqual(by_id["claude:solo-1"]["children"], 0)
        # julianday arithmetic truncates through float; 300s reads as 299–300.
        self.assertAlmostEqual(
            by_id["codex:sup-1"]["duration_seconds"], 300, delta=1
        )

    def test_orchestration_edges_resolve_both_link_spellings(self) -> None:
        body = self._get()
        orch = [e for e in body["edges"] if e["kind"] == "orchestration"]
        self.assertEqual(
            {(e["source"], e["target"]) for e in orch},
            {("codex:sup-1", "codex:kid-1"), ("codex:sup-1", "codex:kid-2")},
        )
        for e in orch:
            self.assertEqual(e["harness"], "codex")
        by_id = {n["id"]: n for n in body["nodes"]}
        self.assertEqual(by_id["codex:kid-1"]["parent_id"], "codex:sup-1")
        self.assertEqual(by_id["codex:kid-2"]["parent_id"], "codex:sup-1")

    def test_membership_edges_link_every_session_to_its_repo(self) -> None:
        body = self._get()
        member = [e for e in body["edges"] if e["kind"] == "membership"]
        self.assertEqual(len(member), 4)
        targets = {e["target"] for e in member}
        self.assertEqual(targets, {"repo:alpha", "repo:beta"})

    def test_repo_composition_aggregates(self) -> None:
        body = self._get()
        repos = {n["id"]: n for n in body["nodes"] if n["kind"] == "repo"}
        alpha = repos["repo:alpha"]
        self.assertEqual(alpha["harnesses"], [{"harness": "codex", "sessions": 3}])
        self.assertEqual(
            alpha["models"],
            [
                {"model": "m-a", "messages": 2},
                {"model": "m-b", "messages": 1},
            ],
        )
        self.assertEqual(
            alpha["efforts"],
            [
                {"effort": "high", "messages": 1},
                {"effort": "medium", "messages": 1},
            ],
        )
        self.assertEqual(alpha["messages"], 3)
        self.assertEqual(alpha["tools"], 0)
        self.assertEqual(alpha["first_at"], "2026-07-01T00:00:00+00:00")
        self.assertEqual(alpha["last_at"], "2026-07-01T00:05:00+00:00")
        beta = repos["repo:beta"]
        self.assertEqual(
            beta["harnesses"], [{"harness": "claude", "sessions": 1}]
        )
        self.assertEqual(beta["models"], [])
        self.assertEqual(beta["efforts"], [])

    def test_harness_composition_uses_stable_order(self) -> None:
        """Multi-harness repos list harnesses in stable ring/lobe order."""
        conn = connect(self.path)
        conn.execute(
            """
            INSERT INTO sessions
                (id, harness, external_id, parent_session_id,
                 started_at, ended_at, cwd, model)
            VALUES
              ('cursor:a1', 'cursor', 'a1', NULL,
               '2026-07-01T00:00:00+00:00', '2026-07-01T00:01:00+00:00',
               '/tmp/alpha', 'm1'),
              ('claude:a2', 'claude', 'a2', NULL,
               '2026-07-01T00:00:00+00:00', '2026-07-01T00:01:00+00:00',
               '/tmp/alpha', 'm1')
            """
        )
        conn.commit()
        conn.close()
        body = self._get()
        alpha = next(n for n in body["nodes"] if n.get("id") == "repo:alpha")
        self.assertEqual(
            [h["harness"] for h in alpha["harnesses"]],
            ["claude", "codex", "cursor"],
        )

    def test_no_transcript_text_in_payload(self) -> None:
        body = self._get()
        blob = str(body).lower()
        self.assertNotIn("hello", blob)
        self.assertNotIn("brief", blob)

    def test_counts_and_truncation_flag(self) -> None:
        body = self._get()
        self.assertEqual(body["counts"]["sessions"], 4)
        self.assertEqual(body["counts"]["repos"], 2)
        self.assertEqual(body["counts"]["orchestration_edges"], 2)
        self.assertIsNone(body["truncated"])

    def test_range_filter_excludes_out_of_window_sessions(self) -> None:
        res = self.client.get(
            "/api/graph",
            params={
                "range": "custom",
                "start": "2026-06-01T00:00:00+00:00",
                "end": "2026-06-30T00:00:00+00:00",
            },
        )
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["counts"]["sessions"], 0)
        self.assertEqual(body["nodes"], [])


if __name__ == "__main__":
    unittest.main()
