from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from agentlog.analysis.briefs import (
    MARKDOWN_BUDGET,
    build_session_brief,
    infer_cross_harness_links,
    render_brief_markdown,
)
from agentlog.api.app import create_app
from agentlog.db.schema import connect, init_db


class SessionBriefTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "brief.db"
        self.conn = connect(self.path)
        init_db(self.conn)
        self.conn.execute(
            """
            INSERT INTO artifacts
            (harness, path, size, mtime_ns, content_hash, parsed_offset, parser_version)
            VALUES ('cursor', '/tmp/a.jsonl', 1, 1, 'h', 0, '1')
            """
        )
        self.art = int(
            self.conn.execute("SELECT id FROM artifacts").fetchone()["id"]
        )
        self.now = datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def _session(
        self,
        sid: str,
        *,
        harness: str,
        started: str,
        ended: str | None = None,
        parent: str | None = None,
        repo: str | None = None,
        branch: str | None = None,
        cwd: str | None = None,
        model: str | None = None,
        effort: str | None = None,
    ) -> None:
        external = sid.split(":", 1)[-1]
        self.conn.execute(
            """
            INSERT INTO sessions (
                id, harness, external_id, parent_session_id, artifact_id,
                started_at, ended_at, repo, branch, cwd, model, effort
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sid,
                harness,
                external,
                parent,
                self.art,
                started,
                ended,
                repo,
                branch,
                cwd,
                model,
                effort,
            ),
        )

    def _msg(
        self,
        mid: str,
        sid: str,
        seq: int,
        role: str,
        text: str,
        *,
        ts: str | None = None,
        model: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO messages
            (id, session_id, seq, role, timestamp, model, text)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (mid, sid, seq, role, ts, model, text),
        )

    def test_brief_with_children_attention_and_commits(self) -> None:
        parent = "cursor:proj/parent"
        child = "cursor:proj/child"
        self._session(
            parent,
            harness="cursor",
            started="2026-08-09T08:00:00+00:00",
            ended="2026-08-09T09:00:00+00:00",
            repo="Users-test-Plugin",
            branch="main",
            cwd="/Users/test/Plugin",
            model="claude-opus",
            effort="high",
        )
        self._session(
            child,
            harness="cursor",
            started="2026-08-09T08:30:00+00:00",
            ended="2026-08-09T08:45:00+00:00",
            parent=parent,
            repo="Users-test-Plugin",
            branch="main",
            model="composer",
        )
        self._msg(
            "p1",
            parent,
            1,
            "user",
            "Implement structured handoffs for agentlog",
            ts="2026-08-09T08:00:00+00:00",
        )
        self._msg(
            "p2",
            parent,
            2,
            "assistant",
            "Plan:\n- [ ] write briefs.py\n- [x] read schema\n\nReady to continue?",
            ts="2026-08-09T09:00:00+00:00",
        )
        self._msg(
            "c1",
            child,
            1,
            "user",
            "Explore parent/child linking",
            ts="2026-08-09T08:30:00+00:00",
        )
        self.conn.execute(
            """
            INSERT INTO tool_events
            (id, session_id, message_id, seq, tool_name, action, success)
            VALUES ('t1', ?, 'p2', 1, 'Shell', 'call', 1)
            """,
            (parent,),
        )
        self.conn.execute(
            """
            INSERT INTO skill_exposures
            (id, session_id, message_id, skill_name, exposure_type)
            VALUES ('sk1', ?, 'p1', 'using-superpowers', 'mention')
            """,
            (parent,),
        )
        # session_commits may exist after v009; insert if table present.
        has_commits = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_commits'"
        ).fetchone()
        if has_commits:
            self.conn.execute(
                """
                INSERT INTO session_commits
                (session_id, commit_sha, join_method, author_date, subject)
                VALUES (?, 'abcdef1234567890', 'explicit',
                        '2026-08-09T08:50:00+00:00', 'Add session briefs')
                """,
                (parent,),
            )
        self.conn.commit()

        brief = build_session_brief(self.conn, parent)
        assert brief is not None
        self.assertEqual(brief["session_id"], parent)
        self.assertEqual(brief["header"]["harness"], "cursor")
        self.assertIn("claude-opus", brief["header"]["models"])
        self.assertEqual(brief["header"]["effort"], "high")
        self.assertEqual(brief["work"]["message_count"], 2)
        self.assertEqual(brief["work"]["tool_event_count"], 1)
        self.assertIn("using-superpowers", brief["work"]["skills"])
        self.assertIn("structured handoffs", brief["work"]["first_human"])
        self.assertEqual(len(brief["orchestration"]["children"]), 1)
        self.assertEqual(brief["orchestration"]["children"][0]["id"], child)
        self.assertIn("Explore parent", brief["orchestration"]["children"][0]["description"])
        self.assertIsNone(brief["orchestration"]["parent"])
        self.assertTrue(brief["open_loops"]["unresolved_todos"])
        self.assertIn("write briefs.py", brief["open_loops"]["unresolved_todos"][0])
        if has_commits:
            self.assertEqual(brief["work"]["commits"][0]["sha"], "abcdef123456")

        md = render_brief_markdown(brief)
        self.assertIn("# Session brief:", md)
        self.assertIn("## Orchestration", md)
        self.assertIn(child, md)
        self.assertLessEqual(len(md.encode("utf-8")), MARKDOWN_BUDGET)

    def test_brief_without_children(self) -> None:
        sid = "codex:lonely"
        self._session(
            sid,
            harness="codex",
            started="2026-08-09T10:00:00+00:00",
            ended="2026-08-09T10:30:00+00:00",
            model="gpt-5",
            cwd="/tmp/proj",
        )
        self._msg("m1", sid, 1, "user", "Quick fix", ts="2026-08-09T10:00:00+00:00")
        self._msg(
            "m2",
            sid,
            2,
            "assistant",
            "Done with the patch.",
            ts="2026-08-09T10:30:00+00:00",
        )
        self.conn.commit()
        brief = build_session_brief(self.conn, sid)
        assert brief is not None
        self.assertEqual(brief["orchestration"]["children"], [])
        self.assertIsNone(brief["orchestration"]["parent"])
        self.assertEqual(brief["open_loops"]["unresolved_todos"], [])
        self.assertNotIn("commits", brief["work"])

    def test_parent_link_from_child(self) -> None:
        parent = "claude:root"
        child = "claude:worker"
        self._session(
            parent,
            harness="claude",
            started="2026-08-09T07:00:00+00:00",
            repo="-Users-test-ai-sec",
            branch="main",
        )
        self._session(
            child,
            harness="claude",
            started="2026-08-09T07:10:00+00:00",
            parent="root",  # bare external_id form
            repo="-Users-test-ai-sec",
            branch="main",
        )
        self._msg("u1", parent, 1, "user", "Supervise the audit")
        self._msg("u2", child, 1, "user", "Scan findings")
        self.conn.commit()
        brief = build_session_brief(self.conn, child)
        assert brief is not None
        parent_link = brief["orchestration"]["parent"]
        assert parent_link is not None
        self.assertEqual(parent_link["kind"], "recorded")
        self.assertEqual(parent_link["id"], parent)
        self.assertIn("Supervise", parent_link["description"])

    def test_recorded_lineage_rejects_foreign_qualified_and_bare_ids(self) -> None:
        self._session(
            "codex:root",
            harness="codex",
            started="2026-08-09T07:00:00+00:00",
        )
        self._msg("secret", "codex:root", 1, "user", "foreign secret text")
        for sid, parent in (
            ("cursor:qualified", "codex:root"),
            ("claude:bare", "root"),
        ):
            self._session(
                sid,
                harness=sid.split(":", 1)[0],
                started="2026-08-09T07:10:00+00:00",
                parent=parent,
            )
        self.conn.commit()

        for sid in ("cursor:qualified", "claude:bare"):
            brief = build_session_brief(self.conn, sid)
            assert brief is not None
            self.assertIsNone(brief["orchestration"]["parent"])
            self.assertNotIn("foreign secret text", render_brief_markdown(brief))
        root = build_session_brief(self.conn, "codex:root")
        assert root is not None
        self.assertEqual(root["orchestration"]["children"], [])

    def test_cross_harness_inference(self) -> None:
        cursor = "cursor:ai-sec/abc"
        codex = "codex:def456"
        self._session(
            cursor,
            harness="cursor",
            started="2026-08-09T08:00:00+00:00",
            ended="2026-08-09T09:00:00+00:00",
            repo="Users-ruttanshbhatelia-ai-sec",
            branch="main",
            cwd="/Users/ruttanshbhatelia/ai/sec",
        )
        self._session(
            codex,
            harness="codex",
            started="2026-08-09T09:30:00+00:00",
            ended="2026-08-09T10:00:00+00:00",
            repo=None,
            branch="main",
            cwd="/Users/ruttanshbhatelia/ai_sec",
        )
        self._msg("a", cursor, 1, "user", "cursor work")
        self._msg("b", codex, 1, "user", "codex continue")
        self.conn.commit()

        row = self.conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (cursor,)
        ).fetchone()
        links = infer_cross_harness_links(self.conn, row)
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0].session_id, codex)
        self.assertEqual(links[0].direction, "successor")
        self.assertEqual(links[0].evidence["method"], "repo_branch_time")
        self.assertTrue(links[0].evidence["shared_tokens"])

        brief = build_session_brief(self.conn, cursor)
        assert brief is not None
        self.assertEqual(brief["orchestration"]["inferred_links"], [])

        inferred_brief = build_session_brief(
            self.conn, cursor, include_inferred=True
        )
        assert inferred_brief is not None
        inferred = inferred_brief["orchestration"]["inferred_links"]
        self.assertEqual(len(inferred), 1)
        self.assertEqual(inferred[0]["kind"], "inferred")
        md = render_brief_markdown(inferred_brief)
        self.assertIn("inferred (cross-harness)", md)
        self.assertIn(codex, md)

    def test_inference_rejects_branch_mismatch(self) -> None:
        a = "cursor:x/1"
        b = "codex:y"
        self._session(
            a,
            harness="cursor",
            started="2026-08-09T08:00:00+00:00",
            ended="2026-08-09T09:00:00+00:00",
            repo="Users-test-Plugin",
            branch="main",
            cwd="/Users/test/Plugin",
        )
        self._session(
            b,
            harness="codex",
            started="2026-08-09T09:10:00+00:00",
            repo="Users-test-Plugin",
            branch="feature",
            cwd="/Users/test/Plugin",
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (a,)
        ).fetchone()
        self.assertEqual(infer_cross_harness_links(self.conn, row), [])

    def test_markdown_budget(self) -> None:
        sid = "codex:longtext"
        self._session(
            sid,
            harness="codex",
            started="2026-08-09T08:00:00+00:00",
            ended="2026-08-09T09:00:00+00:00",
        )
        blob = "word " * 800
        self._msg("u", sid, 1, "user", blob)
        self._msg("a", sid, 2, "assistant", blob)
        self.conn.commit()
        brief = build_session_brief(self.conn, sid)
        assert brief is not None
        md = render_brief_markdown(brief)
        self.assertLessEqual(len(md.encode("utf-8")), MARKDOWN_BUDGET)

    def test_api_json_and_markdown(self) -> None:
        sid = "codex:api1"
        self._session(
            sid,
            harness="codex",
            started="2026-08-09T08:00:00+00:00",
            ended="2026-08-09T09:00:00+00:00",
            model="gpt-5",
        )
        self._msg("u", sid, 1, "user", "hello api")
        self.conn.commit()
        self.conn.close()

        client = TestClient(create_app(self.path))
        res = client.get(f"/api/sessions/{sid}/brief")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["session_id"], sid)
        self.assertIn("header", body)
        self.assertIn("open_loops", body)

        md = client.get(f"/api/sessions/{sid}/brief.md")
        self.assertEqual(md.status_code, 200)
        self.assertIn("text/markdown", md.headers.get("content-type", ""))
        self.assertIn("# Session brief:", md.text)

        missing = client.get("/api/sessions/codex:missing/brief")
        self.assertEqual(missing.status_code, 404)

    def test_api_path_session_id(self) -> None:
        sid = "cursor:Users-test-Plugin/abc-def"
        self._session(
            sid,
            harness="cursor",
            started="2026-08-09T08:00:00+00:00",
            repo="Users-test-Plugin",
        )
        self._msg("u", sid, 1, "user", "path id")
        self.conn.commit()
        self.conn.close()

        client = TestClient(create_app(self.path))
        res = client.get(f"/api/sessions/{sid}/brief")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["session_id"], sid)
        md = client.get(f"/api/sessions/{sid}/brief.md")
        self.assertEqual(md.status_code, 200)


if __name__ == "__main__":
    unittest.main()
