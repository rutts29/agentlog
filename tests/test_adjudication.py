from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from agentlog.api.adjudication import (
    build_report,
    is_adjudicable_request,
)
from agentlog.api.app import create_app
from agentlog.db.schema import connect, init_db


class EligibilityTests(unittest.TestCase):
    def test_rejects_system_reminder_and_agent_authored(self) -> None:
        ok, reason = is_adjudicable_request(
            "<system-reminder>\nOther agents active\n</system-reminder>",
            authored_by_agent=False,
            is_tool_plumbing=False,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "system_reminder")

        ok, reason = is_adjudicable_request(
            "Please implement wave 0 task 9",
            authored_by_agent=True,
            is_tool_plumbing=False,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "authored_by_agent")

    def test_accepts_short_human_soft_approval(self) -> None:
        ok, reason = is_adjudicable_request(
            "lgtm",
            authored_by_agent=False,
            is_tool_plumbing=False,
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "ok")


class ReportMathTests(unittest.TestCase):
    def test_insufficient_data_gate(self) -> None:
        pairs = [
            (
                {
                    "turn_kind": ["human_task"],
                    "user_stance": "neutral",
                    "agent_stance": "executing",
                    "prior_outcome": "abstain",
                },
                {
                    "turn_kind": ["human_task"],
                    "user_stance": "neutral",
                    "agent_stance": "executing",
                    "prior_outcome": "abstain",
                },
            )
        ] * 19
        report = build_report(
            pairs, queue_total=100, adjudicated=19, with_llm=19, min_n=20
        )
        self.assertTrue(report["insufficient_data"])
        self.assertEqual(report["adjudicated"], 19)
        self.assertEqual(report["with_llm"], 19)
        self.assertEqual(report["total_queue"], 100)
        self.assertNotIn("fields", report)

    def test_exact_match_and_precision_with_denominators(self) -> None:
        pairs = []
        for i in range(20):
            if i < 15:
                human = {
                    "turn_kind": ["human_task"],
                    "user_stance": "neutral",
                    "agent_stance": "executing",
                    "prior_outcome": "abstain",
                }
                llm = dict(human)
            elif i < 18:
                human = {
                    "turn_kind": ["correction"],
                    "user_stance": "correcting",
                    "agent_stance": "executing",
                    "prior_outcome": "rejected_redo",
                }
                llm = {
                    "turn_kind": ["human_followup"],
                    "user_stance": "neutral",
                    "agent_stance": "executing",
                    "prior_outcome": "rejected_redo",
                }
            else:
                human = {
                    "turn_kind": ["soft_approval"],
                    "user_stance": "approving",
                    "agent_stance": "narrating_wait",
                    "prior_outcome": "accepted_continue",
                }
                llm = {
                    "turn_kind": ["soft_approval"],
                    "user_stance": "approving",
                    "agent_stance": "executing",
                    "prior_outcome": "accepted_continue",
                }
            pairs.append((human, llm))

        report = build_report(
            pairs, queue_total=100, adjudicated=20, with_llm=20, min_n=20
        )
        self.assertFalse(report["insufficient_data"])
        tk = report["fields"]["turn_kind"]["exact_match"]
        self.assertEqual(tk["n"], 20)
        self.assertEqual(tk["matches"], 17)
        self.assertAlmostEqual(tk["rate"], 17 / 20)


class AdjudicationApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.db_path = self.root / "t.db"
        self.conn = connect(self.db_path)
        init_db(self.conn)

        # w0,w1 human-eligible; w2 agent-authored (ineligible)
        specs = [
            ("s0", "w0", "Please implement the login form carefully", False),
            ("s1", "w1", "That looks wrong — use the other API instead", False),
            (
                "s2",
                "w2",
                "<system-reminder>\nOther agents active in this session\n</system-reminder>",
                False,
            ),
            ("s3", "w3", "Full repository path: /tmp\nImplement wave 0", True),
        ]
        for i, (sid, wid, text, agent) in enumerate(specs):
            self.conn.execute(
                """
                INSERT INTO sessions (id, harness, external_id, started_at, model)
                VALUES (?, ?, ?, ?, 'm')
                """,
                (sid, "codex" if i % 2 == 0 else "claude", f"ext{i}", f"2026-0{i+1}-01T00:00:00+00:00"),
            )
            mid_u = f"{sid}:u"
            mid_a = f"{sid}:a"
            self.conn.execute(
                """
                INSERT INTO messages
                (id, session_id, seq, role, text, timestamp, authored_by_agent, is_tool_plumbing)
                VALUES (?, ?, 1, 'user', ?, '2026-08-01T00:00:00+00:00', ?, 0)
                """,
                (mid_u, sid, text, 1 if agent else 0),
            )
            self.conn.execute(
                """
                INSERT INTO messages (id, session_id, seq, role, text, timestamp)
                VALUES (?, ?, 2, 'assistant', ?, '2026-08-01T00:01:00+00:00')
                """,
                (mid_a, sid, f"assistant reply {i}"),
            )
            # prior agent turn
            self.conn.execute(
                """
                INSERT INTO messages (id, session_id, seq, role, text, timestamp)
                VALUES (?, ?, 0, 'assistant', ?, '2026-08-01T00:00:00+00:00')
                """,
                (f"{sid}:prior", sid, f"prior agent context {i}"),
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
            self.conn.execute(
                """
                INSERT INTO derivation_runs
                (id, kind, extractor_name, extractor_version, started_at, status)
                VALUES (?, 'ux', 'ux_v1', '0.1.0', '2026-08-01T00:00:00+00:00', 'done')
                """,
                (f"run-{wid}",),
            )
            self.conn.execute(
                """
                INSERT INTO ux_observations (
                    id, window_id, run_id, turn_kinds_json, user_stance, agent_stance,
                    prior_outcome, flags_json, spans_json, confidence_json,
                    abstain_reasons_json, novel_observations_json,
                    extractor_name, extractor_version, model, prompt_hash, created_at
                ) VALUES (?, ?, ?, ?, 'neutral', 'executing', 'abstain',
                          '{}', '[]', '{}', '[]', '[]',
                          'ux_v1', '0.1.0', 'm', 'p', '2026-08-01T00:00:00+00:00')
                """,
                (f"ux-{wid}", wid, f"run-{wid}", json.dumps(["human_task"])),
            )
        self.conn.commit()

        self.app = create_app(
            self.db_path,
            adjudication_window_ids=("w0", "w1"),
        )
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass
        self._tmp.cleanup()

    def test_queue_contains_only_explicit_escalations(self) -> None:
        res = self.client.get("/api/adjudication/queue")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        ids = {i["window_id"] for i in data["items"]}
        self.assertEqual(ids, {"w0", "w1"})
        self.assertEqual(data["pack"]["mode"], "explicit_escalations")
        self.assertEqual(data["pack"]["configured"], 2)
        self.assertFalse(data["pack"]["rebuilt"])

    def test_scalar_window_id_is_one_exact_escalation(self) -> None:
        app = create_app(self.db_path, adjudication_window_ids="w0")
        self.assertEqual(app.state.adjudication_window_ids, ("w0",))
        data = TestClient(app).get("/api/adjudication/queue").json()
        self.assertEqual([item["window_id"] for item in data["items"]], ["w0"])

    def test_save_rejects_window_outside_exact_allowlist(self) -> None:
        response = self.client.post(
            "/api/adjudication/w2",
            json={"triage": "no", "source": "ad_hoc"},
        )
        self.assertEqual(response.status_code, 403)
        count = self.conn.execute("SELECT COUNT(*) AS c FROM adjudications").fetchone()
        self.assertEqual(count["c"], 0)

    def test_queue_hides_llm_until_saved_and_includes_turns(self) -> None:
        data = self.client.get("/api/adjudication/queue").json()
        item = next(i for i in data["items"] if i["window_id"] == "w0")
        self.assertIsNone(item["llm"])
        self.assertFalse(item["adjudicated"])
        slots = [t["slot"] for t in item["payload"]["turns"]]
        self.assertIn("prior_agent", slots)
        self.assertIn("human", slots)
        self.assertIn("agent", slots)

        self.client.post(
            "/api/adjudication/w0",
            json={
                "triage": "yes",
                "turn_kind": ["human_task"],
                "user_stance": "neutral",
                "agent_stance": "executing",
                "prior_outcome": "abstain",
                "source": "audit_pack",
            },
        )
        q2 = self.client.get("/api/adjudication/queue").json()
        item2 = next(i for i in q2["items"] if i["window_id"] == "w0")
        self.assertTrue(item2["adjudicated"])
        self.assertIsNotNone(item2["llm"])
        self.assertEqual(q2["progress"]["done"], 1)
        self.assertEqual(item2["position"], item2["index"] + 1)

    def test_triage_no_human_and_vague_paths(self) -> None:
        res = self.client.post(
            "/api/adjudication/w1",
            json={"triage": "no", "source": "audit_pack"},
        )
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["turn_kind"], [])
        self.assertEqual(body["user_stance"], "abstain")
        self.assertIn("triage:no_human", body["notes"])

        res2 = self.client.post(
            "/api/adjudication/w0",
            json={
                "triage": "yes",
                "turn_kind": ["human_followup"],
                "user_stance": "abstain",
                "agent_stance": "executing",
                "prior_outcome": "abstain",
                "vague_fields": ["user_stance"],
                "source": "audit_pack",
            },
        )
        self.assertEqual(res2.status_code, 200)
        self.assertIn("vague:user_stance", res2.json()["notes"])

    def test_keeps_existing_adjudication_across_queue_reads(self) -> None:
        self.client.post(
            "/api/adjudication/w0",
            json={
                "triage": "yes",
                "turn_kind": ["correction"],
                "user_stance": "correcting",
                "agent_stance": "executing",
                "prior_outcome": "abstain",
                "notes": "keep me",
                "source": "audit_pack",
            },
        )
        self.client.get("/api/adjudication/queue")
        row = self.conn.execute(
            "SELECT notes, turn_kind FROM adjudications WHERE window_id='w0'"
        ).fetchone()
        self.assertIn("keep me", row["notes"])
        self.assertEqual(json.loads(row["turn_kind"]), ["correction"])

    def test_report_counts_saved_even_without_enough_llm_pairs(self) -> None:
        self.client.post(
            "/api/adjudication/w0",
            json={"triage": "no", "source": "audit_pack"},
        )
        r = self.client.get("/api/adjudication/report").json()
        self.assertEqual(r["adjudicated"], 1)
        self.assertTrue(r["insufficient_data"])

    def test_taxonomy_has_plain_language(self) -> None:
        data = self.client.get("/api/adjudication/taxonomy").json()
        labels = {o["value"]: o["label"] for o in data["turn_kind"]}
        self.assertEqual(labels["dont_act_yet"], "told the agent to stop or wait")
        self.assertTrue(any(o["key"] == "v" for o in data["user_stance"]))

    def test_missing_escalation_is_skipped_without_general_backfill(self) -> None:
        app = create_app(
            self.db_path,
            adjudication_window_ids=("does-not-exist-anymore", "w0"),
        )
        data = TestClient(app).get("/api/adjudication/queue").json()
        ids = {i["window_id"] for i in data["items"]}
        self.assertEqual(ids, {"w0"})
        self.assertEqual(data["pack"]["configured"], 2)
        self.assertEqual(data["pack"]["skipped_stale"], 1)
        self.assertTrue(all(i["payload"]["turns"] for i in data["items"]))
        self.assertNotIn("w1", ids)
        report = TestClient(app).get("/api/adjudication/report").json()
        self.assertEqual(report["configured"], 2)
        self.assertEqual(report["total_queue"], 1)
        self.assertEqual(report["skipped_stale"], 1)

    def test_default_mode_is_empty_and_cannot_write_or_rebuild(self) -> None:
        paused = TestClient(create_app(self.db_path))
        queue = paused.get("/api/adjudication/queue")
        self.assertEqual(queue.status_code, 200)
        self.assertEqual(queue.json()["items"], [])
        self.assertEqual(
            queue.json()["progress"],
            {"done": 0, "total": 0, "remaining": 0},
        )

        report = paused.get("/api/adjudication/report")
        self.assertEqual(report.status_code, 200)
        self.assertEqual(report.json()["adjudicated"], 0)
        self.assertEqual(report.json()["total_queue"], 0)
        self.assertEqual(report.json()["configured"], 0)

        rebuild = paused.post("/api/adjudication/rebuild")
        self.assertEqual(rebuild.status_code, 409)
        self.assertEqual(
            paused.get("/api/adjudication/queue?rebuild=true").status_code,
            409,
        )
        save = paused.post(
            "/api/adjudication/w0",
            json={"triage": "no", "source": "ad_hoc"},
        )
        self.assertEqual(save.status_code, 403)
        count = self.conn.execute("SELECT COUNT(*) AS c FROM adjudications").fetchone()
        self.assertEqual(count["c"], 0)

    def test_save_waits_out_competing_write_lock(self) -> None:
        # Close the setUp handle so BEGIN EXCLUSIVE can take the file.
        self.conn.close()

        blocker = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=0)
        blocker.execute("PRAGMA busy_timeout = 0")
        blocker.execute("BEGIN EXCLUSIVE")

        result: dict[str, object] = {}

        def do_save() -> None:
            res = self.client.post(
                "/api/adjudication/w0",
                json={
                    "triage": "yes",
                    "turn_kind": ["human_task"],
                    "user_stance": "neutral",
                    "agent_stance": "executing",
                    "prior_outcome": "abstain",
                    "source": "audit_pack",
                },
            )
            result["status"] = res.status_code
            if res.status_code == 200:
                result["body"] = res.json()

        worker = threading.Thread(target=do_save)
        worker.start()
        time.sleep(0.35)
        blocker.commit()
        blocker.close()
        worker.join(timeout=20)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result.get("status"), 200)

        self.conn = connect(self.db_path)
        row = self.conn.execute(
            """
            SELECT content_hash, link_status, orphaned_at, turn_kind
            FROM adjudications WHERE window_id = 'w0'
            """
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["content_hash"], "w0")
        self.assertEqual(row["link_status"], "linked")
        self.assertIsNone(row["orphaned_at"])
        self.assertEqual(json.loads(row["turn_kind"]), ["human_task"])

    def test_save_is_idempotent_and_sets_durability_columns(self) -> None:
        payload = {
            "triage": "yes",
            "turn_kind": ["correction"],
            "user_stance": "correcting",
            "agent_stance": "executing",
            "prior_outcome": "rejected_redo",
            "notes": "first",
            "source": "audit_pack",
        }
        r1 = self.client.post("/api/adjudication/w0", json=payload)
        self.assertEqual(r1.status_code, 200)
        payload2 = dict(payload)
        payload2["notes"] = "second"
        payload2["turn_kind"] = ["human_followup"]
        r2 = self.client.post("/api/adjudication/w0", json=payload2)
        self.assertEqual(r2.status_code, 200)
        self.assertIn("second", r2.json()["notes"])
        self.assertEqual(r2.json()["content_hash"], "w0")
        self.assertEqual(r2.json()["link_status"], "linked")

        rows = self.conn.execute(
            "SELECT notes, turn_kind, content_hash, link_status, orphaned_at "
            "FROM adjudications WHERE window_id = 'w0'"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertIn("second", rows[0]["notes"])
        self.assertEqual(json.loads(rows[0]["turn_kind"]), ["human_followup"])
        self.assertEqual(rows[0]["content_hash"], "w0")
        self.assertEqual(rows[0]["link_status"], "linked")
        self.assertIsNone(rows[0]["orphaned_at"])

    def test_busy_retry_helper_retries_locked_errors(self) -> None:
        from agentlog.api.deps import with_busy_retry

        calls = {"n": 0}

        def flaky() -> str:
            calls["n"] += 1
            if calls["n"] < 3:
                raise sqlite3.OperationalError("database is locked")
            return "ok"

        with mock.patch("agentlog.api.deps.time.sleep", return_value=None):
            self.assertEqual(
                with_busy_retry(flaky, attempts=5, base_delay_s=0.01), "ok"
            )
        self.assertEqual(calls["n"], 3)

        # Locked mid-transaction: rollback between attempts so retry can proceed.
        rolls = {"n": 0}

        class _Conn:
            def rollback(self) -> None:
                rolls["n"] += 1

        calls["n"] = 0
        with mock.patch("agentlog.api.deps.time.sleep", return_value=None):
            self.assertEqual(
                with_busy_retry(flaky, conn=_Conn(), attempts=5, base_delay_s=0.01),
                "ok",
            )
        self.assertEqual(rolls["n"], 2)


if __name__ == "__main__":
    unittest.main()
