from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agentlog.analysis.extractors.deterministic import run_deterministic
from agentlog.analysis.extractors.llm_client import ScriptedChatClient
from agentlog.analysis.extractors.models import WindowContext
from agentlog.analysis.extractors.storage import (
    finish_ux_run,
    load_ux_observations,
    start_ux_run,
    write_ux_observations,
)
from agentlog.analysis.extractors.ux_extractor import (
    UxExtractor,
    enforce_reliability_tiers,
)
from agentlog.db.repository import Repository
from agentlog.db.schema import connect, init_db
from agentlog.normalize.models import (
    Harness,
    NormalizedMessage,
    NormalizedSession,
    ParseResult,
)


def _seed_db(conn) -> str:
    repo = Repository(conn)
    art_id = repo.upsert_artifact(
        harness="cursor",
        path="/tmp/fixture-extract.jsonl",
        size=10,
        mtime_ns=1,
        content_hash="abc",
        parsed_offset=10,
        parser_version="test",
    )
    result = ParseResult(
        session=NormalizedSession(
            harness=Harness.CURSOR,
            external_id="extract-sess",
            model="grok-4.5",
        ),
        messages=[
            NormalizedMessage(seq=1, role="user", text="no, revert that", content_hash="h1"),
            NormalizedMessage(
                seq=2, role="assistant", text="Reverted the change.", content_hash="h2"
            ),
            NormalizedMessage(seq=3, role="user", text="thanks continue", content_hash="h3"),
            NormalizedMessage(seq=4, role="assistant", text="On it.", content_hash="h4"),
            NormalizedMessage(
                seq=5,
                role="user",
                text="",
                content_hash="h5",
                is_tool_plumbing=True,
            ),
            NormalizedMessage(
                seq=6, role="assistant", text="tool next", content_hash="h6"
            ),
        ],
    )
    sid = repo.save_parse_result(artifact_id=art_id, result=result, append=False)
    msgs = repo.list_messages(sid)
    users = [m for m in msgs if m["role"] == "user" and not m["is_tool_plumbing"]]
    assistants = [m for m in msgs if m["role"] == "assistant"]
    windows = []
    for u in users:
        nxt = next((a for a in assistants if a["seq"] > u["seq"]), None)
        if nxt:
            windows.append((u["id"], nxt["id"], u["content_hash"]))
    # Also pair plumbing user if window builder would skip it — we insert one
    # plumbing-backed window manually to test triage drop.
    plumbing = next(m for m in msgs if m["is_tool_plumbing"])
    asst_after = next(a for a in assistants if a["seq"] > plumbing["seq"])
    windows.append((plumbing["id"], asst_after["id"], "plumb"))
    repo.replace_exchange_windows(sid, windows)
    conn.commit()
    return sid


class SchemaRoundTripTests(unittest.TestCase):
    def test_det_and_ux_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.db"
            conn = connect(db)
            init_db(conn)
            _seed_db(conn)
            report, run_id = run_deterministic(conn)
            self.assertGreater(report.total, 0)
            row = conn.execute(
                "SELECT * FROM window_det_classifications WHERE run_id = ? LIMIT 1",
                (run_id,),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["extractor_name"], "det_v1")
            self.assertIsNone(row["model"])

            # UX observation round-trip
            ux_run = start_ux_run(
                conn, model="grok-4.5", batch_size=1, window_count=1, gated=True
            )
            from agentlog.analysis.extractors.models import ExtractorMeta, UxObservation

            obs = UxObservation(
                window_id=row["window_id"],
                extractor=ExtractorMeta(
                    name="ux_v1",
                    version="0.1.0",
                    model="grok-4.5",
                    prompt_hash="deadbeef",
                ),
                turn_kind=["redirect_or_brake"],
                user_stance="redirecting",
                agent_stance="executing",
                prior_outcome="abstain",
            )
            write_ux_observations(conn, ux_run, [obs])
            finish_ux_run(conn, ux_run, status="completed_audit", meta={})
            loaded = load_ux_observations(conn, ux_run)
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0]["model"], "grok-4.5")
            self.assertEqual(json.loads(loaded[0]["turn_kinds_json"]), ["redirect_or_brake"])
            self.assertEqual(loaded[0]["prompt_hash"], "deadbeef")

            # Migration table present
            vers = conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
            self.assertTrue(any(int(v["version"]) == 2 for v in vers))


