from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agentlog.analysis.coach.preprocess import _window_rows
from agentlog.analysis.windows import build_exchange_windows
from agentlog.db.repository import Repository
from agentlog.db.schema import init_db
from agentlog.ingest.base import content_is_tool_plumbing
from agentlog.ingest.claude import ClaudeAdapter
from agentlog.safety.redaction import RedactionReport


def _line(obj: dict) -> bytes:
    return (json.dumps(obj) + "\n").encode()


class ContentIsToolPlumbingTests(unittest.TestCase):
    def test_tool_result_only_is_plumbing(self) -> None:
        content = [
            {"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
        ]
        self.assertTrue(content_is_tool_plumbing(content))

    def test_tool_use_only_is_plumbing(self) -> None:
        content = [
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}},
        ]
        self.assertTrue(content_is_tool_plumbing(content))

    def test_text_plus_tool_is_not_plumbing(self) -> None:
        content = [
            {"type": "text", "text": "hello"},
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}},
        ]
        self.assertFalse(content_is_tool_plumbing(content))

    def test_empty_list_is_not_plumbing(self) -> None:
        self.assertFalse(content_is_tool_plumbing([]))
        self.assertFalse(content_is_tool_plumbing(None))

    def test_server_tool_use_is_plumbing(self) -> None:
        content = [
            {
                "type": "server_tool_use",
                "id": "srv_1",
                "name": "web_search",
                "input": {},
            },
        ]
        self.assertTrue(content_is_tool_plumbing(content))

    def test_empty_thinking_only_is_plumbing(self) -> None:
        content = [{"type": "thinking", "thinking": "", "signature": "x"}]
        self.assertTrue(content_is_tool_plumbing(content))


class ClaudeToolPlumbingAdapterTests(unittest.TestCase):
    def test_tool_result_user_row_flagged_and_tool_event_once(self) -> None:
        data = b"".join(
            [
                _line(
                    {
                        "type": "user",
                        "timestamp": "2026-08-01T12:00:00Z",
                        "sessionId": "sess-plumbing",
                        "message": {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "toolu_1",
                                    "content": "file contents",
                                }
                            ],
                        },
                    }
                ),
                _line(
                    {
                        "type": "user",
                        "timestamp": "2026-08-01T12:00:01Z",
                        "sessionId": "sess-plumbing",
                        "message": {
                            "role": "user",
                            "content": [{"type": "text", "text": "please continue"}],
                        },
                    }
                ),
                _line(
                    {
                        "type": "assistant",
                        "timestamp": "2026-08-01T12:00:02Z",
                        "sessionId": "sess-plumbing",
                        "message": {
                            "role": "assistant",
                            "model": "claude-opus-4-6",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "toolu_2",
                                    "name": "Read",
                                    "input": {"path": "a.py"},
                                }
                            ],
                        },
                    }
                ),
                _line(
                    {
                        "type": "assistant",
                        "timestamp": "2026-08-01T12:00:03Z",
                        "sessionId": "sess-plumbing",
                        "message": {
                            "role": "assistant",
                            "model": "claude-opus-4-6",
                            "content": [
                                {"type": "text", "text": "I read the file."},
                            ],
                        },
                    }
                ),
            ]
        )
        result = ClaudeAdapter().parse_chunk(
            Path("/tmp/sess-plumbing.jsonl"), data, start_offset=0
        )

        self.assertEqual(len(result.messages), 4)
        user_msgs = [m for m in result.messages if m.role == "user"]
        self.assertEqual(len(user_msgs), 2)
        self.assertTrue(user_msgs[0].is_tool_plumbing)
        self.assertEqual(user_msgs[0].text, "")
        self.assertFalse(user_msgs[1].is_tool_plumbing)
        self.assertEqual(user_msgs[1].text, "please continue")

        assistant_msgs = [m for m in result.messages if m.role == "assistant"]
        self.assertTrue(assistant_msgs[0].is_tool_plumbing)
        self.assertFalse(assistant_msgs[1].is_tool_plumbing)

        human_population = [
            m
            for m in result.messages
            if m.role == "user" and not m.is_tool_plumbing and m.text.strip()
        ]
        self.assertEqual(len(human_population), 1)
        self.assertEqual(human_population[0].text, "please continue")

        tool_results = [t for t in result.tool_events if t.action == "result"]
        tool_calls = [t for t in result.tool_events if t.action == "call"]
        self.assertEqual(len(tool_results), 1)
        self.assertEqual(tool_results[0].message_seq, 1)
        self.assertEqual(len(tool_calls), 1)
        self.assertEqual(tool_calls[0].tool_name, "Read")

    def test_seq_continuity_preserved_with_plumbing_rows(self) -> None:
        data = b"".join(
            [
                _line(
                    {
                        "type": "user",
                        "message": {
                            "role": "user",
                            "content": [
                                {"type": "tool_result", "tool_use_id": "t", "content": "x"}
                            ],
                        },
                    }
                ),
                _line(
                    {
                        "type": "user",
                        "message": {
                            "role": "user",
                            "content": [{"type": "text", "text": "hi"}],
                        },
                    }
                ),
            ]
        )
        result = ClaudeAdapter().parse_chunk(
            Path("/tmp/seq.jsonl"), data, start_offset=0
        )
        self.assertEqual([m.seq for m in result.messages], [1, 2])

    def test_structural_user_context_is_persisted_and_excluded_from_coach(self) -> None:
        structural = (
            ("isMeta", True, "metadata context"),
            ("sourceToolUseID", "toolu_context", "tool context"),
            ("isCompactSummary", True, "compaction summary"),
        )
        records: list[dict] = []
        for field, value, text in structural:
            records.append(
                {
                    "type": "user",
                    "timestamp": "2026-08-01T12:00:00Z",
                    "sessionId": "structural-context",
                    field: value,
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": text}],
                    },
                }
            )
        records.extend(
            [
                {
                    "type": "user",
                    "timestamp": "2026-08-01T12:00:01Z",
                    "sessionId": "structural-context",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "real request"}],
                    },
                },
                {
                    "type": "assistant",
                    "timestamp": "2026-08-01T12:00:02Z",
                    "sessionId": "structural-context",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "real response"}],
                    },
                },
            ]
        )
        data = b"".join(_line(record) for record in records)
        parsed = ClaudeAdapter().parse_chunk(
            Path("/tmp/structural-context.jsonl"), data, start_offset=0
        )
        self.assertEqual(
            [message.text for message in parsed.messages[:3]],
            [text for _, _, text in structural],
        )
        self.assertTrue(
            all(
                message.is_tool_plumbing and message.authored_by_agent
                for message in parsed.messages[:3]
            )
        )

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "agentlog.db"
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            init_db(conn)
            repo = Repository(conn)
            artifact_id = repo.upsert_artifact(
                harness="claude",
                path="/tmp/structural-context.jsonl",
                size=len(data),
                mtime_ns=1,
                content_hash="structural-context",
                parsed_offset=len(data),
                parser_version="13",
            )
            session_id = repo.save_parse_result(
                artifact_id=artifact_id, result=parsed, append=False
            )
            stored = repo.list_messages(session_id)
            self.assertEqual(
                [row["text"] for row in stored[:3]],
                [text for _, _, text in structural],
            )
            self.assertTrue(
                all(
                    row["is_tool_plumbing"] and row["authored_by_agent"]
                    for row in stored[:3]
                )
            )
            repo.replace_exchange_windows(
                session_id, build_exchange_windows(stored)
            )
            eligible, _, meta = _window_rows(conn, RedactionReport())
            self.assertEqual(len(eligible), 1)
            self.assertEqual(eligible[0]["user"], "real request")
            self.assertEqual(meta["scanned"], 1)
            conn.close()


