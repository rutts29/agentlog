from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from agentlog.analysis.attention import (
    AttentionThresholds,
    attention_payload,
    derive_attention,
)
from agentlog.api.app import create_app
from agentlog.db.schema import connect, init_db


class AttentionDerivationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "att.db"
        self.presence_path = Path(self._tmp.name) / "presence.json"
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
        self.now = datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc)
        self._write_presence([])

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def _write_presence(self, sessions: list[dict]) -> None:
        payload = {
            "ts": self.now.isoformat(),
            "generation": 1,
            "active_seconds": 90.0,
            "sessions": sessions,
        }
        self.presence_path.write_text(json.dumps(payload), encoding="utf-8")

    def _session(
        self,
        sid: str,
        *,
        started: str,
        ended: str | None = None,
        repo: str | None = None,
        branch: str | None = None,
        harness: str = "codex",
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO sessions (
                id, harness, external_id, artifact_id,
                started_at, ended_at, repo, branch
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sid,
                harness,
                sid.split(":", 1)[-1],
                self.art,
                started,
                ended,
                repo,
                branch,
            ),
        )

    def _derive(self, **kwargs):
        kwargs.setdefault("now", self.now)
        kwargs.setdefault("presence_path", self.presence_path)
        return derive_attention(self.conn, **kwargs)

    def test_waiting_on_user_question(self) -> None:
        self._session(
            "codex:wait",
            started="2026-08-09T08:00:00+00:00",
            ended="2026-08-09T09:00:00+00:00",
            repo="demo/repo",
            branch="main",
        )
        self.conn.execute(
            """
            INSERT INTO messages (id, session_id, seq, role, timestamp, text)
            VALUES
              ('w1', 'codex:wait', 1, 'user', '2026-08-09T08:00:00+00:00', 'help'),
              ('w2', 'codex:wait', 2, 'assistant', '2026-08-09T09:00:00+00:00',
               'I can proceed two ways.\\n\\nWhich approach should I take?')
            """
        )
        self.conn.commit()
        items = self._derive()
        states = {(i.session_id, i.state) for i in items}
        self.assertIn(("codex:wait", "waiting_on_user"), states)
        wait = next(i for i in items if i.state == "waiting_on_user")
        self.assertEqual(wait.severity, "warn")
        self.assertEqual(wait.lane, "urgent")
        self.assertIn("Which approach should I take?", wait.reason)

    def test_t3_root_uses_backing_metrics_and_keeps_worker_visible(self) -> None:
        self._session(
            "t3code:root",
            started="2026-08-09T08:00:00+00:00",
            ended="2026-08-09T10:00:00+00:00",
            harness="t3code",
        )
        self._session(
            "codex:backing",
            started="2026-08-09T08:01:00+00:00",
            ended="2026-08-09T10:00:00+00:00",
        )
        self._session(
            "codex:worker",
            started="2026-08-09T08:30:00+00:00",
            ended="2026-08-09T10:00:00+00:00",
        )
        self.conn.executescript(
            """
            INSERT INTO session_links
              (source_session_id, target_session_id, link_type,
               target_harness, target_external_id, link_role, confidence, evidence_json)
            VALUES
              ('t3code:root', 'codex:backing', 'provider_backing',
               'codex', 'backing', 'root', 'observed', '{}'),
              ('t3code:root', 'codex:worker', 'provider_backing',
               'codex', 'worker', 'worker', 'observed', '{}');
            INSERT INTO messages (id, session_id, seq, role, timestamp, text)
            VALUES
              ('root-a', 't3code:root', 1, 'assistant',
               '2026-08-09T10:00:00+00:00', 'root projection'),
              ('backing-a', 'codex:backing', 1, 'assistant',
               '2026-08-09T10:00:00+00:00', 'Which backing answer should I use?'),
              ('worker-a', 'codex:worker', 1, 'assistant',
               '2026-08-09T10:00:00+00:00', 'Which worker answer should I use?');
            """
        )
        self.conn.commit()

        items = self._derive()
        by_id = {item.session_id: item for item in items}
        self.assertEqual(set(by_id), {"t3code:root", "codex:worker"})
        self.assertEqual(by_id["t3code:root"].harness, "t3code")
        self.assertEqual(by_id["t3code:root"].runtime_harness, "codex")
        self.assertIn("backing answer", by_id["t3code:root"].reason)
        self.assertEqual(by_id["codex:worker"].harness, "t3code")
        self.assertEqual(by_id["codex:worker"].runtime_harness, "codex")

    def test_multi_owner_backing_stays_physical_in_attention(self) -> None:
        for sid in ("t3code:one", "t3code:two"):
            self._session(
                sid,
                started="2026-08-09T08:00:00+00:00",
                ended="2026-08-09T10:00:00+00:00",
                harness="t3code",
            )
        self._session(
            "codex:shared",
            started="2026-08-09T08:01:00+00:00",
            ended="2026-08-09T10:00:00+00:00",
        )
        self.conn.executescript(
            """
            INSERT INTO session_links
              (source_session_id, target_session_id, link_type,
               target_harness, target_external_id, link_role, confidence, evidence_json)
            VALUES
              ('t3code:one', 'codex:shared', 'provider_backing',
               'codex', 'shared', 'root', 'observed', '{}'),
              ('t3code:two', 'codex:shared', 'provider_backing',
               'codex', 'shared', 'root', 'observed', '{}');
            INSERT INTO messages (id, session_id, seq, role, timestamp, text)
            VALUES ('shared-a', 'codex:shared', 1, 'assistant',
                    '2026-08-09T10:00:00+00:00', 'Which owner should I use?');
            """
        )
        self.conn.commit()

        items = self._derive()
        self.assertEqual([item.session_id for item in items], ["codex:shared"])
        self.assertEqual(items[0].harness, "codex")

    def test_open_task_incomplete_todo_within_horizon(self) -> None:
        self._session(
            "codex:todo",
            started="2026-08-08T10:00:00+00:00",
            ended="2026-08-08T11:00:00+00:00",
            repo="demo/repo",
        )
        todo_text = "Plan:\n- [ ] finish migrations\n- [x] draft tests"
        self.conn.execute(
            """
            INSERT INTO messages (id, session_id, seq, role, timestamp, text)
            VALUES
              ('s1', 'codex:todo', 1, 'user', '2026-08-08T10:00:00+00:00', 'go'),
              ('s2', 'codex:todo', 2, 'assistant', '2026-08-08T11:00:00+00:00', ?)
            """,
            (todo_text,),
        )
        self.conn.commit()
        items = self._derive()
        open_items = [i for i in items if i.state == "open_task"]
        self.assertEqual(len(open_items), 1)
        self.assertIn("incomplete todos", open_items[0].reason)

    def test_horizon_boundary_moves_to_resumable(self) -> None:
        # 60h idle → beyond 48h actionable horizon.
        self._session(
            "codex:old",
            started="2026-08-06T10:00:00+00:00",
            ended="2026-08-07T00:00:00+00:00",
            repo="demo/old",
        )
        self.conn.execute(
            """
            INSERT INTO messages (id, session_id, seq, role, timestamp, text)
            VALUES
              ('o1', 'codex:old', 1, 'assistant', '2026-08-07T00:00:00+00:00',
               'Shall I continue?')
            """
        )
        self.conn.commit()
        urgent = self._derive(include_resumable=False)
        self.assertFalse(any(i.session_id == "codex:old" for i in urgent))
        all_items = self._derive(include_resumable=True)
        dormant = [i for i in all_items if i.session_id == "codex:old"]
        self.assertEqual(len(dormant), 1)
        self.assertEqual(dormant[0].state, "resumable")
        self.assertEqual(dormant[0].lane, "resumable")
        self.assertIn("Not urgent", dormant[0].reason)

    def test_one_item_per_session_dedup(self) -> None:
        self._session(
            "codex:dup",
            started="2026-08-09T08:00:00+00:00",
            ended="2026-08-09T09:00:00+00:00",
            repo="demo/dup",
        )
        text = "Plan:\n- [ ] ship it\n\nWhich option?"
        self.conn.execute(
            """
            INSERT INTO messages (id, session_id, seq, role, timestamp, text)
            VALUES
              ('d1', 'codex:dup', 1, 'assistant', '2026-08-09T09:00:00+00:00', ?)
            """,
            (text,),
        )
        self.conn.commit()
        items, stats = self._derive(include_resumable=True, return_stats=True)
        matching = [i for i in items if i.session_id == "codex:dup"]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0].state, "waiting_on_user")
        self.assertGreaterEqual(stats.removed_by_dedup, 1)

    def test_empty_window_not_superseded_by_unrelated(self) -> None:
        self._session(
            "cursor:empty-window/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            started="2026-08-07T08:00:00+00:00",
            ended="2026-08-07T09:00:00+00:00",
            repo="empty-window",
            harness="cursor",
        )
        self.conn.execute(
            """
            INSERT INTO messages (id, session_id, seq, role, timestamp, text)
            VALUES
              ('ew1', 'cursor:empty-window/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
               1, 'assistant', '2026-08-07T09:00:00+00:00',
               'Still blocked — continue?')
            """
        )
        self._session(
            "cursor:empty-window/bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            started="2026-08-09T10:00:00+00:00",
            ended="2026-08-09T11:00:00+00:00",
            repo="empty-window",
            harness="cursor",
        )
        self.conn.execute(
            """
            INSERT INTO messages (id, session_id, seq, role, timestamp, text)
            VALUES
              ('ew2', 'cursor:empty-window/bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb',
               1, 'user', '2026-08-09T10:00:00+00:00', 'other work')
            """
        )
        self.conn.commit()
        items = self._derive(include_resumable=True)
        # 60h idle → resumable, not falsely cleared by another empty-window chat.
        hit = [
            i
            for i in items
            if i.session_id.endswith("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        ]
        self.assertEqual(len(hit), 1)
        self.assertEqual(hit[0].state, "resumable")

    def test_cursor_uuid_mirrors_dedup(self) -> None:
        uuid = "cccccccc-cccc-cccc-cccc-cccccccccccc"
        for repo in ("empty-window", "Users-demo-Plugin", "Users-demo-other"):
            sid = f"cursor:{repo}/{uuid}"
            self._session(
                sid,
                started="2026-08-09T06:00:00+00:00",
                ended=None,
                repo=repo,
                harness="cursor",
            )
            self.conn.execute(
                """
                INSERT INTO messages (id, session_id, seq, role, timestamp, text)
                VALUES (?, ?, 1, 'assistant', '2026-08-09T11:50:00+00:00', 'working')
                """,
                (f"m-{repo}", sid),
            )
        self._write_presence(
            [
                {
                    "harness": "cursor",
                    "external_id": f"Users-demo-Plugin/{uuid}",
                    "session_id": f"cursor:Users-demo-Plugin/{uuid}",
                    "state": "waiting",
                    "last_activity_at": "2026-08-09T11:59:00+00:00",
                    "age_seconds": 8.0,
                    "repo": "Users-demo-Plugin",
                }
            ]
        )
        self.conn.commit()
        items = self._derive()
        mirrors = [i for i in items if uuid in i.session_id]
        self.assertEqual(len(mirrors), 1)
        self.assertEqual(mirrors[0].state, "live_waiting")

    def test_supersession_by_later_session(self) -> None:
        self._session(
            "codex:oldq",
            started="2026-08-08T08:00:00+00:00",
            ended="2026-08-08T09:00:00+00:00",
            repo="demo/work",
            branch="main",
        )
        self.conn.execute(
            """
            INSERT INTO messages (id, session_id, seq, role, timestamp, text)
            VALUES
              ('q1', 'codex:oldq', 1, 'assistant', '2026-08-08T09:00:00+00:00',
               'Should I merge?')
            """
        )
        # Later session in same repo/branch continues the work.
        self._session(
            "codex:new",
            started="2026-08-09T10:00:00+00:00",
            ended="2026-08-09T11:00:00+00:00",
            repo="demo/work",
            branch="main",
        )
        self.conn.execute(
            """
            INSERT INTO messages (id, session_id, seq, role, timestamp, text)
            VALUES
              ('n1', 'codex:new', 1, 'user', '2026-08-09T10:00:00+00:00',
               'continue the merge')
            """
        )
        self.conn.commit()
        items, stats = self._derive(include_resumable=True, return_stats=True)
        self.assertFalse(any(i.session_id == "codex:oldq" for i in items))
        self.assertGreaterEqual(stats.removed_by_resolution, 1)

    def test_resolution_by_later_user_turn(self) -> None:
        self._session(
            "codex:answered",
            started="2026-08-09T08:00:00+00:00",
            ended="2026-08-09T10:00:00+00:00",
            repo="demo/ans",
        )
        self.conn.execute(
            """
            INSERT INTO messages (id, session_id, seq, role, timestamp, text)
            VALUES
              ('a1', 'codex:answered', 1, 'assistant',
               '2026-08-09T09:00:00+00:00', 'Ready to proceed?'),
              ('a2', 'codex:answered', 2, 'user',
               '2026-08-09T10:00:00+00:00', 'yes, go')
            """
        )
        self.conn.commit()
        items = self._derive(include_resumable=True)
        self.assertFalse(any(i.session_id == "codex:answered" for i in items))

    def test_error_streak_uses_known_outcomes_only(self) -> None:
        self._session(
            "codex:err",
            started="2026-08-09T10:00:00+00:00",
            ended="2026-08-09T11:00:00+00:00",
            repo="demo/err",
        )
        self.conn.execute(
            """
            INSERT INTO messages (id, session_id, seq, role, timestamp, text)
            VALUES ('e1', 'codex:err', 1, 'assistant',
                    '2026-08-09T11:00:00+00:00', 'retrying')
            """
        )
        # Trailing NULL success must not break the known-outcome streak.
        events = [
            (1, "Shell", None),
            (2, "Shell", 0),
            (3, "Read", None),
            (4, "Shell", 0),
            (5, "Write", 0),
        ]
        for seq, name, success in events:
            self.conn.execute(
                """
                INSERT INTO tool_events
                (id, session_id, message_id, seq, tool_name, action, success)
                VALUES (?, 'codex:err', 'e1', ?, ?, 'result', ?)
                """,
                (f"t{seq}", seq, name, success),
            )
        self.conn.commit()
        items = self._derive()
        errors = [i for i in items if i.state == "error_streak"]
        self.assertEqual(len(errors), 1)
        self.assertIn("consecutive failed tool results", errors[0].reason)

    def test_null_success_alone_does_not_fire_error_streak(self) -> None:
        self._session(
            "codex:nulls",
            started="2026-08-09T10:00:00+00:00",
            ended="2026-08-09T11:00:00+00:00",
        )
        self.conn.execute(
            """
            INSERT INTO messages (id, session_id, seq, role, timestamp, text)
            VALUES ('n1', 'codex:nulls', 1, 'assistant',
                    '2026-08-09T11:00:00+00:00', 'working')
            """
        )
        for i in range(1, 6):
            self.conn.execute(
                """
                INSERT INTO tool_events
                (id, session_id, message_id, seq, tool_name, action, success)
                VALUES (?, 'codex:nulls', 'n1', ?, 'Shell', 'call', NULL)
                """,
                (f"tn{i}", i),
            )
        self.conn.commit()
        items = self._derive()
        self.assertFalse(any(i.state == "error_streak" for i in items))

    def test_live_waiting_outranks_historical(self) -> None:
        self._session(
            "cursor:live1",
            started="2026-08-09T10:00:00+00:00",
            ended=None,
            repo="demo/live",
            harness="cursor",
        )
        self.conn.execute(
            """
            INSERT INTO messages (id, session_id, seq, role, timestamp, text)
            VALUES ('l1', 'cursor:live1', 1, 'assistant',
                    '2026-08-09T11:50:00+00:00', 'Which file next?')
            """
        )
        self._write_presence(
            [
                {
                    "harness": "cursor",
                    "external_id": "live1",
                    "session_id": "cursor:live1",
                    "state": "waiting",
                    "last_activity_at": "2026-08-09T11:59:00+00:00",
                    "age_seconds": 12.0,
                    "repo": "demo/live",
                }
            ]
        )
        self.conn.commit()
        items = self._derive()
        live = [i for i in items if i.session_id == "cursor:live1"]
        self.assertEqual(len(live), 1)
        self.assertEqual(live[0].state, "live_waiting")
        self.assertEqual(live[0].severity, "warn")

    def test_severity_scales_with_recency(self) -> None:
        # 20h idle → within horizon but past warn_within_hours (12h) → info
        self._session(
            "codex:aging",
            started="2026-08-08T12:00:00+00:00",
            ended="2026-08-08T16:00:00+00:00",
            repo="demo/aging",
        )
        self.conn.execute(
            """
            INSERT INTO messages (id, session_id, seq, role, timestamp, text)
            VALUES
              ('g1', 'codex:aging', 1, 'assistant', '2026-08-08T16:00:00+00:00',
               'Continue with option A?')
            """
        )
        self.conn.commit()
        items = self._derive()
        hit = next(i for i in items if i.session_id == "codex:aging")
        self.assertEqual(hit.state, "waiting_on_user")
        self.assertEqual(hit.severity, "info")

    def test_severity_ordering_live_before_info(self) -> None:
        self._session(
            "cursor:live2",
            started="2026-08-09T11:00:00+00:00",
            harness="cursor",
            repo="demo/ord",
        )
        self.conn.execute(
            """
            INSERT INTO messages (id, session_id, seq, role, timestamp, text)
            VALUES ('x1', 'cursor:live2', 1, 'assistant',
                    '2026-08-09T11:55:00+00:00', 'ok')
            """
        )
        self._session(
            "codex:aging2",
            started="2026-08-08T12:00:00+00:00",
            ended="2026-08-08T16:00:00+00:00",
            repo="demo/aging2",
        )
        self.conn.execute(
            """
            INSERT INTO messages (id, session_id, seq, role, timestamp, text)
            VALUES
              ('x2', 'codex:aging2', 1, 'assistant', '2026-08-08T16:00:00+00:00',
               'Ship it?')
            """
        )
        self._write_presence(
            [
                {
                    "harness": "cursor",
                    "external_id": "live2",
                    "session_id": "cursor:live2",
                    "state": "waiting",
                    "last_activity_at": "2026-08-09T11:59:30+00:00",
                    "age_seconds": 5.0,
                }
            ]
        )
        self.conn.commit()
        items = self._derive()
        states = [i.state for i in items]
        self.assertIn("live_waiting", states)
        self.assertIn("waiting_on_user", states)
        self.assertLess(states.index("live_waiting"), states.index("waiting_on_user"))

    def test_long_running_active_within_horizon(self) -> None:
        self._session(
            "codex:long",
            started="2026-08-09T06:00:00+00:00",
            ended=None,
        )
        self.conn.execute(
            """
            INSERT INTO messages (id, session_id, seq, role, timestamp, text)
            VALUES ('l1', 'codex:long', 1, 'assistant',
                    '2026-08-09T11:50:00+00:00', 'still working')
            """
        )
        self.conn.commit()
        items = self._derive()
        long_items = [i for i in items if i.state == "long_running"]
        self.assertEqual(len(long_items), 1)
        self.assertEqual(long_items[0].severity, "info")

    def test_zombie_long_running_dropped(self) -> None:
        # Started weeks ago; recent chatter must not look like a 400h run.
        self._session(
            "cursor:zombie",
            started="2026-07-20T10:00:00+00:00",
            ended=None,
            harness="cursor",
        )
        self.conn.execute(
            """
            INSERT INTO messages (id, session_id, seq, role, timestamp, text)
            VALUES ('z1', 'cursor:zombie', 1, 'assistant',
                    '2026-08-09T11:50:00+00:00', 'still here')
            """
        )
        self.conn.commit()
        items = self._derive()
        self.assertFalse(any(i.session_id == "cursor:zombie" for i in items))

    def test_clip_breaks_on_word_boundary(self) -> None:
        from agentlog.analysis.attention import _clip

        text = "What commitment follows the 24-hour usage sprint after launch?"
        clipped = _clip(text, limit=40)
        self.assertTrue(clipped.endswith("…"))
        self.assertNotIn("spri…", clipped)
        self.assertLessEqual(len(clipped), 40)

    def test_no_false_waiting_without_question(self) -> None:
        self._session(
            "codex:plain",
            started="2026-08-09T08:00:00+00:00",
            ended="2026-08-09T09:00:00+00:00",
        )
        self.conn.execute(
            """
            INSERT INTO messages (id, session_id, seq, role, timestamp, text)
            VALUES ('p1', 'codex:plain', 1, 'assistant',
                    '2026-08-09T09:00:00+00:00', 'Done with the patch.')
            """
        )
        self.conn.commit()
        items = self._derive(
            thresholds=AttentionThresholds(waiting_hours=2.0),
        )
        self.assertFalse(any(i.state == "waiting_on_user" for i in items))

    def test_payload_separates_lanes_and_stats(self) -> None:
        self._session(
            "codex:fresh",
            started="2026-08-09T08:00:00+00:00",
            ended="2026-08-09T09:00:00+00:00",
            repo="demo/fresh",
        )
        self.conn.execute(
            """
            INSERT INTO messages (id, session_id, seq, role, timestamp, text)
            VALUES
              ('f1', 'codex:fresh', 1, 'assistant', '2026-08-09T09:00:00+00:00',
               'Pick a path?')
            """
        )
        self._session(
            "codex:dorm",
            started="2026-07-20T08:00:00+00:00",
            ended="2026-07-20T09:00:00+00:00",
            repo="demo/dorm",
        )
        self.conn.execute(
            """
            INSERT INTO messages (id, session_id, seq, role, timestamp, text)
            VALUES
              ('f2', 'codex:dorm', 1, 'assistant', '2026-07-20T09:00:00+00:00',
               'Resume later?')
            """
        )
        self.conn.commit()
        payload = attention_payload(
            self.conn, now=self.now, presence_path=self.presence_path
        )
        self.assertEqual(payload["count"], len(payload["items"]))
        self.assertTrue(all(i["lane"] == "urgent" for i in payload["items"]))
        self.assertTrue(all(i["lane"] == "resumable" for i in payload["resumable"]))
        self.assertIn("stats", payload)
        self.assertIn("removed_by_horizon", payload["stats"])

    def test_api_filter_and_sort(self) -> None:
        now = datetime.now(timezone.utc)
        wait_at = (now - timedelta(hours=3)).isoformat()
        long_start = (now - timedelta(hours=5)).isoformat()
        long_last = (now - timedelta(minutes=5)).isoformat()

        self._session("codex:wait", started=wait_at, ended=wait_at, repo="api/w")
        self.conn.execute(
            """
            INSERT INTO messages (id, session_id, seq, role, timestamp, text)
            VALUES ('a1', 'codex:wait', 1, 'assistant', ?, 'Ready to continue?')
            """,
            (wait_at,),
        )
        self._session("codex:long", started=long_start, ended=None)
        self.conn.execute(
            """
            INSERT INTO messages (id, session_id, seq, role, timestamp, text)
            VALUES ('a2', 'codex:long', 1, 'assistant', ?, 'working')
            """,
            (long_last,),
        )
        self.conn.commit()
        self.conn.close()

        # Empty presence beside the test DB.
        (self.path.parent / "presence.json").write_text(
            json.dumps({"ts": now.isoformat(), "generation": 0, "sessions": []}),
            encoding="utf-8",
        )

        client = TestClient(create_app(self.path))
        res = client.get("/api/attention")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertIn("items", body)
        self.assertIn("stats", body)
        states = {i["state"] for i in body["items"]}
        self.assertIn("waiting_on_user", states)
        self.assertIn("long_running", states)

        filtered = client.get("/api/attention", params={"state": "long_running"})
        self.assertEqual(filtered.status_code, 200)
        for item in filtered.json()["items"]:
            self.assertEqual(item["state"], "long_running")

        bad = client.get("/api/attention", params={"state": "stale_session"})
        self.assertEqual(bad.status_code, 400)


if __name__ == "__main__":
    unittest.main()
