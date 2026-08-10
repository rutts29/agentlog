from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from agentlog.api.app import create_app
from agentlog.db.schema import connect, init_db
from agentlog.normalize.model_identity import (
    UNKNOWN_MODEL_LABEL,
    backfill_model_identity,
    is_known_non_model,
)


class ApiModelCanonicalTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "m.db"
        conn = connect(self.path)
        init_db(conn)
        conn.executescript(
            """
            INSERT INTO sessions
              (id, harness, external_id, started_at, model)
            VALUES
              ('codex:a', 'codex', 'a', '2026-08-01T00:00:00+00:00', 'gpt-5.5'),
              ('codex:b', 'codex', 'b', '2026-08-01T00:00:00+00:00', 'codex-auto-review'),
              ('claude:c', 'claude', 'c', '2026-08-01T00:00:00+00:00', 'grok-4.5-build'),
              ('codex:d', 'codex', 'd', '2026-08-01T00:00:00+00:00', 'openai'),
              ('warp:e', 'warp', 'e', '2026-08-01T00:00:00+00:00', NULL);

            INSERT INTO messages
              (id, session_id, seq, role, model, text, content_hash)
            VALUES
              ('a:1', 'codex:a', 1, 'assistant', 'gpt-5.5', '', 'a1'),
              ('b:1', 'codex:b', 1, 'assistant', 'codex-auto-review', '', 'b1'),
              ('c:1', 'claude:c', 1, 'assistant', 'grok-4.5-build', '', 'c1'),
              ('d:1', 'codex:d', 1, 'assistant', 'openai', '', 'd1'),
              ('e:1', 'warp:e', 1, 'assistant', NULL, '', 'e1');
            """
        )
        backfill_model_identity(conn)
        conn.commit()
        conn.close()
        self.client = TestClient(create_app(self.path))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_models_endpoint_uses_canonical(self) -> None:
        res = self.client.get("/api/models", params={"range": "all"})
        self.assertEqual(res.status_code, 200)
        body = res.json()
        models = {i["model"] for i in body["items"]}
        self.assertIn("gpt-5.5", models)
        self.assertIn("grok-4.5", models)
        self.assertIn("(unknown)", models)
        self.assertNotIn("codex-auto-review", models)
        self.assertNotIn("grok-4.5-build", models)
        self.assertNotIn("openai", models)

        harnesses = {
            i["model"]: {h["harness"] for h in i["harnesses"]}
            for i in body["items"]
        }
        self.assertEqual(harnesses["gpt-5.5"], {"codex"})
        self.assertEqual(harnesses["grok-4.5"], {"claude"})
        self.assertEqual(harnesses["(unknown)"], {"codex", "warp"})

        profiles = {p["agent_profile"] for p in body["profiles"]}
        self.assertIn("codex-auto-review", profiles)
        self.assertIn("grok-4.5-build", profiles)

    def test_model_mix_rows_are_unique_and_never_non_models(self) -> None:
        res = self.client.get("/api/models", params={"range": "all"})
        body = res.json()
        labels = [i["model"] for i in body["items"]]
        self.assertEqual(
            len(labels), len(set(labels)), f"duplicate model rows: {labels}"
        )
        for label in labels:
            if label == UNKNOWN_MODEL_LABEL:
                continue
            self.assertFalse(
                is_known_non_model(label),
                f"{label} is a provider/placeholder/agent-profile, not a model",
            )
        self.assertAlmostEqual(sum(i["share"] for i in body["items"]), 1.0)

    def test_unknown_bucket_declares_why_each_session_landed_there(self) -> None:
        res = self.client.get("/api/models", params={"range": "all"})
        body = res.json()
        detail = body["unknown"]
        self.assertEqual(detail["messages"], 3)
        reasons = {r["reason"]: r["messages"] for r in detail["reasons"]}
        self.assertEqual(reasons["agent_profile"], 1)
        self.assertEqual(reasons["provider_name"], 1)
        self.assertEqual(reasons["no_model_recorded"], 1)
        self.assertEqual(sum(reasons.values()), detail["messages"])
        self.assertEqual(detail["sessions"], 3)

    def test_monthly_and_auto_review_rows_are_unique_per_model(self) -> None:
        monthly = self.client.get(
            "/api/models/monthly", params={"range": "all"}
        ).json()
        for month in monthly["series"]:
            labels = [i["model"] for i in month["items"]]
            self.assertEqual(len(labels), len(set(labels)), month["month"])

        review = self.client.get(
            "/api/auto-review", params={"range": "all"}
        ).json()
        labels = [i["model"] for i in review["by_model"]]
        self.assertEqual(len(labels), len(set(labels)))

    def test_session_detail_never_shows_a_raw_profile_as_model(self) -> None:
        detail = self.client.get("/api/sessions/codex:b").json()
        self.assertEqual(detail["session"]["model"], UNKNOWN_MODEL_LABEL)
        self.assertEqual(detail["session"]["model_raw"], "codex-auto-review")

    def test_activity_rollup_and_tokens_group_by(self) -> None:
        roll = self.client.get("/api/activity/rollup", params={"range": "all"})
        self.assertEqual(roll.status_code, 200)
        body = roll.json()
        model_names = {
            r["session_start_model"] for r in body["by_session_start_model"]
        } | {r["message_model"] for r in body["by_message_model"]}
        self.assertNotIn("codex-auto-review", model_names)
        self.assertNotIn("grok-4.5-build", model_names)
        profiles = {
            (r["agent_profile"], r["harness"])
            for r in body["by_agent_profile"]
        }
        self.assertIn(("codex-auto-review", "codex"), profiles)

        usage = self.client.get(
            "/api/tokens/usage",
            params={"range": "all", "group_by": "model"},
        )
        self.assertEqual(usage.status_code, 200)
        keys = {g["key"] for g in usage.json()["groups"]}
        self.assertNotIn("codex-auto-review", keys)
        self.assertNotIn("grok-4.5-build", keys)

        by_profile = self.client.get(
            "/api/tokens/usage",
            params={"range": "all", "group_by": "agent_profile"},
        )
        self.assertEqual(by_profile.status_code, 200)
        pkeys = {g["key"] for g in by_profile.json()["groups"]}
        self.assertTrue(
            {"codex-auto-review", "grok-4.5-build"} & pkeys
            or "(none)" in pkeys
        )


if __name__ == "__main__":
    unittest.main()
