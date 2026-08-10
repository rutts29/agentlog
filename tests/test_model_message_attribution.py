"""Model-facing aggregates must follow response-level model evidence."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from agentlog.api.descriptive import list_sessions_v2
from agentlog.api.model_rollup import strict_message_model_sql
from agentlog.api.queries import model_mix, models_profile
from agentlog.api.ranges import TimeRange
from agentlog.api.semantic import eligible_windows
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


class ModelMessageAttributionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self._tmp.name) / "attribution.db")
        init_db(self.conn)
        self.conn.executescript(
            """
            INSERT INTO sessions (id, harness, external_id, started_at, model)
            VALUES ('cursor:s1', 'cursor', 's1', '2026-08-01T00:00:00+00:00', 'grok-4.5');

            INSERT INTO messages
              (id, session_id, seq, role, model, text, content_hash)
            VALUES
              ('u1', 'cursor:s1', 1, 'user', NULL, 'first', 'u1'),
              ('a1', 'cursor:s1', 2, 'assistant', 'gpt-5.5', 'first answer', 'a1'),
              ('u2', 'cursor:s1', 3, 'user', NULL, 'second', 'u2'),
              ('a2', 'cursor:s1', 4, 'assistant', 'grok-4.5', 'second answer', 'a2'),
              ('u3', 'cursor:s1', 5, 'user', NULL, 'third', 'u3'),
              ('a3', 'cursor:s1', 6, 'assistant', NULL, 'third answer', 'a3');

            INSERT INTO exchange_windows
              (id, session_id, request_message_id, response_message_id, input_hash,
               content_hash)
            VALUES
              ('w1', 'cursor:s1', 'u1', 'a1', 'w1', 'w1'),
              ('w2', 'cursor:s1', 'u2', 'a2', 'w2', 'w2'),
              ('w3', 'cursor:s1', 'u3', 'a3', 'w3', 'w3');

            INSERT INTO derivation_runs
              (id, kind, extractor_name, extractor_version, started_at, status)
            VALUES ('run-1', 'ux', 'test', '1', '2026-08-01T00:00:00+00:00', 'completed');

            INSERT INTO window_det_classifications
              (id, window_id, run_id, turn_kinds_json, route, request_kind,
               extractor_name, extractor_version, created_at)
            VALUES
              ('d1', 'w1', 'run-1', '[]', 'ux', 'substantive', 'test', '1',
               '2026-08-01T00:00:00+00:00'),
              ('d2', 'w2', 'run-1', '[]', 'ux', 'substantive', 'test', '1',
               '2026-08-01T00:00:00+00:00'),
              ('d3', 'w3', 'run-1', '[]', 'ux', 'substantive', 'test', '1',
               '2026-08-01T00:00:00+00:00');
            """
        )
        backfill_model_identity(self.conn)
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_mix_uses_each_assistant_message_without_session_double_counting(
        self,
    ) -> None:
        rows = {row["model"]: row for row in model_mix(self.conn, _tr())}
        self.assertEqual(
            {model: row["messages"] for model, row in rows.items()},
            {"gpt-5.5": 1, "grok-4.5": 1, "(unknown)": 1},
        )
        self.assertAlmostEqual(sum(row["share"] for row in rows.values()), 1.0)
        profile = {row["model"]: row for row in models_profile(self.conn, _tr())["items"]}
        self.assertEqual(profile["gpt-5.5"]["sessions"], 1)
        self.assertEqual(profile["gpt-5.5"]["messages"], 1)

    def test_session_filter_matches_any_observed_assistant_model(self) -> None:
        for model in ("gpt-5.5", "grok-4.5", "(unknown)"):
            result = list_sessions_v2(self.conn, _tr(), model=[model])
            self.assertEqual([item["id"] for item in result["items"]], ["cursor:s1"])

    def test_performance_windows_follow_their_response_model(self) -> None:
        self.assertEqual(
            [
                row["window_id"]
                for row in eligible_windows(self.conn, _tr(), model="gpt-5.5")
            ],
            ["w1"],
        )
        self.assertEqual(
            [
                row["window_id"]
                for row in eligible_windows(self.conn, _tr(), model="grok-4.5")
            ],
            ["w2"],
        )
        self.assertEqual(
            [
                row["window_id"]
                for row in eligible_windows(self.conn, _tr(), model="(unknown)")
            ],
            ["w3"],
        )

    def test_direct_model_lookup_has_matching_partial_index(self) -> None:
        indexes = {
            str(row[1]) for row in self.conn.execute("PRAGMA index_list(messages)")
        }
        self.assertIn("idx_messages_direct_assistant_model_session", indexes)
        expression = strict_message_model_sql()
        self.assertIn("observed_model.role = 'assistant'", expression)
        self.assertIn("observed_model.model_canonical <> ''", expression)


if __name__ == "__main__":
    unittest.main()
