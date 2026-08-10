from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from agentlog.analysis.performance.gates import (
    CLUSTER_EVENT_FLOOR,
    CLUSTER_N_HARD_FLOOR,
    WILSON_MAX_HALF_WIDTH,
    evaluate_binary_rate,
    evaluate_continuous_rate,
)
from agentlog.analysis.performance.stats import wilson_interval
from agentlog.api.app import create_app
from agentlog.db.schema import connect, init_db


class GateAbstentionTests(unittest.TestCase):
    def test_underpowered_binary_cell_abstains_no_point_estimate(self) -> None:
        # n=8 successes=4: below event floor of 10 → must abstain.
        cell = evaluate_binary_rate(
            metric="had_redirect_brake",
            successes=4,
            n_clusters=8,
            session_ids=[f"s{i}" for i in range(8)],
            availability=1.0,
        )
        self.assertEqual(cell.status, "abstain")
        self.assertEqual(cell.reason, "insufficient_sample")
        self.assertIsNone(cell.estimate)
        self.assertGreaterEqual(len(cell.session_ids), 1)
        self.assertIn("small_sample", cell.flags)

    def test_precision_gate_blocks_wide_wilson(self) -> None:
        # n=12, p=0.5 → Wilson half-width > 10pp → abstain despite n>=10.
        n = 12
        successes = 6
        iv = wilson_interval(successes, n)
        half = (iv.high - iv.low) / 2.0
        self.assertGreater(half, WILSON_MAX_HALF_WIDTH)
        self.assertGreaterEqual(n, CLUSTER_EVENT_FLOOR)

        cell = evaluate_binary_rate(
            metric="had_redirect_brake",
            successes=successes,
            n_clusters=n,
            session_ids=[f"s{i}" for i in range(n)],
            availability=1.0,
        )
        self.assertEqual(cell.status, "abstain")
        self.assertEqual(cell.reason, "insufficient_precision")
        self.assertIsNone(cell.estimate)
        self.assertIsNotNone(cell.interval_low)
        self.assertIsNotNone(cell.interval_high)

    def test_hard_floor_below_five_abstains(self) -> None:
        cell = evaluate_binary_rate(
            metric="had_redirect_brake",
            successes=2,
            n_clusters=CLUSTER_N_HARD_FLOOR - 1,
            session_ids=["a", "b", "c", "d"],
            availability=1.0,
        )
        self.assertEqual(cell.status, "abstain")
        self.assertEqual(cell.reason, "insufficient_sample")
        self.assertIsNone(cell.estimate)

    def test_passing_binary_includes_interval(self) -> None:
        # Large n with moderate p should pass the Wilson half-width gate.
        n = 200
        successes = 40
        cell = evaluate_binary_rate(
            metric="had_redirect_brake",
            successes=successes,
            n_clusters=n,
            session_ids=[f"s{i}" for i in range(n)],
            availability=1.0,
        )
        self.assertEqual(cell.status, "ok")
        self.assertIsNotNone(cell.estimate)
        self.assertIsNotNone(cell.interval_low)
        self.assertIsNotNone(cell.interval_high)
        half = (cell.interval_high - cell.interval_low) / 2.0
        self.assertLessEqual(half, WILSON_MAX_HALF_WIDTH)

    def test_continuous_small_n_abstains(self) -> None:
        values = [1.0, 2.0, 1.5, 0.5, 3.0, 2.5, 1.2, 0.8, 1.1]
        self.assertLess(len(values), CLUSTER_EVENT_FLOOR)
        cell = evaluate_continuous_rate(
            metric="redirects_brakes_per_10_exchange_windows",
            per_cluster_values=values,
            session_ids=[f"s{i}" for i in range(len(values))],
            availability=1.0,
        )
        self.assertEqual(cell.status, "abstain")
        self.assertIsNone(cell.estimate)


class ApiAbstentionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "dash.db"
        conn = connect(self.path)
        init_db(conn)
        conn.execute(
            """
            INSERT INTO artifacts
            (harness, path, size, mtime_ns, content_hash, parsed_offset, parser_version)
            VALUES ('codex', '/tmp/a.jsonl', 1, 1, 'h', 0, '1')
            """
        )
        art = conn.execute("SELECT id FROM artifacts").fetchone()
        assert art is not None
        for i in range(3):
            conn.execute(
                """
                INSERT INTO sessions (
                    id, harness, external_id, parent_session_id, artifact_id,
                    started_at, model
                ) VALUES (?, 'codex', ?, NULL, ?, '2026-07-01T00:00:00+00:00', 'gpt-5.5')
                """,
                (f"codex:s{i}", f"s{i}", int(art["id"])),
            )
        conn.commit()
        conn.close()
        self.client = TestClient(create_app(self.path))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_binary_route_abstains_underpowered(self) -> None:
        res = self.client.get("/api/aggregates/binary", params={"successes": 3, "n": 7})
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["status"], "abstain")
        self.assertIsNone(body["estimate"])
        self.assertNotIn("rate", body)
        self.assertTrue(body["session_ids"])

    def test_summary_interaction_style_unavailable_without_ux(self) -> None:
        res = self.client.get("/api/summary", params={"range": "all"})
        self.assertEqual(res.status_code, 200)
        body = res.json()
        lead = body["kpis"]["interaction_style"]
        self.assertEqual(lead["status"], "unavailable")
        self.assertIsNone(lead["estimate"])
        self.assertIn("published semantic extraction run", lead["message"])

    def test_models_profile_does_not_rank_by_quality(self) -> None:
        res = self.client.get("/api/models", params={"range": "all"})
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertIn("usage", body["title"].lower())
        for item in body["items"]:
            style = item["interaction_style"]
            self.assertIn(style["status"], {"unavailable", "abstain"})
            self.assertIsNone(style["estimate"])


if __name__ == "__main__":
    unittest.main()