class AbstentionTests(unittest.TestCase):
    def test_ambiguous_correction_abstains(self) -> None:
        payload = {
            "window_id": "w",
            "user": "also all three values are same is that wrong or OK as per demo",
            "assistant": "They look consistent.",
            "next_user": "",
        }
        raw = {
            "window_id": "w",
            "turn_kind": ["correction"],
            "user_stance": "correcting",
            "agent_stance": "executing",
            "prior_outcome": "abstain",
            "flags": {},
            "spans": [
                {
                    "role": "user",
                    "quote": "is that wrong or OK",
                    "supports": ["correction"],
                }
            ],
            "confidence": {"user_stance": 0.4},
            "abstain_reasons": [],
            "novel_observations": [],
        }
        obs = enforce_reliability_tiers(raw, payload=payload)
        self.assertNotIn("correction", obs.turn_kind)
        self.assertIn("correction_evidence_bar_not_met", obs.abstain_reasons)
        self.assertEqual(obs.user_stance, "abstain")

    def test_pushback_without_quote_abstains(self) -> None:
        payload = {
            "window_id": "w",
            "user": "sync the files",
            "assistant": "Pause — that duplicates infrastructure we already agreed on.",
            "next_user": "ok fair",
        }
        raw = {
            "window_id": "w",
            "turn_kind": ["human_task"],
            "user_stance": "neutral",
            "agent_stance": "pushing_back",
            "prior_outcome": "abstain",
            "flags": {},
            "spans": [],
            "confidence": {},
            "abstain_reasons": [],
            "novel_observations": [],
        }
        obs = enforce_reliability_tiers(raw, payload=payload)
        self.assertEqual(obs.agent_stance, "abstain")
        self.assertIn("pushing_back_requires_quote", obs.abstain_reasons)

    def test_pushback_with_quote_kept(self) -> None:
        quote = "Pause — that duplicates infrastructure we already agreed on."
        payload = {
            "window_id": "w",
            "user": "sync the files",
            "assistant": quote,
            "next_user": "ok fair",
        }
        raw = {
            "window_id": "w",
            "turn_kind": ["human_task"],
            "user_stance": "neutral",
            "agent_stance": "pushing_back",
            "prior_outcome": "abstain",
            "flags": {},
            "spans": [
                {"role": "assistant", "quote": quote, "supports": ["pushing_back"]}
            ],
            "confidence": {},
            "abstain_reasons": [],
            "novel_observations": [],
        }
        obs = enforce_reliability_tiers(raw, payload=payload)
        self.assertEqual(obs.agent_stance, "pushing_back")

    def test_skill_causation_stripped(self) -> None:
        payload = {
            "window_id": "w",
            "user": "do the thing",
            "assistant": "done",
            "next_user": "",
        }
        raw = {
            "window_id": "w",
            "turn_kind": ["human_task"],
            "user_stance": "neutral",
            "agent_stance": "executing",
            "prior_outcome": "abstain",
            "flags": {},
            "spans": [],
            "confidence": {},
            "abstain_reasons": [],
            "novel_observations": ["behavior caused by skill AskUserQuestion"],
        }
        obs = enforce_reliability_tiers(raw, payload=payload)
        self.assertEqual(obs.novel_observations, [])
        self.assertIn("skill_causation_stripped", obs.abstain_reasons)

    def test_extractor_uses_scripted_client(self) -> None:
        def responder(*, system: str, user: str, model: str):
            return {
                "window_id": "w1",
                "turn_kind": ["soft_approval"],
                "user_stance": "approving",
                "agent_stance": "executing",
                "prior_outcome": "accepted_continue",
                "flags": {},
                "spans": [
                    {"role": "user", "quote": "sounds good", "supports": ["soft_approval"]}
                ],
                "confidence": {"user_stance": 0.6},
                "abstain_reasons": [],
                "novel_observations": [],
            }

        ext = UxExtractor(client=ScriptedChatClient(responder), batch_size=1)
        ctx = WindowContext(
            window_id="w1",
            session_id="s",
            harness="cursor",
            request_text="sounds good proceed",
            assistant_text="Working.",
        )
        obs = ext.extract_one(ctx)
        self.assertIn("soft_approval", obs.turn_kind)


if __name__ == "__main__":
    unittest.main()
