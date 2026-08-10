from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from agentlog.analysis.skills import (
    discover_skill_files,
    index_skills,
    list_skill_profiles,
    parse_skill_frontmatter,
    skill_aliases,
    skill_detail,
)
from agentlog.api.app import create_app
from agentlog.db.schema import connect, init_db


class FrontmatterTests(unittest.TestCase):
    def test_parse_name_and_folded_description(self) -> None:
        text = """---
name: create-rule
description: >-
  Create Cursor rules for persistent AI guidance. Use when you want
  to create a rule.
tools: Read
---
# Body
"""
        meta = parse_skill_frontmatter(text)
        self.assertEqual(meta["name"], "create-rule")
        self.assertIn("Create Cursor rules", meta["description"])
        self.assertIn("create a rule", meta["description"])


class IndexerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.db_path = self.root / "t.db"
        self.conn = connect(self.db_path)
        init_db(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def _write_skill(
        self, base: Path, rel: str, name: str, body: str = "body v1"
    ) -> Path:
        path = base / rel / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"---\nname: {name}\ndescription: test skill\n---\n# {name}\n\n{body}\n",
            encoding="utf-8",
        )
        return path

    def test_index_idempotent_and_version_on_change(self) -> None:
        cursor = self.root / "cursor-skills"
        agents = self.root / "agent-skills"
        self._write_skill(cursor, "create-rule", "create-rule", "v1")
        self._write_skill(agents, "learn", "source-command-learn", "learn-v1")
        # Should be skipped
        junk = cursor / "node_modules" / "pkg" / "SKILL.md"
        junk.parent.mkdir(parents=True)
        junk.write_text("---\nname: junk\n---\n", encoding="utf-8")

        roots = [("cursor", cursor), ("agents", agents)]
        found = discover_skill_files(roots)
        self.assertEqual(len(found), 2)

        s1 = index_skills(self.conn, roots, now="2026-08-09T10:00:00+00:00")
        self.assertEqual(s1.scanned, 2)
        self.assertEqual(s1.inserted, 2)
        self.assertEqual(s1.versions_added, 2)
        self.assertEqual(s1.unchanged, 0)

        s2 = index_skills(self.conn, roots, now="2026-08-09T11:00:00+00:00")
        self.assertEqual(s2.inserted, 0)
        self.assertEqual(s2.updated, 0)
        self.assertEqual(s2.unchanged, 2)
        self.assertEqual(s2.versions_added, 0)
        n_skills = self.conn.execute("SELECT COUNT(*) AS c FROM skills").fetchone()["c"]
        n_vers = self.conn.execute(
            "SELECT COUNT(*) AS c FROM skill_versions"
        ).fetchone()["c"]
        self.assertEqual(int(n_skills), 2)
        self.assertEqual(int(n_vers), 2)

        path = cursor / "create-rule" / "SKILL.md"
        path.write_text(
            "---\nname: create-rule\ndescription: changed\n---\n# create-rule\n\nv2\n",
            encoding="utf-8",
        )
        s3 = index_skills(self.conn, roots, now="2026-08-09T12:00:00+00:00")
        self.assertEqual(s3.updated, 1)
        self.assertEqual(s3.versions_added, 1)
        n_vers2 = self.conn.execute(
            "SELECT COUNT(*) AS c FROM skill_versions"
        ).fetchone()["c"]
        self.assertEqual(int(n_vers2), 3)

    def test_aliases_include_plugin_qualifier(self) -> None:
        path = Path(
            "/Users/x/.claude/plugins/cache/claude-plugins-official/"
            "superpowers/6.1.1/skills/writing-plans/SKILL.md"
        )
        aliases = skill_aliases("writing-plans", "claude-plugins", path)
        self.assertIn("writing-plans", aliases)
        self.assertIn("superpowers:writing-plans", aliases)


class ProfileJoinTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.db_path = self.root / "p.db"
        self.conn = connect(self.db_path)
        init_db(self.conn)
        self.conn.execute(
            """
            INSERT INTO artifacts
            (harness, path, size, mtime_ns, content_hash, parsed_offset, parser_version)
            VALUES ('claude', '/tmp/a.jsonl', 1, 1, 'h', 0, '1')
            """
        )
        self.art = int(self.conn.execute("SELECT id FROM artifacts").fetchone()["id"])
        skills_dir = self.root / "skills"
        path = skills_dir / "writing-plans" / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text(
            "---\nname: writing-plans\ndescription: plans\n---\n# writing-plans\n",
            encoding="utf-8",
        )
        # Fake claude-plugins path so aliases include superpowers:writing-plans
        plugin_path = (
            self.root
            / "cache"
            / "claude-plugins-official"
            / "superpowers"
            / "6.1.1"
            / "skills"
            / "writing-plans"
            / "SKILL.md"
        )
        plugin_path.parent.mkdir(parents=True)
        plugin_path.write_text(
            "---\nname: writing-plans\ndescription: plans\n---\n# writing-plans\n",
            encoding="utf-8",
        )
        index_skills(
            self.conn,
            [("claude-plugins", self.root / "cache")],
            now="2026-08-09T10:00:00+00:00",
        )

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def _session(self, sid: str, started: str, ended: str) -> None:
        self.conn.execute(
            """
            INSERT INTO sessions (
                id, harness, external_id, artifact_id, started_at, ended_at
            ) VALUES (?, 'claude', ?, ?, ?, ?)
            """,
            (sid, sid, self.art, started, ended),
        )

    def _messages(self, sid: str, n: int) -> None:
        for i in range(n):
            self.conn.execute(
                """
                INSERT INTO messages (id, session_id, seq, role, text)
                VALUES (?, ?, ?, 'user', 'hi')
                """,
                (f"{sid}:m{i}", sid, i),
            )

    def _window_and_ux(
        self, sid: str, wid: str, kinds: list[str], *, labeled: bool = True
    ) -> None:
        mid_u = f"{wid}:u"
        mid_a = f"{wid}:a"
        self.conn.execute(
            """
            INSERT INTO messages (id, session_id, seq, role, text)
            VALUES (?, ?, 100, 'user', 'u'), (?, ?, 101, 'assistant', 'a')
            """,
            (mid_u, sid, mid_a, sid),
        )
        self.conn.execute(
            """
            INSERT INTO exchange_windows
            (id, session_id, request_message_id, response_message_id,
             input_hash, content_hash)
            VALUES (?, ?, ?, ?, 'h', ?)
            """,
            (wid, sid, mid_u, mid_a, wid),
        )
        if not labeled:
            return
        self.conn.execute(
            """
            INSERT INTO derivation_runs
            (id, kind, extractor_name, extractor_version, started_at, status)
            VALUES (?, 'ux', 'ux_v1', '0.1.0', '2026-08-09T00:00:00+00:00', 'done')
            """,
            (f"run-{wid}",),
        )
        self.conn.execute(
            """
            INSERT INTO ux_observations (
                id, window_id, run_id, turn_kinds_json, flags_json, spans_json,
                confidence_json, abstain_reasons_json, novel_observations_json,
                extractor_name, extractor_version, model, prompt_hash, created_at
            ) VALUES (?, ?, ?, ?, '{}', '[]', '{}', '[]', '[]',
                      'ux_v1', '0.1.0', 'm', 'p', '2026-08-09T00:00:00+00:00')
            """,
            (f"ux-{wid}", wid, f"run-{wid}", json.dumps(kinds)),
        )

    def test_join_rates_and_insufficient_flag(self) -> None:
        # 3 sessions with skill — below default min 5 → insufficient_data
        for i in range(3):
            sid = f"s{i}"
            self._session(
                sid,
                f"2026-08-0{i+1}T10:00:00+00:00",
                f"2026-08-0{i+1}T11:00:00+00:00",
            )
            self._messages(sid, 4 + i)
            self.conn.execute(
                """
                INSERT INTO skill_exposures
                (id, session_id, message_id, skill_name, exposure_type)
                VALUES (?, ?, NULL, 'superpowers:writing-plans', 'invoked')
                """,
                (f"ex{i}", sid),
            )
            self.conn.execute(
                """
                INSERT INTO tool_events
                (id, session_id, message_id, seq, tool_name, action, success)
                VALUES (?, ?, NULL, 1, 'Bash', 'call', ?)
                """,
                (f"t{i}", sid, 0 if i == 0 else 1),
            )
        # Session 0: redirect; session 1: correction; session 2: unlabeled window only
        self._window_and_ux("s0", "w0", ["human_task", "redirect_or_brake"])
        self._window_and_ux("s1", "w1", ["correction"])
        self._window_and_ux("s2", "w2", [], labeled=False)
        self.conn.commit()

        payload = list_skill_profiles(self.conn, min_sessions=5)
        indexed = [i for i in payload["items"] if i["indexed"]]
        self.assertEqual(len(indexed), 1)
        item = indexed[0]
        self.assertEqual(item["name"], "writing-plans")
        self.assertIn("superpowers:writing-plans", item["matched_exposure_names"])
        profile = item["profile"]
        self.assertEqual(profile["session_count"], 3)
        self.assertEqual(profile["sessions_with_messages"], 3)
        self.assertTrue(profile["insufficient_data"])
        redirect = profile["outcomes"]["redirect_or_brake"]
        self.assertEqual(redirect["numerator"], 1)
        self.assertEqual(redirect["denominator"], 2)
        self.assertAlmostEqual(redirect["rate"], 0.5)
        self.assertIn("sessions where writing-plans was active showed rate", redirect["phrasing"])
        self.assertEqual(redirect["ux_coverage"]["windows_labeled"], 2)
        self.assertEqual(redirect["ux_coverage"]["windows_total"], 3)
        correction = profile["outcomes"]["correction"]
        self.assertEqual(correction["numerator"], 1)
        self.assertEqual(correction["denominator"], 2)
        tool = profile["outcomes"]["tool_failures"]
        self.assertEqual(tool["sessions_with_failure"], 1)
        self.assertEqual(tool["denominator"], 3)

        detail = skill_detail(self.conn, item["id"])
        assert detail is not None
        self.assertEqual(len(detail["versions"]), 1)
        self.assertEqual(len(detail["exposure_sessions"]), 3)

    def test_api_list_and_detail(self) -> None:
        self._session("api1", "2026-08-01T10:00:00+00:00", "2026-08-01T11:00:00+00:00")
        self.conn.execute(
            """
            INSERT INTO skill_exposures
            (id, session_id, message_id, skill_name, exposure_type)
            VALUES ('e1', 'api1', NULL, 'writing-plans', 'invoked')
            """
        )
        self.conn.commit()
        client = TestClient(create_app(self.db_path))
        r = client.get("/api/skills?range=all")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertGreaterEqual(body["indexed_count"], 1)
        self.assertTrue(any(i.get("name") == "writing-plans" for i in body["items"]))
        skill_id = next(i["id"] for i in body["items"] if i["id"])
        d = client.get(f"/api/skills/{skill_id}?range=all")
        self.assertEqual(d.status_code, 200)
        self.assertEqual(d.json()["name"], "writing-plans")
        self.assertIn("versions", d.json())
        missing = client.get("/api/skills/does-not-exist")
        self.assertEqual(missing.status_code, 404)


if __name__ == "__main__":
    unittest.main()
