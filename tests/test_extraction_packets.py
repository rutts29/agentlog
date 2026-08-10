from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agentlog.analysis.extractors.packets import (
    DEFAULT_WINDOWS_PER_PACKET,
    PacketExtractionProvider,
    emit_packet_run,
    ingest_packet_result,
    ingest_packet_results,
    labeled_window_ids,
    pack_windows,
    packet_run_status,
    raw_to_observation,
    validate_window_result,
)
from agentlog.analysis.extractors.storage import (
    load_ux_observations,
    write_ux_observations,
)
from agentlog.db.repository import Repository
from agentlog.db.schema import connect, init_db
from agentlog.normalize.models import (
    Harness,
    NormalizedMessage,
    NormalizedSession,
    ParseResult,
)


def _seed_ux_windows(conn, n: int = 6) -> list[str]:
    repo = Repository(conn)
    art_id = repo.upsert_artifact(
        harness="cursor",
        path="/tmp/packet-fixture.jsonl",
        size=10,
        mtime_ns=1,
        content_hash="packet-hash",
        parsed_offset=10,
        parser_version="test",
    )
    messages: list[NormalizedMessage] = []
    seq = 1
    for i in range(n):
        messages.append(
            NormalizedMessage(
                seq=seq,
                role="user",
                text=f"please implement feature number {i} carefully without scaffolding yet",
                content_hash=f"u{i}",
            )
        )
        seq += 1
        messages.append(
            NormalizedMessage(
                seq=seq,
                role="assistant",
                text=f"I will implement feature {i}. Pause before scaffolding.",
                content_hash=f"a{i}",
            )
        )
        seq += 1
        messages.append(
            NormalizedMessage(
                seq=seq,
                role="user",
                text=f"dont jump on scaffolding for feature {i} yet",
                content_hash=f"n{i}",
            )
        )
        seq += 1
        messages.append(
            NormalizedMessage(
                seq=seq,
                role="assistant",
                text=f"Understood, waiting on feature {i}.",
                content_hash=f"a2{i}",
            )
        )
        seq += 1

    result = ParseResult(
        session=NormalizedSession(
            harness=Harness.CURSOR,
            external_id="packet-sess",
            model="grok-4.5",
        ),
        messages=messages,
    )
    sid = repo.save_parse_result(artifact_id=art_id, result=result, append=False)
    msgs = repo.list_messages(sid)
    users = [m for m in msgs if m["role"] == "user" and not m["is_tool_plumbing"]]
    assistants = [m for m in msgs if m["role"] == "assistant"]
    windows: list[tuple[str, str, str]] = []
    # Request users are even indices; each has a following next-user turn.
    for i in range(0, len(users) - 1, 2):
        u = users[i]
        nxt_asst = next(a for a in assistants if a["seq"] > u["seq"])
        windows.append((u["id"], nxt_asst["id"], u["content_hash"]))
    repo.replace_exchange_windows(sid, windows)
    conn.commit()
    rows = conn.execute(
        "SELECT id FROM exchange_windows WHERE session_id = ? ORDER BY id",
        (sid,),
    ).fetchall()
    return [r["id"] for r in rows]


def _valid_result(window_id: str, *, quote: str, role: str = "next_user") -> dict:
    return {
        "window_id": window_id,
        "turn_kind": ["redirect_or_brake"],
        "user_stance": "redirecting",
        "agent_stance": "executing",
        "prior_outcome": "partial_accept",
        "flags": {
            "premature_action_called_out": True,
            "scope_expansion": False,
            "scope_narrowing": False,
            "multi_agent_reference": False,
            "instruction_violation_alleged": False,
            "verification_requested": False,
            "usage_or_api_limit": False,
        },
        "spans": [
            {
                "role": role,
                "quote": quote,
                "supports": ["redirect_or_brake"],
            }
        ],
        "confidence": {"user_stance": 0.8, "agent_stance": 0.5, "prior_outcome": 0.4},
        "abstain_reasons": [],
        "novel_observations": [],
    }


class PackSizingTests(unittest.TestCase):
    def test_default_and_char_budget(self) -> None:
        small = [
            {"window_id": f"s{i}", "user": "x" * 100, "assistant": "y" * 100, "next_user": "z" * 100}
            for i in range(9)
        ]
        groups = pack_windows(small, windows_per_packet=4, max_chars_per_packet=28_000)
        self.assertEqual(DEFAULT_WINDOWS_PER_PACKET, 4)
        self.assertEqual(len(groups), 3)
        self.assertEqual([len(g) for g in groups], [4, 4, 1])

    def test_large_window_singleton(self) -> None:
        payloads = [
            {"window_id": "tiny", "user": "hi", "assistant": "yo", "next_user": "ok"},
            {
                "window_id": "huge",
                "user": "u" * 5000,
                "assistant": "a" * 5000,
                "next_user": "n" * 3000,
            },
            {"window_id": "tiny2", "user": "hi", "assistant": "yo", "next_user": "ok"},
        ]
        groups = pack_windows(payloads, windows_per_packet=4, max_chars_per_packet=28_000)
        self.assertEqual(len(groups[1]), 1)
        self.assertEqual(groups[1][0]["window_id"], "huge")


class ValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.payload = {
            "window_id": "w1",
            "user": "please proceed carefully",
            "assistant": "Pause — this duplicates infrastructure we agreed on.",
            "next_user": "dont jump on scaffolding yet",
        }
        self.allowed = {"w1"}

    def test_accepts_valid(self) -> None:
        raw = _valid_result("w1", quote="dont jump on scaffolding yet")
        ok, failures = validate_window_result(
            raw, payload=self.payload, allowed_ids=self.allowed
        )
        self.assertIsNotNone(ok)
        self.assertEqual(failures, [])

    def test_rejects_unknown_label(self) -> None:
        raw = _valid_result("w1", quote="dont jump on scaffolding yet")
        raw["turn_kind"] = ["not_a_real_label"]
        ok, failures = validate_window_result(
            raw, payload=self.payload, allowed_ids=self.allowed
        )
        self.assertIsNone(ok)
        self.assertTrue(any("unknown_turn_kind" in f.reason for f in failures))

    def test_rejects_unknown_window_id(self) -> None:
        raw = _valid_result("other", quote="dont jump on scaffolding yet")
        ok, failures = validate_window_result(
            raw, payload=self.payload, allowed_ids=self.allowed
        )
        self.assertIsNone(ok)
        self.assertTrue(any(f.reason == "unknown_window_id" for f in failures))

    def test_rejects_missing_required_field(self) -> None:
        raw = _valid_result("w1", quote="dont jump on scaffolding yet")
        del raw["confidence"]
        ok, failures = validate_window_result(
            raw, payload=self.payload, allowed_ids=self.allowed
        )
        self.assertIsNone(ok)
        self.assertTrue(any("missing_required_field:confidence" in f.reason for f in failures))

    def test_rejects_fabricated_evidence_quote(self) -> None:
        raw = _valid_result("w1", quote="this quote was never said by anyone")
        ok, failures = validate_window_result(
            raw, payload=self.payload, allowed_ids=self.allowed
        )
        self.assertIsNone(ok)
        self.assertTrue(
            any(f.reason == "evidence_quote_not_in_source" for f in failures)
        )


class PacketRoundTripTests(unittest.TestCase):
    def test_emit_ingest_round_trip_and_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.db"
            conn = connect(db)
            init_db(conn)
            window_ids = _seed_ux_windows(conn, n=5)
            run_dir = Path(tmp) / "run"
            manifest = emit_packet_run(
                conn,
                run_dir,
                windows_per_packet=2,
                max_chars_per_packet=28_000,
                model="grok-4.5-test",
            )
            self.assertEqual(manifest["provider"], PacketExtractionProvider.name)
            self.assertGreaterEqual(manifest["packet_count"], 1)
            # Resume emit is idempotent.
            again = emit_packet_run(conn, run_dir, resume=True)
            self.assertEqual(again["run_id"], manifest["run_id"])

            packet_ids = sorted(manifest["packets"])
            first = packet_ids[0]
            packet = json.loads(
                (run_dir / manifest["packets"][first]["packet_path"]).read_text()
            )
            windows = packet["windows"]
            results = []
            for w in windows:
                quote = w["next_user"][:40]
                # use a substring that exists
                self.assertTrue(quote)
                results.append(_valid_result(w["window_id"], quote=quote))
            inbox = run_dir / "results_inbox"
            inbox.mkdir(parents=True, exist_ok=True)
            result_path = inbox / f"{first}.json"
            result_path.write_text(
                json.dumps({"packet_id": first, "windows": results}),
                encoding="utf-8",
            )

            out = ingest_packet_results(conn, run_dir, model="grok-4.5-test")
            completed = [r for r in out if r.status == "completed" and r.accepted]
            self.assertEqual(len(completed), 1)
            obs = load_ux_observations(conn, manifest["db_run_id"])
            self.assertEqual(len(obs), len(windows))
            self.assertEqual(obs[0]["model"], "grok-4.5-test")
            raw = json.loads(obs[0]["raw_json"])
            self.assertEqual(raw["extractor"]["packet_id"], first)
            self.assertEqual(raw["extractor"]["provider"], "packet")

            # Interrupt/resume: second ingest does not double-write.
            out2 = ingest_packet_results(conn, run_dir, model="grok-4.5-test")
            self.assertTrue(
                all(
                    r.status == "completed" and not r.accepted
                    for r in out2
                    if r.packet_id == first
                )
            )
            obs2 = load_ux_observations(conn, manifest["db_run_id"])
            self.assertEqual(len(obs2), len(windows))

            status = packet_run_status(run_dir)
            self.assertEqual(status["status_counts"].get("completed"), 1)
            self.assertIn("pending", status["status_counts"])

            # Remaining packets stay pending — simulate finishing one more after "interrupt".
            if len(packet_ids) > 1:
                second = packet_ids[1]
                packet2 = json.loads(
                    (run_dir / manifest["packets"][second]["packet_path"]).read_text()
                )
                results2 = [
                    _valid_result(w["window_id"], quote=w["next_user"][:40])
                    for w in packet2["windows"]
                ]
                (inbox / f"{second}.json").write_text(
                    json.dumps({"packet_id": second, "windows": results2}),
                    encoding="utf-8",
                )
                ingest_packet_results(conn, run_dir, model="grok-4.5-test")
                status2 = packet_run_status(run_dir)
                self.assertGreaterEqual(status2["status_counts"].get("completed", 0), 2)

            # Reject path reports fabricated quote.
            if len(packet_ids) > 2:
                third = packet_ids[2]
                packet3 = json.loads(
                    (run_dir / manifest["packets"][third]["packet_path"]).read_text()
                )
                bad = [
                    _valid_result(w["window_id"], quote="FABRICATED_EVIDENCE_QUOTE_XYZ")
                    for w in packet3["windows"]
                ]
                bad_path = inbox / f"{third}.json"
                bad_path.write_text(
                    json.dumps({"packet_id": third, "windows": bad}),
                    encoding="utf-8",
                )
                rejected = ingest_packet_result(
                    run_dir, third, bad_path, conn=conn, model="grok-4.5-test"
                )
                self.assertEqual(rejected.status, "rejected")
                self.assertTrue(
                    any(f.reason == "evidence_quote_not_in_source" for f in rejected.failures)
                )
                self.assertTrue((run_dir / "rejects" / f"{third}.json").exists())

            self.assertGreaterEqual(len(window_ids), 1)


