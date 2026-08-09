from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agentlog.analysis.extractors.audit import (
    compare_batch_vs_single,
    emit_audit_pack,
    evaluate_gate,
    load_gold,
    score_predictions,
    stratified_sample,
)
from agentlog.analysis.extractors.llm_client import ScriptedChatClient
from agentlog.analysis.extractors.models import ExtractorMeta, UxObservation, WindowContext
from agentlog.analysis.extractors.ux_extractor import UxExtractor


def _obs(wid: str, kinds: list[str], **kwargs) -> UxObservation:
    return UxObservation(
        window_id=wid,
        extractor=ExtractorMeta(name="ux_v1", version="0.1.0", model="grok-4.5", prompt_hash="x"),
        turn_kind=kinds,
        user_stance=kwargs.get("user_stance"),
        agent_stance=kwargs.get("agent_stance"),
        prior_outcome=kwargs.get("prior_outcome"),
        abstain_reasons=kwargs.get("abstain_reasons", []),
    )


class AuditScoringTests(unittest.TestCase):
    def test_precision_recall_per_label(self) -> None:
        gold = {
            "a": {"turn_kind": ["redirect_or_brake"]},
            "b": {"turn_kind": ["redirect_or_brake"]},
            "c": {"turn_kind": ["correction"]},
            "d": {"turn_kind": ["soft_approval"]},
        }
        preds = [
            _obs("a", ["redirect_or_brake"]),
            _obs("b", ["human_followup"]),  # FN for redirect
            _obs("c", ["correction"], user_stance="correcting"),
            _obs("d", ["redirect_or_brake"]),  # FP for redirect
        ]
        scores = score_predictions(preds, gold, labels=["redirect_or_brake", "correction"])
        self.assertEqual(scores["redirect_or_brake"].tp, 1)
        self.assertEqual(scores["redirect_or_brake"].fp, 1)
        self.assertEqual(scores["redirect_or_brake"].fn, 1)
        self.assertAlmostEqual(scores["redirect_or_brake"].precision or 0, 0.5)
        self.assertAlmostEqual(scores["redirect_or_brake"].recall or 0, 0.5)
        self.assertEqual(scores["correction"].tp, 1)

    def test_gate_fails_on_low_precision(self) -> None:
        gold = {"a": {"turn_kind": ["redirect_or_brake"]}, "b": {"turn_kind": []}}
        preds = [
            _obs("a", ["redirect_or_brake"]),
            _obs("b", ["redirect_or_brake"]),
        ]
        scores = score_predictions(preds, gold, labels=["redirect_or_brake"])
        gate = evaluate_gate(scores, batch_disagreement_rate=0.0)
        self.assertFalse(gate.passed)
        self.assertTrue(any("redirect_or_brake precision" in f for f in gate.failures))

    def test_batch_disagreement_measured(self) -> None:
        # Contaminating batched responder: second window in a batch flips label.
        state = {"calls": 0}

        def responder(*, system: str, user: str, model: str):
            state["calls"] += 1
            # Count windows in the prompt.
            n = user.count("<window ")
            if n == 1:
                wid = "w1" if "w1" in user else ("w2" if "w2" in user else "w3")
                return {
                    "window_id": wid,
                    "turn_kind": ["human_followup"],
                    "user_stance": "neutral",
                    "agent_stance": "executing",
                    "prior_outcome": "abstain",
                    "flags": {},
                    "spans": [],
                    "confidence": {},
                    "abstain_reasons": [],
                    "novel_observations": [],
                }
            # Batched: contaminate w2
            return {
                "windows": [
                    {
                        "window_id": "w1",
                        "turn_kind": ["human_followup"],
                        "user_stance": "neutral",
                        "agent_stance": "executing",
                        "prior_outcome": "abstain",
                        "flags": {},
                        "spans": [],
                        "confidence": {},
                        "abstain_reasons": [],
                        "novel_observations": [],
                    },
                    {
                        "window_id": "w2",
                        "turn_kind": ["correction"],  # bleed
                        "user_stance": "correcting",
                        "agent_stance": "executing",
                        "prior_outcome": "abstain",
                        "flags": {},
                        "spans": [],
                        "confidence": {},
                        "abstain_reasons": [],
                        "novel_observations": [],
                    },
                    {
                        "window_id": "w3",
                        "turn_kind": ["human_followup"],
                        "user_stance": "neutral",
                        "agent_stance": "executing",
                        "prior_outcome": "abstain",
                        "flags": {},
                        "spans": [],
                        "confidence": {},
                        "abstain_reasons": [],
                        "novel_observations": [],
                    },
                ]
            }

        ext = UxExtractor(client=ScriptedChatClient(responder), batch_size=1)
        contexts = [
            WindowContext(
                window_id=f"w{i}",
                session_id="s",
                harness="codex",
                request_text="please continue the refactor",
                assistant_text="Working on it.",
            )
            for i in (1, 2, 3)
        ]
        rate, diffs = compare_batch_vs_single(ext, contexts, batch_size=3)
        self.assertGreater(rate, 0.0)
        self.assertTrue(any(d["window_id"] == "w2" for d in diffs))
        gate = evaluate_gate({}, batch_disagreement_rate=rate)
        self.assertEqual(gate.recommended_batch_size, 1)

    def test_emit_and_load_gold(self) -> None:
        contexts = [
            WindowContext(
                window_id=f"{h}-{i}",
                session_id="s",
                harness=h,
                request_text=f"task {i}",
            )
            for h in ("codex", "claude", "cursor")
            for i in range(5)
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pack.jsonl"
            sample = emit_audit_pack(contexts, path, n=6, seed=1)
            self.assertEqual(len(sample), 6)
            lines = path.read_text().strip().splitlines()
            self.assertEqual(len(lines), 6)
            # Simulate hand labels on first row
            row = json.loads(lines[0])
            row["label_status"] = "labeled"
            row["labels"] = {"turn_kind": ["redirect_or_brake"], "user_stance": "redirecting"}
            gold_path = Path(tmp) / "gold.jsonl"
            gold_path.write_text(json.dumps(row) + "\n", encoding="utf-8")
            gold = load_gold(gold_path)
            self.assertEqual(len(gold), 1)
            self.assertIn("redirect_or_brake", gold[row["window_id"]]["turn_kind"])

    def test_stratified_sample_covers_harnesses(self) -> None:
        contexts = [
            WindowContext(window_id=f"{h}-{i}", session_id="s", harness=h, request_text="x")
            for h in ("codex", "claude", "cursor")
            for i in range(20)
        ]
        sample = stratified_sample(contexts, n=12, seed=0)
        harnesses = {c.harness for c in sample}
        self.assertEqual(harnesses, {"codex", "claude", "cursor"})


if __name__ == "__main__":
    unittest.main()
