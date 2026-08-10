"""Tests for grouped token usage, activity calendar, and activity rollup."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from agentlog.api.app import create_app
from agentlog.api.activity import _streaks, activity_calendar, activity_rollup
from agentlog.api.ranges import TimeRange, parse_range
from agentlog.api import tokens as tokens_api
from agentlog.db.schema import connect, init_db
from agentlog.normalize.model_identity import backfill_model_identity


def _tr(
    *,
    key: str = "all",
    start: datetime | None = None,
    end: datetime | None = None,
) -> TimeRange:
    end = end or datetime(2026, 8, 10, tzinfo=timezone.utc)
    return TimeRange(
        key=key, start=start, end=end, prev_start=None, prev_end=None
    )


def _seed_minimal(conn) -> None:
    conn.executescript(
        """
        INSERT INTO artifacts
          (id, harness, path, size, mtime_ns, content_hash, parsed_offset, parser_version)
        VALUES
          (1, 'codex', '/a', 1, 1, 'h1', 1, '1'),
          (2, 'claude', '/b', 1, 1, 'h2', 1, '1'),
          (3, 'cursor', '/c', 1, 1, 'h3', 1, '1');

        INSERT INTO sessions
          (id, harness, external_id, artifact_id, started_at, ended_at, repo, model, effort)
        VALUES
          ('codex:s1', 'codex', 's1', 1,
           '2026-08-01T10:00:00+00:00', '2026-08-01T11:00:00+00:00',
           'https://github.com/acme/plugin.git', 'gpt-5', 'high'),
          ('claude:s1', 'claude', 's1', 2,
           '2026-08-02T10:00:00+00:00', '2026-08-02T10:30:00+00:00',
           'https://github.com/acme/plugin.git', 'claude-opus-4-6', NULL),
          ('cursor:s1', 'cursor', 's1', 3,
           '2026-08-03T10:00:00+00:00', '2026-08-03T10:05:00+00:00',
           'other-repo', 'gpt-5.5', 'medium'),
          ('codex:s2', 'codex', 's2', 1,
           '2026-08-05T10:00:00+00:00', '2026-08-05T10:20:00+00:00',
           'https://github.com/acme/plugin.git', 'gpt-5', 'low');

        INSERT INTO messages
          (id, session_id, seq, role, timestamp, model, text, content_hash)
        VALUES
          ('m1', 'codex:s1', 1, 'user', '2026-08-01T10:00:00+00:00', NULL, 'hi', 'a'),
          ('m2', 'codex:s1', 2, 'assistant', '2026-08-01T10:01:00+00:00', 'gpt-5', 'ok', 'b'),
          ('m3', 'claude:s1', 1, 'user', '2026-08-02T10:00:00+00:00', NULL, 'hi', 'c'),
          ('m4', 'claude:s1', 2, 'assistant', '2026-08-02T10:01:00+00:00',
           'claude-opus-4-6', 'ok', 'd'),
          ('m5', 'claude:s1', 3, 'assistant', '2026-08-02T10:02:00+00:00',
           'claude-sonnet-4', 'switched', 'e'),
          ('m6', 'cursor:s1', 1, 'user', '2026-08-03T10:00:00+00:00', NULL, 'hi', 'f'),
          ('m7', 'cursor:s1', 2, 'assistant', '2026-08-03T10:01:00+00:00', 'gpt-5.5', 'ok', 'g'),
          ('m8', 'codex:s2', 1, 'user', '2026-08-05T10:00:00+00:00', NULL, 'hi', 'h');

        INSERT INTO tool_events
          (id, session_id, message_id, seq, tool_name, action, success, duration_ms)
        VALUES
          ('t1', 'codex:s1', 'm2', 3, 'shell', 'call', 1, 10),
          ('t2', 'claude:s1', 'm4', 4, 'Read', 'call', 1, 5),
          ('t3', 'claude:s1', 'm5', 5, 'Edit', 'call', 1, 8);

        -- Codex: two cumulative snapshots; summing them would wrongly yield 385.
        INSERT INTO token_usage
          (id, session_id, message_id, seq, granularity, usage_source, model,
           input_tokens, output_tokens, cached_input_tokens, cache_write_input_tokens,
           reasoning_output_tokens, total_tokens, timestamp)
        VALUES
          ('u1', 'codex:s1', NULL, 2, 'turn', 'codex_last_token_usage', 'gpt-5',
           100, 10, 0, 0, 0, 110, '2026-08-01T10:01:00+00:00'),
          ('u2', 'codex:s1', NULL, 2, 'session_cumulative', 'codex_total_token_usage', 'gpt-5',
           100, 10, 0, 0, 0, 110, '2026-08-01T10:01:00+00:00'),
          ('u3', 'codex:s1', NULL, 4, 'turn', 'codex_last_token_usage', 'gpt-5',
           150, 15, 40, 5, 3, 165, '2026-08-01T10:02:00+00:00'),
          ('u4', 'codex:s1', NULL, 4, 'session_cumulative', 'codex_total_token_usage', 'gpt-5',
           250, 25, 40, 5, 3, 275, '2026-08-01T10:02:00+00:00');

        INSERT INTO token_usage
          (id, session_id, message_id, seq, granularity, usage_source, model,
           input_tokens, output_tokens, cache_creation_input_tokens,
           cache_read_input_tokens, timestamp)
        VALUES
          ('u5', 'claude:s1', 'm4', 2, 'message', 'claude_message_usage',
           'claude-opus-4-6', 100, 20, 5, 40, '2026-08-02T10:01:00+00:00'),
          ('u6', 'claude:s1', 'm5', 3, 'message', 'claude_message_usage',
           'claude-sonnet-4', 50, 10, NULL, NULL, '2026-08-02T10:02:00+00:00');
        """
    )
    backfill_model_identity(conn)
    conn.commit()


class CodexCumulativeTrapTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "t.db"
        self.conn = connect(self.db)
        init_db(self.conn)
        _seed_minimal(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_usage_by_harness_avoids_cumulative_double_count(self) -> None:
        out = tokens_api.usage(self.conn, _tr(), group_by="harness")
        by_key = {g["key"]: g for g in out["groups"]}
        codex = by_key["codex"]
        # Final cumulative is 275, not 110+275=385, not sum of turns 110+165=275
        # (turns happen to equal final here — assert against cumulative sum trap).
        self.assertEqual(codex["total_tokens"], 275)
        self.assertEqual(codex["input_tokens"], 250)
        self.assertNotEqual(codex["total_tokens"], 110 + 275)
        self.assertEqual(
            codex["coverage"]["aggregation"], "final_session_cumulative"
        )
        self.assertFalse(codex["coverage"]["complete"])  # messages not linked
        self.assertTrue(codex["coverage"]["partial"])

    def test_claude_sums_messages_and_reports_partial_message_coverage(self) -> None:
        out = tokens_api.usage(self.conn, _tr(), group_by="harness")
        claude = {g["key"]: g for g in out["groups"]}["claude"]
        self.assertEqual(claude["input_tokens"], 150)  # 100+50
        self.assertEqual(claude["output_tokens"], 30)
        self.assertEqual(claude["cache_read_input_tokens"], 40)
        self.assertEqual(claude["coverage"]["messages_with_usage"], 2)
        self.assertEqual(claude["coverage"]["messages_total"], 3)
        self.assertFalse(claude["coverage"]["complete"])
        self.assertTrue(claude["coverage"]["partial"])

    def test_cursor_tokens_null_not_zero(self) -> None:
        out = tokens_api.usage(self.conn, _tr(), group_by="harness")
        cursor = {g["key"]: g for g in out["groups"]}["cursor"]
        self.assertIsNone(cursor["input_tokens"])
        self.assertIsNone(cursor["total_tokens"])
        self.assertEqual(cursor["coverage"]["sessions_with_usage"], 0)
        self.assertFalse(cursor["coverage"]["partial"])
        self.assertEqual(cursor["cost"]["status"], "unavailable")
        self.assertEqual(cursor["cost"]["reason"], "pricing_rates_unconfigured")

    def test_coverage_math_and_group_by_model(self) -> None:
        out = tokens_api.usage(self.conn, _tr(), group_by="model")
        by_key = {g["key"]: g for g in out["groups"]}
        self.assertIn("claude-sonnet-4", by_key)
        self.assertEqual(by_key["claude-sonnet-4"]["input_tokens"], 50)
        self.assertEqual(by_key["gpt-5"]["total_tokens"], 275)

    def test_range_filtering(self) -> None:
        tr = parse_range(
            "custom",
            custom_start="2026-08-02T00:00:00+00:00",
            custom_end="2026-08-04T00:00:00+00:00",
        )
        out = tokens_api.usage(self.conn, tr, group_by="harness")
        keys = {g["key"] for g in out["groups"] if g["usage_rows"] > 0}
        self.assertEqual(keys, {"claude"})


class ActivityCalendarTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "t.db"
        self.conn = connect(self.db)
        init_db(self.conn)
        _seed_minimal(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_empty_days_filled_and_tokens_additive(self) -> None:
        tr = parse_range(
            "custom",
            custom_start="2026-08-01T00:00:00+00:00",
            custom_end="2026-08-06T00:00:00+00:00",
        )
        out = activity_calendar(self.conn, tr)
        dates = [c["date"] for c in out["days"]]
        self.assertEqual(
            dates,
            ["2026-08-01", "2026-08-02", "2026-08-03", "2026-08-04", "2026-08-05"],
        )
        by = {c["date"]: c for c in out["days"]}
        self.assertEqual(by["2026-08-01"]["sessions"], 1)
        self.assertEqual(by["2026-08-01"]["total_tokens"], 275)
        # Claude rows omit total_tokens; calendar falls back to input+output.
        self.assertEqual(by["2026-08-02"]["input_tokens"], 150)
        self.assertEqual(by["2026-08-02"]["total_tokens"], 180)
        self.assertEqual(by["2026-08-04"]["sessions"], 0)
        self.assertIsNone(by["2026-08-04"]["total_tokens"])
        self.assertEqual(by["2026-08-03"]["sessions"], 1)
        self.assertFalse(by["2026-08-03"]["tokens_known"])
        self.assertGreaterEqual(out["max"]["sessions"], 1)
        self.assertIn("current_days", out["streaks"])

    def test_streak_computation(self) -> None:
        end = datetime(2026, 8, 6, tzinfo=timezone.utc)
        # Contiguous through 08-05; gap to end_day=08-06 is 1 → current kept
        s = _streaks(["2026-08-05", "2026-08-03", "2026-08-02", "2026-08-01"], end=end)
        self.assertEqual(s["current"], 1)  # only 08-05 contiguous from latest
        self.assertEqual(s["longest"], 3)  # 08-01..08-03
        broken = _streaks(["2026-08-01"], end=end)
        self.assertEqual(broken["current"], 0)


class ActivityRollupTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "t.db"
        self.conn = connect(self.db)
        init_db(self.conn)
        _seed_minimal(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_rollup_counts_and_model_switches(self) -> None:
        out = activity_rollup(self.conn, _tr())
        by_h = {r["harness"]: r for r in out["by_harness"]}
        self.assertEqual(by_h["codex"]["sessions"], 2)
        self.assertEqual(by_h["claude"]["messages"], 3)
        self.assertEqual(by_h["claude"]["tool_events"], 2)
        self.assertIsNotNone(by_h["claude"]["mean_session_duration_seconds"])
        # m4 opus -> m5 sonnet is one switch on claude
        self.assertEqual(by_h["claude"]["model_switch_count"], 1)
        efforts = {
            e["effort"]: e["sessions"]
            for e in by_h["codex"]["effort_distribution"]
        }
        self.assertEqual(efforts["high"], 1)
        self.assertEqual(efforts["low"], 1)


class EndpointSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "t.db"
        conn = connect(self.db)
        init_db(conn)
        _seed_minimal(conn)
        conn.close()
        self.client = TestClient(create_app(self.db))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_endpoints_registered(self) -> None:
        r = self.client.get("/api/tokens/usage?range=all&group_by=harness")
        self.assertEqual(r.status_code, 200)
        self.assertIn("groups", r.json())
        r = self.client.get("/api/activity/calendar?range=90d")
        self.assertEqual(r.status_code, 200)
        self.assertIn("days", r.json())
        r = self.client.get("/api/activity/rollup?range=30d")
        self.assertEqual(r.status_code, 200)
        self.assertIn("by_harness", r.json())
        r = self.client.get("/api/activity/calendar?range=7d")
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
