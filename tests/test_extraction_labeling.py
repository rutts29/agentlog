from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from agentlog.analysis.extractors.audit import load_gold
from agentlog.analysis.extractors.labeling import (
    KEY_MAP,
    LabelSession,
    apply_label_key,
    empty_labels,
    run_labeling_loop,
)


def _pack_row(wid: str, *, harness: str = "cursor") -> dict:
    return {
        "window_id": wid,
        "harness": harness,
        "session_id": "s1",
        "payload": {
            "window_id": wid,
            "harness": harness,
            "user": f"user request for {wid}",
            "assistant": f"agent did things for {wid}",
            "next_user": f"next message for {wid}",
            "tool_timeline": ["Read|read|?"],
        },
        "labels": empty_labels(),
        "label_status": "unlabeled",
    }


class LabelKeyMapTests(unittest.TestCase):
    def test_keymap_covers_gate_labels_and_abstain(self) -> None:
        self.assertEqual(KEY_MAP["r"], "redirect_or_brake")
        self.assertEqual(KEY_MAP["c"], "correction")
        self.assertEqual(KEY_MAP["s"], "soft_approval")
        self.assertEqual(KEY_MAP["d"], "dont_act_yet")
        self.assertEqual(KEY_MAP["p"], "pushing_back")
        self.assertEqual(KEY_MAP["u"], "abstain")

    def test_apply_label_key(self) -> None:
        labs = apply_label_key(empty_labels(), "r")
        self.assertIn("redirect_or_brake", labs["turn_kind"])
        labs_u = apply_label_key(empty_labels(), "u")
        self.assertEqual(labs_u["user_stance"], "abstain")
        self.assertEqual(labs_u["turn_kind"], [])
        labs_p = apply_label_key(empty_labels(), "p")
        self.assertEqual(labs_p["agent_stance"], "pushing_back")


class LabelSessionTests(unittest.TestCase):
    def test_save_resume_and_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pack = Path(tmp) / "pack.jsonl"
            gold = Path(tmp) / "gold.jsonl"
            rows = [_pack_row(f"w{i}") for i in range(5)]
            pack.write_text(
                "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
            )

            session = LabelSession.open(pack, gold)
            self.assertEqual(session.index, 0)
            session.apply_and_advance("r")
            self.assertEqual(session.index, 1)
            session.apply_and_advance("c")
            self.assertEqual(session.labeled_count, 2)

            # Quit and resume — should land on first unlabeled (index 2).
            session2 = LabelSession.open(pack, gold)
            self.assertEqual(session2.index, 2)
            self.assertEqual(session2.labeled_count, 2)

            # Back navigation to change previous label.
            session2.go_back()
            self.assertEqual(session2.index, 1)
            session2.apply_and_advance("u")
            g = load_gold(gold)
            self.assertEqual(g["w1"]["user_stance"], "abstain")
            self.assertEqual(g["w1"]["turn_kind"], [])
            # w0 still redirect
            self.assertIn("redirect_or_brake", g["w0"]["turn_kind"])

            # Gold format consumable by audit harness (skips unlabeled).
            self.assertEqual(len(g), 2)

    def test_note_and_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pack = Path(tmp) / "pack.jsonl"
            gold = Path(tmp) / "gold.jsonl"
            rows = [_pack_row("a"), _pack_row("b"), _pack_row("c")]
            pack.write_text(
                "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
            )

            keys = iter(["n", "r", "b", "s", "q"])
            lines = iter(["taxonomy gap: sounds like scheduling\n"])

            out = io.StringIO()
            session = run_labeling_loop(
                pack,
                gold,
                stdout=out,
                read_key_fn=lambda: next(keys),
                read_line_fn=lambda: next(lines),
            )
            self.assertGreaterEqual(session.labeled_count, 1)
            # After note on a, label r advances; back to a; relabel soft_approval; quit.
            g = json.loads(gold.read_text().strip().splitlines()[0])
            self.assertEqual(g["label_status"], "labeled")
            self.assertIn("soft_approval", g["labels"]["turn_kind"])
            self.assertIn("taxonomy gap", g["labels"]["notes"])
            self.assertNotIn("predicted", out.getvalue().lower())
            self.assertNotIn("model label", out.getvalue().lower())


if __name__ == "__main__":
    unittest.main()
