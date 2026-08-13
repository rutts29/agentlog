from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agentlog.analysis.owner_notes import (
    collect_owner_turns,
    collect_packet_dir_turns,
    compact_turns,
    validate_owner_items,
    write_owner_fact_packet,
)


class OwnerNotesTests(unittest.TestCase):
    def test_collects_user_turns_from_a_coach_packet(self) -> None:
        turns = collect_owner_turns(
            {
                "packet_id": "cpkt_0001",
                "windows": [
                    {
                        "session_id": "codex:abc",
                        "harness": "codex",
                        "user": "do not start yet",
                        "request": {"seq": 3},
                    },
                    {"session_id": "codex:abc", "user": ""},
                ],
            }
        )
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["seq"], 3)
        self.assertIn("do not start yet", turns[0]["user"])

    def test_validate_and_write_fact_packet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "facts.json"
            payload = write_owner_fact_packet(
                path,
                run_id="owner-test",
                items=[
                    {
                        "session_id": "codex:abc",
                        "message_seq": 3,
                        "kind": "how_you_brief",
                        "title": "Say the use first",
                        "body": "Lead with the decision you will make.",
                        "quote": "do not start yet",
                        "does_not_prove": "That every brief must be long.",
                    }
                ],
            )
            self.assertEqual(payload["run_id"], "owner-test")
            self.assertEqual(len(payload["items"]), 1)
            loaded = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(loaded["items"][0]["quote"], "do not start yet")

    def test_rejects_missing_fields(self) -> None:
        with self.assertRaisesRegex(ValueError, "missing title"):
            validate_owner_items(
                [
                    {
                        "session_id": "codex:abc",
                        "kind": "x",
                        "title": "",
                        "body": "b",
                        "quote": "q",
                        "does_not_prove": "d",
                    }
                ]
            )

    def test_collects_from_a_packet_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "cpkt_0001.json").write_text(
                json.dumps(
                    {
                        "packet_id": "cpkt_0001",
                        "windows": [
                            {
                                "session_id": "cursor:one",
                                "harness": "cursor",
                                "user": "lets make it for ourselves",
                                "request": {"seq": 50},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            turns = collect_packet_dir_turns(folder)
            self.assertEqual(len(turns), 1)
            digest = compact_turns(turns)
            self.assertIn("lets make it for ourselves", digest)


if __name__ == "__main__":
    unittest.main()