class ExchangeWindowPlumbingTests(unittest.TestCase):
    def test_windows_skip_tool_plumbing_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "t.db"
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            init_db(conn)
            repo = Repository(conn)
            data = b"".join(
                [
                    _line(
                        {
                            "type": "user",
                            "timestamp": "2026-08-01T12:00:00Z",
                            "sessionId": "win1",
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
                            "timestamp": "2026-08-01T12:00:01Z",
                            "sessionId": "win1",
                            "message": {
                                "role": "user",
                                "content": [{"type": "text", "text": "real ask"}],
                            },
                        }
                    ),
                    _line(
                        {
                            "type": "assistant",
                            "timestamp": "2026-08-01T12:00:02Z",
                            "sessionId": "win1",
                            "message": {
                                "role": "assistant",
                                "content": [
                                    {
                                        "type": "tool_use",
                                        "id": "t2",
                                        "name": "Bash",
                                        "input": {},
                                    }
                                ],
                            },
                        }
                    ),
                    _line(
                        {
                            "type": "assistant",
                            "timestamp": "2026-08-01T12:00:03Z",
                            "sessionId": "win1",
                            "message": {
                                "role": "assistant",
                                "content": [{"type": "text", "text": "done"}],
                            },
                        }
                    ),
                ]
            )
            parsed = ClaudeAdapter().parse_chunk(
                Path("/tmp/win1.jsonl"), data, start_offset=0
            )
            art_id = repo.upsert_artifact(
                harness="claude",
                path="/tmp/win1.jsonl",
                size=len(data),
                mtime_ns=1,
                content_hash="abc",
                parsed_offset=len(data),
                parser_version="5",
            )
            sid = repo.save_parse_result(
                artifact_id=art_id, result=parsed, append=False
            )
            messages = repo.list_messages(sid)
            windows = build_exchange_windows(messages)
            self.assertEqual(len(windows), 1)
            req_id, resp_id = windows[0][0], windows[0][1]
            by_id = {m["id"]: m for m in messages}
            self.assertEqual(by_id[req_id]["text"], "real ask")
            self.assertEqual(by_id[resp_id]["text"], "done")
            self.assertEqual(by_id[req_id]["is_tool_plumbing"], 0)
            conn.close()


class SchemaMigrationTests(unittest.TestCase):
    def test_migrate_adds_is_tool_plumbing_to_legacy_db(self) -> None:
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
            self.assertIn("is_tool_plumbing", cols)
            conn.close()


if __name__ == "__main__":
    unittest.main()
