from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agentlog.analysis.extractors.models import WindowContext
from agentlog.analysis.extractors.taxonomy import Route
from agentlog.analysis.extractors.triage import triage_window
from agentlog.db.schema import init_db
from agentlog.ingest.claude import ClaudeAdapter
from agentlog.ingest.codex import CodexAdapter
from agentlog.ingest.cursor import CursorAdapter


def _line(obj: dict) -> bytes:
    return (json.dumps(obj) + "\n").encode()


class CursorSubagentAuthoredTests(unittest.TestCase):
    def test_cursor_synthetic_followup_is_not_human_owned(self) -> None:
        callback = (
            "<timestamp>Sunday, Aug 9, 2026, 1:59 PM (UTC+5:30)</timestamp>\n"
            "<user_query>Perform any necessary follow-up actions in response to "
            "the subagent completion above.</user_query>"
        )
        data = b"".join(
            [
                _line(
                    {
                        "role": "user",
                        "message": {
                            "role": "user",
                            "content": [{"type": "text", "text": callback}],
                        },
                    }
                ),
                _line(
                    {
                        "role": "user",
                        "message": {
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": (
                                        "Why does Cursor say ‘Perform any necessary "
                                        "follow-up actions in response to the subagent "
                                        "completion above.’?"
                                    ),
                                }
                            ],
                        },
                    }
                ),
            ]
        )
        result = CursorAdapter().parse_chunk(
            Path("/tmp/proj/agent-transcripts/root-uuid/root-uuid.jsonl"),
            data,
            start_offset=0,
        )
        self.assertTrue(result.messages[0].authored_by_agent)
        self.assertFalse(result.messages[1].authored_by_agent)

    def test_child_initial_user_flagged(self) -> None:
        data = b"".join(
            [
                _line(
                    {
                        "role": "user",
                        "message": {
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": (
                                        "<timestamp>Sunday, Aug 9, 2026, 6:00 PM "
                                        "(UTC+5:30)</timestamp>\n"
                                        "<user_query>\n"
                                        "You are a builder for the agentlog project.\n"
                                        "</user_query>"
                                    ),
                                }
                            ],
                        },
                    }
                ),
                _line(
                    {
                        "role": "assistant",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "On it."}],
                        },
                    }
                ),
            ]
        )
        path = Path(
            "/tmp/proj/agent-transcripts/parent-uuid/subagents/child-uuid.jsonl"
        )
        result = CursorAdapter().parse_chunk(path, data, start_offset=0)
        self.assertEqual(result.session.parent_session_id, "parent-uuid")
        self.assertEqual(result.session.external_id, "child-uuid")
        users = [m for m in result.messages if m.role == "user"]
        self.assertEqual(len(users), 1)
        self.assertTrue(users[0].authored_by_agent)

    def test_root_session_not_flagged(self) -> None:
        data = _line(
            {
                "role": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "please fix the bug"}],
                },
            }
        )
        path = Path(
            "/tmp/proj/agent-transcripts/parent-uuid/parent-uuid.jsonl"
        )
        result = CursorAdapter().parse_chunk(path, data, start_offset=0)
        self.assertIsNone(result.session.parent_session_id)
        self.assertFalse(result.messages[0].authored_by_agent)

    def test_append_chunk_does_not_flag(self) -> None:
        data = _line(
            {
                "role": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "follow-up"}],
                },
            }
        )
        path = Path(
            "/tmp/proj/agent-transcripts/parent-uuid/subagents/child-uuid.jsonl"
        )
        result = CursorAdapter().parse_chunk(path, data, start_offset=100)
        self.assertFalse(result.messages[0].authored_by_agent)

    def test_side_chat_copying_parent_history_not_flagged(self) -> None:
        shared = (
            "<timestamp>Saturday, Aug 8, 2026, 3:56 PM (UTC+5:30)</timestamp>\n"
            "<user_query>\nhttps://agent-plugins.org/\n</user_query>"
        )
        parent_line = _line(
            {
                "role": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": shared}],
                },
            }
        )
        child_data = b"".join(
            [
                _line(
                    {
                        "role": "user",
                        "message": {
                            "role": "user",
                            "content": [{"type": "text", "text": shared}],
                        },
                    }
                ),
                _line(
                    {
                        "role": "user",
                        "message": {
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": (
                                        "<side_chat_boundary>\nSide chat boundary."
                                    ),
                                }
                            ],
                        },
                    }
                ),
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "agent-transcripts" / "parent-uuid"
            root.mkdir(parents=True)
            (root / "parent-uuid.jsonl").write_bytes(parent_line)
            sub = root / "subagents"
            sub.mkdir()
            child_path = sub / "child-uuid.jsonl"
            child_path.write_bytes(child_data)
            result = CursorAdapter().parse_chunk(
                child_path, child_data, start_offset=0
            )
            self.assertIsNotNone(result.session.parent_session_id)
            self.assertFalse(result.messages[0].authored_by_agent)
            self.assertFalse(result.messages[1].authored_by_agent)


