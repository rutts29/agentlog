from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agentlog.db.repository import Repository
from agentlog.db.schema import connect, init_db
from agentlog.ingest.cursor import CursorAdapter
from agentlog.ingest.pipeline import IngestStats, _ingest_one


def _line(obj: dict) -> bytes:
    return (json.dumps(obj) + "\n").encode()


def _message(role: str, blocks: list[dict]) -> bytes:
    return _line(
        {
            "role": role,
            "message": {
                "role": role,
                "content": blocks,
            },
        }
    )


class CursorSideChatBoundaryTests(unittest.TestCase):
    def test_explicit_boundary_removes_inherited_messages_tools_and_skills(self) -> None:
        inherited_user = _message(
            "user",
            [
                {
                    "type": "text",
                    "text": (
                        "<manually_attached_skills>\n"
                        "Skill Name: inherited-skill\n"
                        "</manually_attached_skills>\n"
                        "<user_query>parent request</user_query>"
                    ),
                }
            ],
        )
        inherited_assistant = _message(
            "assistant",
            [
                {"type": "text", "text": "parent response"},
                {
                    "type": "tool_use",
                    "id": "old-tool",
                    "name": "Shell",
                    "input": {"command": "pwd"},
                },
            ],
        )
        inherited_turn_end = _line({"type": "turn_ended", "status": "success"})
        boundary = _message(
            "user",
            [
                {
                    "type": "text",
                    "text": (
                        "<timestamp>2026-08-12T09:00:00Z</timestamp>\n"
                        "<side_chat_boundary>\n"
                        "Side chat boundary.\n"
                        "Everything before this boundary is inherited history.\n"
                        "</side_chat_boundary>"
                    ),
                }
            ],
        )
        local_user = _message(
            "user",
            [
                {
                    "type": "text",
                    "text": (
                        "<manually_attached_skills>\n"
                        "Skill Name: local-skill\n"
                        "</manually_attached_skills>\n"
                        "<user_query>local request</user_query>"
                    ),
                }
            ],
        )
        local_assistant = _message(
            "assistant",
            [
                {"type": "text", "text": "local response"},
                {
                    "type": "tool_use",
                    "id": "new-tool",
                    "name": "Read",
                    "input": {},
                },
                {
                    "type": "tool_result",
                    "tool_use_id": "new-tool",
                    "content": "ok",
                },
            ],
        )
        data = b"".join(
            [
                inherited_user,
                inherited_assistant,
                inherited_turn_end,
                boundary,
                local_user,
                local_assistant,
            ]
        )
        path = Path(
            "/tmp/proj/agent-transcripts/parent-uuid/subagents/side-chat.jsonl"
        )

        result = CursorAdapter().parse_chunk(path, data, start_offset=0)

        self.assertEqual(
            [(message.seq, message.role, message.text) for message in result.messages],
            [
                (1, "user", "local request"),
                (2, "assistant", "local response"),
            ],
        )
        self.assertFalse(result.messages[0].authored_by_agent)
        self.assertEqual(
            [
                (tool.seq, tool.message_seq, tool.tool_name, tool.action)
                for tool in result.tool_events
            ],
            [
                (1, 2, "Read", "call"),
                (2, 2, "Read", "result"),
            ],
        )
        self.assertEqual(
            [
                (skill.message_seq, skill.skill_name)
                for skill in result.skill_exposures
            ],
            [(1, "local-skill")],
        )
        self.assertEqual(result.extras["inherited_message_count"], 2)
        self.assertEqual(result.extras["inherited_record_count"], 3)
        self.assertEqual(result.extras["fork_context_status"], "trimmed")
        self.assertEqual(
            result.extras["fork_context_boundary"],
            f"cursor:byte:{len(inherited_user + inherited_assistant + inherited_turn_end)}",
        )
        self.assertEqual(result.bytes_consumed, len(data))

    def test_boundary_text_is_not_filtered_outside_subagent_path(self) -> None:
        data = b"".join(
            [
                _message(
                    "user",
                    [{"type": "text", "text": "parent request"}],
                ),
                _message(
                    "user",
                    [
                        {
                            "type": "text",
                            "text": (
                                "<side_chat_boundary>\n"
                                "Side chat boundary.\n"
                                "</side_chat_boundary>"
                            ),
                        }
                    ],
                ),
            ]
        )
        path = Path("/tmp/proj/agent-transcripts/root-uuid/root-uuid.jsonl")

        result = CursorAdapter().parse_chunk(path, data, start_offset=0)

        self.assertEqual(len(result.messages), 2)
        self.assertEqual(result.extras, {})

    def test_ordinary_prompt_mentioning_boundary_is_not_filtered(self) -> None:
        data = _message(
            "user",
            [
                {
                    "type": "text",
                    "text": (
                        "Explain this literal example:\n"
                        "<side_chat_boundary>\n"
                        "Side chat boundary.\n"
                        "</side_chat_boundary>"
                    ),
                }
            ],
        )
        path = Path(
            "/tmp/proj/agent-transcripts/parent-uuid/subagents/side-chat.jsonl"
        )

        result = CursorAdapter().parse_chunk(path, data, start_offset=0)

        self.assertEqual(len(result.messages), 1)
        self.assertEqual(result.extras, {})

    def test_multiple_structural_boundaries_fail_closed(self) -> None:
        boundary = _message(
            "user",
            [
                {
                    "type": "text",
                    "text": (
                        "<side_chat_boundary>\n"
                        "Side chat boundary.\n"
                        "</side_chat_boundary>"
                    ),
                }
            ],
        )
        path = Path(
            "/tmp/proj/agent-transcripts/parent-uuid/subagents/side-chat.jsonl"
        )

        with self.assertRaisesRegex(ValueError, "multiple Cursor side-chat"):
            CursorAdapter().parse_chunk(path, boundary + boundary, start_offset=0)

    def test_boundary_discovered_in_append_requests_full_reparse(self) -> None:
        boundary = _message(
            "user",
            [
                {
                    "type": "text",
                    "text": (
                        "<side_chat_boundary>\n"
                        "Side chat boundary.\n"
                        "</side_chat_boundary>"
                    ),
                }
            ],
        )
        local = _message(
            "user",
            [{"type": "text", "text": "new local request"}],
        )
        path = Path(
            "/tmp/proj/agent-transcripts/parent-uuid/subagents/side-chat.jsonl"
        )

        result = CursorAdapter().parse_chunk(
            path, boundary + local, start_offset=400
        )

        self.assertEqual([message.text for message in result.messages], ["new local request"])
        self.assertTrue(result.extras["requires_full_reparse"])
        self.assertEqual(result.bytes_consumed, 400 + len(boundary + local))

    def test_append_after_existing_boundary_remains_local(self) -> None:
        data = _message(
            "user",
            [{"type": "text", "text": "later local request"}],
        )
        path = Path(
            "/tmp/proj/agent-transcripts/parent-uuid/subagents/side-chat.jsonl"
        )

        result = CursorAdapter().parse_chunk(path, data, start_offset=900)

        self.assertEqual([message.text for message in result.messages], ["later local request"])
        self.assertEqual(result.extras, {})
        self.assertEqual(result.bytes_consumed, 900 + len(data))

    def test_incomplete_trailing_record_keeps_safe_checkpoint(self) -> None:
        boundary = _message(
            "user",
            [
                {
                    "type": "text",
                    "text": (
                        "<side_chat_boundary>\n"
                        "Side chat boundary.\n"
                        "</side_chat_boundary>"
                    ),
                }
            ],
        )
        local = _message(
            "user",
            [{"type": "text", "text": "complete local request"}],
        )
        partial = b'{"role":"assistant","message":'
        path = Path(
            "/tmp/proj/agent-transcripts/parent-uuid/subagents/side-chat.jsonl"
        )

        result = CursorAdapter().parse_chunk(
            path, boundary + local + partial, start_offset=0
        )

        self.assertEqual(result.bytes_consumed, len(boundary + local))
        self.assertEqual([message.text for message in result.messages], ["complete local request"])
        self.assertTrue(any("incomplete trailing line" in warning for warning in result.warnings))

    def test_boundary_first_seen_in_append_does_not_advance_checkpoint(self) -> None:
        inherited = b"".join(
            [
                _message(
                    "user",
                    [{"type": "text", "text": "inherited request"}],
                ),
                _message(
                    "assistant",
                    [{"type": "text", "text": "inherited response"}],
                ),
            ]
        )
        boundary = _message(
            "user",
            [
                {
                    "type": "text",
                    "text": (
                        "<side_chat_boundary>\n"
                        "Side chat boundary.\n"
                        "</side_chat_boundary>"
                    ),
                }
            ],
        )
        local = _message(
            "user",
            [{"type": "text", "text": "local request"}],
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = (
                root
                / "agent-transcripts"
                / "parent-uuid"
                / "subagents"
                / "side-chat.jsonl"
            )
            path.parent.mkdir(parents=True)
            path.write_bytes(inherited)
            conn = connect(root / "agentlog.db")
            init_db(conn)
            repo = Repository(conn)
            adapter = CursorAdapter()
            _ingest_one(repo, adapter, path, IngestStats())
            conn.commit()
            artifact_before = repo.get_artifact_by_path(str(path))
            assert artifact_before is not None
            stored_before = conn.execute(
                "SELECT role, content_hash FROM messages ORDER BY seq"
            ).fetchall()

            path.write_bytes(inherited + boundary + local)
            with self.assertRaisesRegex(
                RuntimeError, "requires an exact full reparse"
            ):
                _ingest_one(repo, adapter, path, IngestStats())
            conn.rollback()

            artifact_after = repo.get_artifact_by_path(str(path))
            assert artifact_after is not None
            stored_after = conn.execute(
                "SELECT role, content_hash FROM messages ORDER BY seq"
            ).fetchall()
            self.assertEqual(
                artifact_after.parsed_offset,
                artifact_before.parsed_offset,
            )
            self.assertEqual(
                [(row["role"], row["content_hash"]) for row in stored_after],
                [(row["role"], row["content_hash"]) for row in stored_before],
            )
            conn.close()


if __name__ == "__main__":
    unittest.main()
