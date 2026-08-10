import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from typer.testing import CliRunner

from agentlog.cli import app
from agentlog.db.schema import init_db


class CoachCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "selected.db"
        self.run_dir = self.tmp / "coach-run"
        self.runner = CliRunner()

    def invoke(self, *args: str):
        return self.runner.invoke(app, ["--db", str(self.db), *args])

    def test_prepare_honors_injected_db_and_stays_in_run_dir(self):
        result = self.invoke("coach", "prepare", "--run-dir", str(self.run_dir))

        self.assertEqual(result.exit_code, 0, result.output)
        body = json.loads(result.output)
        self.assertEqual(body["phase"], "prepare")
        self.assertEqual(body["luna_packets"]["expected"], 0)
        self.assertNotIn("processed", result.output)
        self.assertTrue(self.db.is_file())
        self.assertTrue((self.run_dir / "manifest.json").is_file())
        self.assertFalse((self.tmp / "agentlog.db").exists())

    def test_full_prepare_packetizes_more_than_default_sample_bound(self):
        conn = sqlite3.connect(self.db)
        init_db(conn)
        conn.execute(
            "INSERT INTO sessions(id,harness,external_id,repo) VALUES(?,?,?,?)",
            ("root", "codex", "root", "demo"),
        )
        for number in range(9):
            request_id = f"request-{number}"
            response_id = f"response-{number}"
            window_id = f"window-{number}"
            conn.execute(
                "INSERT INTO messages(id,session_id,seq,role,text,content_hash) VALUES(?,?,?,?,?,?)",
                (request_id, "root", number * 2 + 1, "user", "Please verify the tests", f"request-hash-{number}"),
            )
            conn.execute(
                "INSERT INTO messages(id,session_id,seq,role,text,content_hash) VALUES(?,?,?,?,?,?)",
                (response_id, "root", number * 2 + 2, "assistant", "I will verify the tests", f"response-hash-{number}"),
            )
            conn.execute(
                "INSERT INTO exchange_windows(id,session_id,request_message_id,response_message_id,input_hash,content_hash) VALUES(?,?,?,?,?,?)",
                (window_id, "root", request_id, response_id, f"input-hash-{number}", f"window-hash-{number}"),
            )
        conn.commit()
        conn.close()

        result = self.invoke("coach", "prepare", "--run-dir", str(self.run_dir))

        self.assertEqual(result.exit_code, 0, result.output)
        body = json.loads(result.output)
        self.assertEqual(body["sampling"]["mode"], "full")
        self.assertTrue(body["sampling"]["publishable"])
        self.assertEqual(body["coverage"]["eligible_windows"], 9)
        self.assertEqual(body["coverage"]["packetized_windows"], 9)
        self.assertEqual(body["coverage"]["eligible_roots"], 1)
        self.assertEqual(body["coverage"]["packetized_roots"], 1)
        self.assertEqual(body["sampling"]["bounds"]["max_windows_per_packet"], 24)
        self.assertEqual(body["sampling"]["bounds"]["max_packet_chars"], 1_500_000)

    def test_sampled_prepare_is_visible_as_non_publishable(self):
        result = self.invoke(
            "coach",
            "prepare",
            "--run-dir",
            str(self.run_dir),
            "--sampled",
        )

        self.assertEqual(result.exit_code, 0, result.output)
        body = json.loads(result.output)
        self.assertEqual(body["sampling"]["mode"], "sampled")
        self.assertFalse(body["sampling"]["publishable"])
        self.assertFalse(body["proposal_readiness"]["publishable"])

    def test_full_prepare_keeps_long_redacted_message_untruncated(self):
        conn = sqlite3.connect(self.db)
        init_db(conn)
        conn.execute(
            "INSERT INTO sessions(id,harness,external_id,repo) VALUES(?,?,?,?)",
            ("root", "codex", "root", "demo"),
        )
        long_request = "HEAD " + ("middle " * 200) + "TAIL"
        conn.execute(
            "INSERT INTO messages(id,session_id,seq,role,text,content_hash) VALUES(?,?,?,?,?,?)",
            ("request", "root", 1, "user", long_request, "request-hash"),
        )
        conn.execute(
            "INSERT INTO messages(id,session_id,seq,role,text,content_hash) VALUES(?,?,?,?,?,?)",
            ("response", "root", 2, "assistant", "I will verify the tests", "response-hash"),
        )
        conn.execute(
            "INSERT INTO exchange_windows(id,session_id,request_message_id,response_message_id,input_hash,content_hash) VALUES(?,?,?,?,?,?)",
            ("window", "root", "request", "response", "input-hash", "window-hash"),
        )
        conn.commit()
        conn.close()

        result = self.invoke("coach", "prepare", "--run-dir", str(self.run_dir))

        self.assertEqual(result.exit_code, 0, result.output)
        body = json.loads(result.output)
        self.assertEqual(body["source_truncated_messages"], 0)
        packet_path = self.run_dir / "packets" / "cpkt_0001.json"
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        message = packet["windows"][0]["messages"][0]
        self.assertFalse(message["source_truncated"])
        self.assertTrue(message["source_text"].endswith("TAIL"))

        sampled_run = self.tmp / "sampled-run"
        sampled = self.invoke(
            "coach",
            "prepare",
            "--run-dir",
            str(sampled_run),
            "--sampled",
            "--max-quote-chars",
            "800",
        )
        self.assertEqual(sampled.exit_code, 0, sampled.output)
        sampled_body = json.loads(sampled.output)
        self.assertGreater(sampled_body["source_truncated_messages"], 0)
        self.assertFalse(sampled_body["proposal_readiness"]["publishable"])
        blocked = self.invoke("coach", "materialize", "--run-dir", str(sampled_run))
        self.assertEqual(blocked.exit_code, 1, blocked.output)
        self.assertEqual(json.loads(blocked.output)["failures"][0]["reason"], "coach_run_not_publishable")

    def test_prepare_rejects_an_oversized_packet_byte_budget(self):
        conn = sqlite3.connect(self.db)
        init_db(conn)
        conn.execute(
            "INSERT INTO sessions(id,harness,external_id,repo) VALUES(?,?,?,?)",
            ("root", "codex", "root", "demo"),
        )
        conn.execute(
            "INSERT INTO messages(id,session_id,seq,role,text,content_hash) VALUES(?,?,?,?,?,?)",
            ("request", "root", 1, "user", "Please verify the tests", "request-hash"),
        )
        conn.execute(
            "INSERT INTO messages(id,session_id,seq,role,text,content_hash) VALUES(?,?,?,?,?,?)",
            ("response", "root", 2, "assistant", "I will verify the tests", "response-hash"),
        )
        conn.execute(
            "INSERT INTO exchange_windows(id,session_id,request_message_id,response_message_id,input_hash,content_hash) VALUES(?,?,?,?,?,?)",
            ("window", "root", "request", "response", "input-hash", "window-hash"),
        )
        conn.commit()
        conn.close()

        result = self.invoke(
            "coach", "prepare", "--run-dir", str(self.run_dir), "--max-packet-chars", "1"
        )

        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("coach_packet_byte_budget_exceeded", json.loads(result.output)["failures"][0]["reason"])

    def test_synthesize_reports_missing_exact_luna_results(self):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        init_db(conn)
        conn.execute(
            "INSERT INTO sessions(id,harness,external_id,repo) VALUES(?,?,?,?)",
            ("root", "codex", "root", "demo"),
        )
        conn.execute(
            "INSERT INTO messages(id,session_id,seq,role,text,content_hash) VALUES(?,?,?,?,?,?)",
            ("request", "root", 1, "user", "Please verify the tests", "request-hash"),
        )
        conn.execute(
            "INSERT INTO messages(id,session_id,seq,role,text,content_hash) VALUES(?,?,?,?,?,?)",
            ("response", "root", 2, "assistant", "I will verify the tests", "response-hash"),
        )
        conn.execute(
            "INSERT INTO exchange_windows(id,session_id,request_message_id,response_message_id,input_hash,content_hash) VALUES(?,?,?,?,?,?)",
            ("window", "root", "request", "response", "input-hash", "window-hash"),
        )
        conn.commit()
        conn.close()

        prepared = self.invoke("coach", "prepare", "--run-dir", str(self.run_dir))
        self.assertEqual(prepared.exit_code, 0, prepared.output)
        result = self.invoke("coach", "synthesize", "--run-dir", str(self.run_dir))

        self.assertEqual(result.exit_code, 1)
        body = json.loads(result.output)
        self.assertFalse(body["complete"])
        self.assertEqual(body["failures"][0]["reason"], "missing_luna_result_packets")
        self.assertNotIn("processed", result.output)

    def test_quarantine_defaults_to_dry_run_and_apply_is_explicit(self):
        dry_run = self.invoke("coach", "quarantine")
        self.assertEqual(dry_run.exit_code, 0, dry_run.output)
        self.assertEqual(json.loads(dry_run.output)["mode"], "dry-run")

        applied = self.invoke("coach", "quarantine", "--apply")
        self.assertEqual(applied.exit_code, 0, applied.output)
        self.assertEqual(json.loads(applied.output)["mode"], "apply")

    def test_synthesis_stages_luna_then_terra_then_review_before_ready(self):
        prepared = self.invoke("coach", "prepare", "--run-dir", str(self.run_dir))
        self.assertEqual(prepared.exit_code, 0, prepared.output)
        terra_path = self.run_dir / "terra.json"
        terra_path.write_text("{}", encoding="utf-8")
        review_path = self.run_dir / "review.json"
        review_path.write_text("{}", encoding="utf-8")
        synthesis_manifest = {"packets": [{"packet_id": "spkt-1"}]}
        luna_only = {
            "synthesis_manifest": synthesis_manifest,
            "validated_results": [],
            "validation_failures": [],
            "catalog": None,
            "second_review": None,
            "run_bundle": {"bundle_hash": "bundle-1"},
        }
        terra_only = {
            "synthesis_manifest": synthesis_manifest,
            "validated_results": [{"packet_id": "spkt-1"}],
            "validation_failures": [],
            "catalog": {"catalog_id": "catalog-1"},
            "second_review": None,
            "run_bundle": {"bundle_hash": "bundle-1"},
        }
        reviewed = {
            **terra_only,
            "second_review": {"review_id": "review-1"},
        }
        with (
            patch(
                "agentlog.analysis.coach.run_synthesis_pipeline",
                side_effect=[luna_only, terra_only, reviewed],
            ),
            patch("agentlog.analysis.coach.verify_coach_run", return_value=object()),
        ):
            luna = self.invoke("coach", "synthesize", "--run-dir", str(self.run_dir))
            terra = self.invoke(
                "coach",
                "synthesize",
                "--run-dir",
                str(self.run_dir),
                "--terra-results",
                str(terra_path),
            )
            final = self.invoke(
                "coach",
                "synthesize",
                "--run-dir",
                str(self.run_dir),
                "--terra-results",
                str(terra_path),
                "--second-review",
                str(review_path),
            )

        self.assertEqual(luna.exit_code, 0, luna.output)
        self.assertEqual(json.loads(luna.output)["stage"], "awaiting_terra")
        self.assertFalse(json.loads(luna.output)["materialization_ready"])
        self.assertEqual(terra.exit_code, 0, terra.output)
        self.assertEqual(json.loads(terra.output)["stage"], "awaiting_review")
        self.assertFalse(json.loads(terra.output)["complete"])
        self.assertEqual(final.exit_code, 0, final.output)
        final_body = json.loads(final.output)
        self.assertEqual(final_body["stage"], "ready")
        self.assertTrue(final_body["materialization_ready"])
        self.assertTrue(final_body["complete"])
