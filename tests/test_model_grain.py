"""Model grain contract: session counts and message counts never mix.

Reproduces the H6 defect — a session recorded as model A whose second message
runs on model B — and asserts the aggregates stay internally consistent.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from agentlog.api.activity import activity_rollup
from agentlog.api import tokens as tokens_api
from agentlog.api.app import create_app
from agentlog.api.model_rollup import MESSAGE_MODEL, SESSION_START_MODEL
from agentlog.api.ranges import TimeRange
from agentlog.db.schema import connect, init_db
from agentlog.normalize.model_identity import backfill_model_identity


def _tr() -> TimeRange:
    return TimeRange(
        key="all",
        start=None,
        end=datetime(2026, 8, 10, tzinfo=timezone.utc),
        prev_start=None,
        prev_end=None,
    )


class ModelGrainTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "grain.db"
        self.conn = connect(self.db)
        init_db(self.conn)
        self.conn.executescript(
            """
            INSERT INTO artifacts
              (id, harness, path, size, mtime_ns, content_hash,
               parsed_offset, parser_version)
            VALUES (1, 'cursor', '/a', 1, 1, 'h1', 1, '1');

            INSERT INTO sessions
              (id, harness, external_id, artifact_id, started_at, ended_at,
               repo, model, effort)
            VALUES
              ('cursor:s1', 'cursor', 's1', 1,
               '2026-08-01T10:00:00+00:00', '2026-08-01T10:10:00+00:00',
               'acme', 'gpt-5.5', NULL);

            INSERT INTO messages
              (id, session_id, seq, role, timestamp, model, text, content_hash)
            VALUES
              ('m1', 'cursor:s1', 1, 'assistant',
               '2026-08-01T10:01:00+00:00', 'gpt-5.5', 'a', 'h1'),
              ('m2', 'cursor:s1', 2, 'assistant',
               '2026-08-01T10:02:00+00:00', 'grok-4.5', 'b', 'h2');

            INSERT INTO token_usage
              (id, session_id, message_id, seq, granularity, usage_source,
               model, input_tokens, output_tokens, total_tokens, timestamp)
            VALUES
              ('u1', 'cursor:s1', 'm1', 1, 'message', 'msg', 'gpt-5.5',
               10, 1, 11, '2026-08-01T10:01:00+00:00'),
              ('u2', 'cursor:s1', 'm2', 2, 'message', 'msg', 'grok-4.5',
               20, 2, 22, '2026-08-01T10:02:00+00:00');
            """
        )
        backfill_model_identity(self.conn)
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_switched_model_keeps_its_own_message_row(self) -> None:
        out = activity_rollup(self.conn, _tr())
        by_msg = {
            r["message_model"]: r["messages"] for r in out["by_message_model"]
        }
        self.assertEqual(by_msg, {"gpt-5.5": 1, "grok-4.5": 1})

        by_sess = {
            r["session_start_model"]: r["sessions"]
            for r in out["by_session_start_model"]
        }
        self.assertEqual(by_sess, {"gpt-5.5": 1})

    def test_message_rows_reconcile_to_harness_total(self) -> None:
        out = activity_rollup(self.conn, _tr())
        harness = {r["harness"]: r for r in out["by_harness"]}
        self.assertEqual(harness["cursor"]["messages"], 2)
        self.assertEqual(
            sum(r["messages"] for r in out["by_message_model"]),
            harness["cursor"]["messages"],
        )
        self.assertEqual(
            sum(r["sessions"] for r in out["by_session_start_model"]),
            harness["cursor"]["sessions"],
        )
        self.assertTrue(out["reconciliation"]["ok"], out["reconciliation"])
        self.assertTrue(
            all(c["ok"] for c in out["reconciliation"]["checks"])
        )

    def test_rollup_declares_the_grain_of_every_model_block(self) -> None:
        out = activity_rollup(self.conn, _tr())
        self.assertEqual(
            out["grains"]["by_session_start_model"], SESSION_START_MODEL
        )
        self.assertEqual(out["grains"]["by_message_model"], MESSAGE_MODEL)
        self.assertNotIn(
            "by_model", out, "ambiguous by_model key must not come back"
        )

    def test_token_usage_by_model_shares_one_grain(self) -> None:
        out = tokens_api.usage(self.conn, _tr(), group_by="model")
        self.assertEqual(out["grain"], MESSAGE_MODEL)
        by_key = {g["key"]: g for g in out["groups"]}
        self.assertEqual(set(by_key), {"gpt-5.5", "grok-4.5"})

        # The switched-to model has usage, messages, and a session denominator
        # it can actually be measured against.
        grok = by_key["grok-4.5"]
        self.assertEqual(grok["total_tokens"], 22)
        self.assertEqual(grok["coverage"]["messages_with_usage"], 1)
        self.assertEqual(grok["coverage"]["messages_total"], 1)
        self.assertEqual(grok["coverage"]["sessions_with_usage"], 1)
        self.assertEqual(grok["coverage"]["sessions_total"], 1)

    def test_no_group_has_a_contradictory_coverage_tuple(self) -> None:
        for group_by in ("model", "harness", "agent_profile", "day", "repo"):
            out = tokens_api.usage(self.conn, _tr(), group_by=group_by)
            for g in out["groups"]:
                cov = g["coverage"]
                self.assertLessEqual(
                    cov["sessions_with_usage"],
                    cov["sessions_total"],
                    f"{group_by}/{g['key']}",
                )
                self.assertLessEqual(
                    cov["messages_with_usage"],
                    cov["messages_total"],
                    f"{group_by}/{g['key']}",
                )
                if g["usage_rows"]:
                    self.assertGreater(
                        cov["sessions_total"], 0, f"{group_by}/{g['key']}"
                    )

    def test_session_grain_token_totals_are_named_as_such(self) -> None:
        out = tokens_api.by_model(self.conn, _tr())
        self.assertEqual(out["grain"], SESSION_START_MODEL)
        keys = {i["session_start_model"] for i in out["items"]}
        self.assertEqual(keys, {"gpt-5.5"})
        self.assertNotIn("model", out["items"][0])


class LiveCorpusGrainTests(unittest.TestCase):
    """Guards the shape on any DB, including one with real switch traffic."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "corpus.db"
        conn = connect(self.db)
        init_db(conn)
        conn.executescript(
            """
            INSERT INTO sessions
              (id, harness, external_id, started_at, model)
            VALUES
              ('codex:a', 'codex', 'a', '2026-08-01T00:00:00+00:00', 'gpt-5.5'),
              ('codex:b', 'codex', 'b', '2026-08-01T00:00:00+00:00', NULL);

            INSERT INTO messages
              (id, session_id, seq, role, timestamp, model, text, content_hash)
            VALUES
              ('x1', 'codex:a', 1, 'assistant', '2026-08-01T00:01:00+00:00',
               'gpt-5.5', 'a', 'p'),
              ('x2', 'codex:a', 2, 'assistant', '2026-08-01T00:02:00+00:00',
               'gpt-5.6-terra', 'b', 'q'),
              ('x3', 'codex:b', 1, 'assistant', '2026-08-01T00:03:00+00:00',
               'gpt-5.4', 'c', 'r');
            """
        )
        backfill_model_identity(conn)
        conn.commit()
        conn.close()
        self.client = TestClient(create_app(self.db))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_endpoint_reconciles_and_surfaces_switched_models(self) -> None:
        body = self.client.get(
            "/api/activity/rollup", params={"range": "all"}
        ).json()
        self.assertTrue(body["reconciliation"]["ok"], body["reconciliation"])
        seen = {r["message_model"] for r in body["by_message_model"]}
        # gpt-5.6-terra and gpt-5.4 exist only at message level; under the old
        # session-model join they had no row at all.
        self.assertIn("gpt-5.6-terra", seen)
        self.assertIn("gpt-5.4", seen)
        starts = {
            r["session_start_model"] for r in body["by_session_start_model"]
        }
        self.assertEqual(starts, {"gpt-5.5", "(unknown)"})


if __name__ == "__main__":
    unittest.main()
