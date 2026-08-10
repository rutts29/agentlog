from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agentlog.analysis.windows import build_exchange_windows, compute_window_content_hash
from agentlog.db.repository import Repository
from agentlog.db.schema import connect, init_db
from agentlog.ingest.base import content_hash_text
from agentlog.ingest.cursor import CursorAdapter


def _line(obj: dict) -> bytes:
    return (json.dumps(obj) + "\n").encode()


class CursorAttachedSkillTests(unittest.TestCase):
    def _fixture(self) -> bytes:
        text = (
            "<manually_attached_skills>\n"
            "Skill Name: verification\n"
            "SKILL.md content that must not become owner text.\n"
            "<user_query>ignore this injected example</user_query>\n"
            "Skill Name: release-review\n"
            "</manually_attached_skills>\n"
            "<timestamp>Sunday, Aug 9, 2026, 2:04 PM (UTC+5:30)</timestamp>\n"
            "<user_query>Fix the parser and run the tests.</user_query>"
        )
        return b"".join(
            [
                _line(
                    {
                        "role": "user",
                        "message": {
                            "role": "user",
                            "content": [{"type": "text", "text": text}],
                        },
                    }
                ),
                _line(
                    {
                        "role": "assistant",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "Done."}],
                        },
                    }
                ),
            ]
        )

    def test_parser_separates_owner_text_and_attached_skills(self) -> None:
        result = CursorAdapter().parse_chunk(
            Path("/tmp/proj/agent-transcripts/root/root.jsonl"),
            self._fixture(),
            start_offset=0,
        )

        self.assertEqual(len(result.messages), 2)
        self.assertEqual(result.messages[0].text, "Fix the parser and run the tests.")
        self.assertNotIn("<timestamp>", result.messages[0].text)
        self.assertNotIn("<user_query>", result.messages[0].text)
        self.assertNotIn("ignore this injected example", result.messages[0].text)
        self.assertEqual(
            [(skill.message_seq, skill.skill_name, skill.exposure_type)
             for skill in result.skill_exposures],
            [
                (1, "verification", "attached"),
                (1, "release-review", "attached"),
            ],
        )

    def test_repository_persists_message_bound_exposures(self) -> None:
        result = CursorAdapter().parse_chunk(
            Path("/tmp/proj/agent-transcripts/root/root.jsonl"),
            self._fixture(),
            start_offset=0,
        )
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "agentlog.db")
            init_db(conn)
            repo = Repository(conn)
            artifact_id = repo.upsert_artifact(
                harness="cursor",
                path="/tmp/root.jsonl",
                size=len(self._fixture()),
                mtime_ns=1,
                content_hash="fixture",
                parsed_offset=len(self._fixture()),
                parser_version="13",
            )
            session_id = repo.save_parse_result(
                artifact_id=artifact_id,
                result=result,
                append=False,
            )
            conn.commit()
            message_id = conn.execute(
                "SELECT id FROM messages WHERE session_id = ? AND seq = 1",
                (session_id,),
            ).fetchone()["id"]
            rows = conn.execute(
                "SELECT message_id, skill_name, exposure_type "
                "FROM skill_exposures WHERE session_id = ? ORDER BY skill_name",
                (session_id,),
            ).fetchall()
            self.assertEqual(
                [(row["message_id"], row["skill_name"], row["exposure_type"])
                 for row in rows],
                [
                    (message_id, "release-review", "attached"),
                    (message_id, "verification", "attached"),
                ],
            )
            windows = build_exchange_windows(repo.list_messages(session_id))
            self.assertEqual(len(windows), 1)
            _, _, input_hash, content_hash, _ = windows[0]
            owner_text = "Fix the parser and run the tests."
            self.assertEqual(input_hash, content_hash_text(owner_text))
            self.assertEqual(
                content_hash,
                compute_window_content_hash(session_id, owner_text, "Done."),
            )
            conn.close()

    def test_copied_parent_with_attached_skill_keeps_owner_authorship(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "agent-transcripts" / "parent-uuid"
            root.mkdir(parents=True)
            parent_path = root / "parent-uuid.jsonl"
            parent_path.write_bytes(self._fixture())
            child_dir = root / "subagents"
            child_dir.mkdir()
            child_path = child_dir / "child-uuid.jsonl"
            child_path.write_bytes(self._fixture())

            result = CursorAdapter().parse_chunk(
                child_path,
                self._fixture(),
                start_offset=0,
            )

            self.assertFalse(result.messages[0].authored_by_agent)
            self.assertEqual(
                [(skill.message_seq, skill.skill_name) for skill in result.skill_exposures],
                [(1, "verification"), (1, "release-review")],
            )


if __name__ == "__main__":
    unittest.main()