class ClaudeSidechainAuthoredTests(unittest.TestCase):
    def test_subagent_first_non_plumbing_user_flagged(self) -> None:
        data = b"".join(
            [
                _line(
                    {
                        "type": "user",
                        "timestamp": "2026-08-01T12:00:00Z",
                        "sessionId": "parent-sess",
                        "agentId": "abc123",
                        "message": {
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": "Implement Wave 0 Task 9.",
                                }
                            ],
                        },
                    }
                ),
                _line(
                    {
                        "type": "user",
                        "timestamp": "2026-08-01T12:00:01Z",
                        "sessionId": "parent-sess",
                        "agentId": "abc123",
                        "message": {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "t1",
                                    "content": "ok",
                                }
                            ],
                        },
                    }
                ),
                _line(
                    {
                        "type": "user",
                        "timestamp": "2026-08-01T12:00:02Z",
                        "sessionId": "parent-sess",
                        "agentId": "abc123",
                        "message": {
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": "Coordinator nudge: keep going.",
                                }
                            ],
                        },
                    }
                ),
            ]
        )
        path = Path(
            "/tmp/proj/parent-sess/subagents/agent-abc123.jsonl"
        )
        result = ClaudeAdapter().parse_chunk(path, data, start_offset=0)
        self.assertEqual(result.session.parent_session_id, "parent-sess")
        users = [m for m in result.messages if m.role == "user"]
        self.assertTrue(users[0].authored_by_agent)
        self.assertTrue(users[1].is_tool_plumbing)
        self.assertFalse(users[1].authored_by_agent)
        self.assertFalse(users[2].authored_by_agent)


class CodexSubagentAuthoredTests(unittest.TestCase):
    def test_roleed_spawn_flags_first_user(self) -> None:
        data = b"".join(
            [
                _line(
                    {
                        "type": "session_meta",
                        "payload": {
                            "id": "child-1",
                            "parent_thread_id": "parent-1",
                            "thread_source": "subagent",
                            "agent_role": "explorer",
                            "source": {
                                "subagent": {
                                    "thread_spawn": {
                                        "parent_thread_id": "parent-1",
                                        "depth": 1,
                                    }
                                }
                            },
                        },
                    }
                ),
                _line(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "user_message",
                            "message": "Codex Security repository scan file-review shard.",
                        },
                    }
                ),
            ]
        )
        result = CodexAdapter().parse_chunk(
            Path("/tmp/rollout-child-1.jsonl"), data, start_offset=0
        )
        self.assertEqual(result.session.parent_session_id, "parent-1")
        self.assertTrue(result.messages[0].authored_by_agent)

    def test_history_fork_without_role_not_flagged(self) -> None:
        data = b"".join(
            [
                _line(
                    {
                        "type": "session_meta",
                        "payload": {
                            "id": "child-2",
                            "parent_thread_id": "parent-1",
                            "thread_source": "subagent",
                            "agent_role": None,
                            "source": {
                                "subagent": {
                                    "thread_spawn": {
                                        "parent_thread_id": "parent-1",
                                        "depth": 1,
                                    }
                                }
                            },
                        },
                    }
                ),
                _line(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "user_message",
                            "message": "yo! human message copied into fork",
                        },
                    }
                ),
            ]
        )
        result = CodexAdapter().parse_chunk(
            Path("/tmp/rollout-child-2.jsonl"), data, start_offset=0
        )
        self.assertEqual(result.session.parent_session_id, "parent-1")
        self.assertFalse(result.messages[0].authored_by_agent)

    def test_guardian_other_spawn_flagged(self) -> None:
        data = b"".join(
            [
                _line(
                    {
                        "type": "session_meta",
                        "payload": {
                            "id": "child-3",
                            "parent_thread_id": "parent-1",
                            "thread_source": "subagent",
                            "source": {"subagent": {"other": "guardian"}},
                        },
                    }
                ),
                _line(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "user_message",
                            "message": (
                                "The following is the Codex agent history whose "
                                "request action you are assessing."
                            ),
                        },
                    }
                ),
            ]
        )
        result = CodexAdapter().parse_chunk(
            Path("/tmp/rollout-child-3.jsonl"), data, start_offset=0
        )
        self.assertTrue(result.messages[0].authored_by_agent)


class TriageAuthoredOverrideTests(unittest.TestCase):
    def test_authored_by_agent_becomes_worker_brief(self) -> None:
        ctx = WindowContext(
            window_id="w1",
            session_id="s1",
            harness="cursor",
            request_text=(
                "<user_query>\nYou are a builder for agentlog.\n</user_query>"
            ),
            authored_by_agent=True,
        )
        result = triage_window(ctx)
        self.assertEqual(result.request_kind, "worker_brief")
        self.assertEqual(result.route, Route.WORKER_TASK)
        self.assertIn("authored_by_agent", result.matched_rules)
        self.assertIn("worker_brief", result.matched_rules)

    def test_authored_does_not_override_auto_review(self) -> None:
        ctx = WindowContext(
            window_id="w2",
            session_id="s2",
            harness="codex",
            request_text=(
                "The following is the Codex agent history whose request "
                "action you are assessing."
            ),
            authored_by_agent=True,
        )
        result = triage_window(ctx)
        self.assertEqual(result.request_kind, "auto_review")
        self.assertEqual(result.route, Route.AUTO_REVIEW)


class MigrationAuthoredByAgentTests(unittest.TestCase):
    def test_migrate_adds_authored_by_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "legacy.db"
            conn = sqlite3.connect(str(db_path))
            conn.executescript(
                """
                CREATE TABLE messages (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    timestamp TEXT,
                    model TEXT,
                    effort TEXT,
                    text TEXT NOT NULL DEFAULT '',
                    content_hash TEXT NOT NULL DEFAULT ''
                );
                """
            )
            conn.commit()
            init_db(conn)
            cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(messages)").fetchall()
            }
            self.assertIn("authored_by_agent", cols)
            versions = {
                int(r[0])
                for r in conn.execute("SELECT version FROM schema_migrations")
            }
            self.assertIn(10, versions)
            conn.close()


if __name__ == "__main__":
    unittest.main()