class SkipLabeledEmitTests(unittest.TestCase):
    def test_skip_labeled_excludes_linked_windows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.db"
            conn = connect(db)
            init_db(conn)
            _seed_ux_windows(conn, n=5)

            baseline = emit_packet_run(
                conn,
                Path(tmp) / "run_all",
                windows_per_packet=2,
                model="grok-4.5-test",
            )
            all_ids = {
                wid
                for meta in baseline["packets"].values()
                for wid in meta["window_ids"]
            }
            self.assertGreaterEqual(len(all_ids), 2)

            labeled = sorted(all_ids)[0]
            write_ux_observations(
                conn,
                baseline["db_run_id"],
                [
                    raw_to_observation(
                        _valid_result(labeled, quote="anything"),
                        model="grok-4.5-test",
                        prompt_hash="deadbeef",
                        packet_id="pkt_0001",
                    )
                ],
            )
            self.assertEqual(labeled_window_ids(conn), {labeled})

            filtered = emit_packet_run(
                conn,
                Path(tmp) / "run_unlabeled",
                windows_per_packet=2,
                model="grok-4.5-test",
                skip_labeled=True,
            )
            remaining = {
                wid
                for meta in filtered["packets"].values()
                for wid in meta["window_ids"]
            }
            self.assertNotIn(labeled, remaining)
            self.assertEqual(remaining, all_ids - {labeled})
            self.assertEqual(filtered["window_count"], len(all_ids) - 1)

    def test_orphaned_labels_are_not_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.db"
            conn = connect(db)
            init_db(conn)
            _seed_ux_windows(conn, n=3)
            baseline = emit_packet_run(
                conn,
                Path(tmp) / "run_all",
                windows_per_packet=2,
                model="grok-4.5-test",
            )
            all_ids = {
                wid
                for meta in baseline["packets"].values()
                for wid in meta["window_ids"]
            }
            target = sorted(all_ids)[0]
            write_ux_observations(
                conn,
                baseline["db_run_id"],
                [
                    raw_to_observation(
                        _valid_result(target, quote="anything"),
                        model="grok-4.5-test",
                        prompt_hash="deadbeef",
                        packet_id="pkt_0001",
                    )
                ],
            )
            conn.execute(
                "UPDATE ux_observations SET link_status = 'orphaned' WHERE window_id = ?",
                (target,),
            )
            conn.commit()
            self.assertEqual(labeled_window_ids(conn), set())

            filtered = emit_packet_run(
                conn,
                Path(tmp) / "run_unlabeled",
                windows_per_packet=2,
                model="grok-4.5-test",
                skip_labeled=True,
            )
            remaining = {
                wid
                for meta in filtered["packets"].values()
                for wid in meta["window_ids"]
            }
            self.assertIn(target, remaining)


if __name__ == "__main__":
    unittest.main()
