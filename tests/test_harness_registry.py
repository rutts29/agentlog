from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from agentlog.api.app import create_app
from agentlog.db.schema import connect, init_db
from agentlog.registry import CAPABILITY_KEYS, HARNESSES, get_harness, supports


class HarnessRegistryTests(unittest.TestCase):
    def test_active_harnesses_have_required_fields(self) -> None:
        active = [
            h for h in HARNESSES.values() if h["ingest_status"] == "active"
        ]
        self.assertGreaterEqual(len(active), 3)
        required = {
            "id",
            "display_name",
            "vendor",
            "ingest_status",
            "transcript_locations",
            "capabilities",
        }
        for harness in active:
            missing = required - set(harness)
            self.assertFalse(missing, msg=f"{harness.get('id')}: {missing}")
            self.assertTrue(harness["transcript_locations"])
            caps = harness["capabilities"]
            for key in CAPABILITY_KEYS:
                self.assertIn(key, caps)
                self.assertIn(
                    caps[key],
                    {"supported", "partial", "absent", "unknown"},
                )

    def test_warp_hermes_active(self) -> None:
        self.assertEqual(HARNESSES["warp"]["ingest_status"], "active")
        self.assertEqual(HARNESSES["hermes"]["ingest_status"], "active")
        self.assertEqual(
            HARNESSES["warp"]["capabilities"]["tool_events"], "partial"
        )
        self.assertEqual(
            HARNESSES["hermes"]["capabilities"]["tool_events"], "supported"
        )

    def test_supports_helper(self) -> None:
        self.assertEqual(supports("claude", "skill_exposures"), "supported")
        self.assertEqual(supports("codex", "skill_exposures"), "absent")
        self.assertEqual(supports("nope", "branch"), "unknown")

    def test_get_harness(self) -> None:
        self.assertIsNotNone(get_harness("cursor"))
        self.assertIsNone(get_harness("missing"))

    def test_api_returns_live_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "reg.db"
            conn = connect(path)
            init_db(conn)
            conn.execute(
                """
                INSERT INTO artifacts
                (harness, path, size, mtime_ns, content_hash, parsed_offset, parser_version)
                VALUES ('cursor', '/tmp/c.jsonl', 1, 1, 'h', 0, '1')
                """
            )
            art = int(conn.execute("SELECT id FROM artifacts").fetchone()["id"])
            conn.execute(
                """
                INSERT INTO sessions (
                    id, harness, external_id, artifact_id,
                    started_at, ended_at, branch, model
                ) VALUES (
                    'cursor:s1', 'cursor', 's1', ?,
                    '2026-07-01T00:00:00+00:00', '2026-07-01T01:00:00+00:00',
                    'main', 'gpt-5'
                )
                """,
                (art,),
            )
            conn.execute(
                """
                INSERT INTO sessions (
                    id, harness, external_id, artifact_id,
                    started_at, ended_at, branch, model
                ) VALUES (
                    'cursor:s2', 'cursor', 's2', ?,
                    '2026-07-01T00:00:00+00:00', '2026-07-01T01:00:00+00:00',
                    NULL, 'gpt-5'
                )
                """,
                (art,),
            )
            conn.execute(
                """
                INSERT INTO messages (id, session_id, seq, role, timestamp, model, text)
                VALUES
                  ('m1', 'cursor:s1', 1, 'user', '2026-07-01T00:00:01+00:00', NULL, 'hi'),
                  ('m2', 'cursor:s1', 2, 'assistant', '2026-07-01T00:00:02+00:00', 'gpt-5', 'ok')
                """
            )
            conn.commit()
            conn.close()

            client = TestClient(create_app(path))
            res = client.get("/api/harnesses")
            self.assertEqual(res.status_code, 200)
            body = res.json()
            self.assertIn("items", body)
            by_id = {item["id"]: item for item in body["items"]}
            self.assertIn("claude", by_id)
            self.assertIn("codex", by_id)
            self.assertIn("cursor", by_id)
            self.assertIn("warp", by_id)
            cursor = by_id["cursor"]
            self.assertEqual(cursor["sessions"], 2)
            branch = cursor["capabilities"]["branch"]
            self.assertEqual(branch["level"], "partial")
            self.assertEqual(branch["observed"], 1)
            self.assertEqual(branch["total"], 2)
            self.assertAlmostEqual(branch["coverage"], 0.5)
            model = cursor["capabilities"]["per_message_model"]
            self.assertEqual(model["observed"], 1)
            self.assertEqual(model["total"], 2)
            self.assertEqual(by_id["warp"]["sessions"], 0)
            self.assertIsNone(
                by_id["warp"]["capabilities"]["branch"].get("coverage")
            )


if __name__ == "__main__":
    unittest.main()
