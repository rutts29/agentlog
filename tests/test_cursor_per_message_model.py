from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agentlog.ingest.base import parse_cursor_wrapper_ts, parse_ts
from agentlog.ingest.cursor import (
    CursorAdapter,
    _composer_meta_cached,
    lookup_composer_meta,
)


def _clear() -> None:
    _composer_meta_cached.cache_clear()


class CursorPerMessageModelTests(unittest.TestCase):
    def test_user_bubble_model_info_attributed_to_following_assistants(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.vscdb"
            conn = sqlite3.connect(str(db_path))
            conn.execute(
                "CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value BLOB)"
            )
            cid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
            u1, u2 = "user-bubble-1", "user-bubble-2"
            a1, a2 = "ai-bubble-1", "ai-bubble-2"
            composer = {
                "composerId": cid,
                "status": "completed",
                "createdAt": 1700000000000,
                "lastUpdatedAt": 1700001000000,
                "modelConfig": {
                    "modelName": "session-default-model",
                    "selectedModels": [
                        {
                            "modelId": "session-default-model",
                            "parameters": [{"id": "effort", "value": "high"}],
                        }
                    ],
                },
                "trackedGitRepos": [
                    {
                        "repoPath": "/tmp/demo",
                        "branches": [
                            {
                                "branchName": "feature/x",
                                "lastInteractionAt": 1700000500000,
                            }
                        ],
                    }
                ],
                "fullConversationHeadersOnly": [
                    {"bubbleId": u1, "type": 1, "createdAt": "2026-07-01T10:00:00Z"},
                    {"bubbleId": a1, "type": 2, "createdAt": "2026-07-01T10:00:05Z"},
                    {"bubbleId": u2, "type": 1, "createdAt": "2026-07-01T10:01:00Z"},
                    {"bubbleId": a2, "type": 2, "createdAt": "2026-07-01T10:01:05Z"},
                ],
            }
            conn.execute(
                "INSERT INTO cursorDiskKV (key, value) VALUES (?, ?)",
                (f"composerData:{cid}", json.dumps(composer)),
            )
            bubbles = {
                u1: {
                    "bubbleId": u1,
                    "type": 1,
                    "text": "first question",
                    "createdAt": "2026-07-01T10:00:00Z",
                    "modelInfo": {"modelName": "grok-4.5"},
                },
                a1: {
                    "bubbleId": a1,
                    "type": 2,
                    "text": "first answer",
                    "createdAt": "2026-07-01T10:00:05Z",
                },
                u2: {
                    "bubbleId": u2,
                    "type": 1,
                    "text": "second question",
                    "createdAt": "2026-07-01T10:01:00Z",
                    "modelInfo": {"modelName": "claude-opus-5"},
                },
                a2: {
                    "bubbleId": a2,
                    "type": 2,
                    "text": "second answer",
                    "createdAt": "2026-07-01T10:01:05Z",
                },
            }
            for bid, payload in bubbles.items():
                conn.execute(
                    "INSERT INTO cursorDiskKV (key, value) VALUES (?, ?)",
                    (f"bubbleId:{cid}:{bid}", json.dumps(payload)),
                )
            conn.commit()
            conn.close()

            _clear()
            import agentlog.ingest.cursor as cursor_mod

            original = cursor_mod.CURSOR_STATE_VSCDB
            cursor_mod.CURSOR_STATE_VSCDB = db_path
            try:
                data = (
                    # Composer bubble metadata keeps the unwrapped query key;
                    # transcript storage must normalize wrappers without losing
                    # per-turn model attribution.
                    json.dumps(
                        {
                            "role": "user",
                            "message": {
                                "content": [
                                    {
                                        "type": "text",
                                        "text": (
                                            "<timestamp>Wednesday, Jul 1, 2026, "
                                            "10:00 AM (UTC+00:00)</timestamp>\n"
                                            "<user_query>first question</user_query>"
                                        ),
                                    }
                                ]
                            },
                        }
                    )
                    + "\n"
                    + json.dumps(
                        {
                            "role": "assistant",
                            "message": {
                                "content": [
                                    {"type": "text", "text": "first answer"}
                                ]
                            },
                        }
                    )
                    + "\n"
                    + json.dumps(
                        {
                            "role": "user",
                            "message": {
                                "content": [
                                    {
                                        "type": "text",
                                        "text": (
                                            "<timestamp>Wednesday, Jul 1, 2026, "
                                            "10:01 AM (UTC+00:00)</timestamp>\n"
                                            "<user_query>second question</user_query>"
                                        ),
                                    }
                                ]
                            },
                        }
                    )
                    + "\n"
                    + json.dumps(
                        {
                            "role": "assistant",
                            "message": {
                                "content": [
                                    {"type": "text", "text": "second answer"}
                                ]
                            },
                        }
                    )
                    + "\n"
                ).encode()
                path = (
                    Path("/tmp/proj/agent-transcripts") / cid / f"{cid}.jsonl"
                )
                result = CursorAdapter().parse_chunk(path, data, start_offset=0)
                self.assertEqual(result.session.model, "session-default-model")
                self.assertEqual(result.session.branch, "feature/x")
                self.assertIsNotNone(result.session.ended_at)
                self.assertEqual(result.session.effort, "high")
                self.assertEqual(result.session.effort_source, "high")
                asst = [m for m in result.messages if m.role == "assistant"]
                self.assertEqual([m.model for m in asst], ["grok-4.5", "claude-opus-5"])
                users = [m for m in result.messages if m.role == "user"]
                self.assertEqual([m.text for m in users], ["first question", "second question"])
            finally:
                cursor_mod.CURSOR_STATE_VSCDB = original
                _clear()

    def test_no_bubble_model_leaves_message_model_null(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.vscdb"
            conn = sqlite3.connect(str(db_path))
            conn.execute(
                "CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value BLOB)"
            )
            cid = "11111111-2222-3333-4444-555555555555"
            conn.execute(
                "INSERT INTO cursorDiskKV (key, value) VALUES (?, ?)",
                (
                    f"composerData:{cid}",
                    json.dumps(
                        {
                            "status": "completed",
                            "lastUpdatedAt": 1700001000000,
                            "modelConfig": {
                                "modelName": "composer-2.5",
                                "selectedModels": [],
                            },
                            "fullConversationHeadersOnly": [],
                        }
                    ),
                ),
            )
            conn.commit()
            conn.close()
            _clear()
            import agentlog.ingest.cursor as cursor_mod

            original = cursor_mod.CURSOR_STATE_VSCDB
            cursor_mod.CURSOR_STATE_VSCDB = db_path
            try:
                data = (
                    json.dumps(
                        {
                            "role": "assistant",
                            "message": {
                                "content": [{"type": "text", "text": "hi"}]
                            },
                        }
                    )
                    + "\n"
                ).encode()
                path = (
                    Path("/tmp/proj/agent-transcripts") / cid / f"{cid}.jsonl"
                )
                result = CursorAdapter().parse_chunk(path, data, start_offset=0)
                self.assertEqual(result.session.model, "composer-2.5")
                self.assertIsNone(result.messages[0].model)
                self.assertIsNotNone(result.session.ended_at)
            finally:
                cursor_mod.CURSOR_STATE_VSCDB = original
                _clear()


class CursorTimestampParseTests(unittest.TestCase):
    def test_wrapper_timestamp(self) -> None:
        dt = parse_cursor_wrapper_ts(
            "Thursday, Jul 23, 2026, 11:09 PM (UTC+5:30)"
        )
        assert dt is not None
        self.assertEqual(dt.year, 2026)
        self.assertEqual(dt.month, 7)
        self.assertEqual(dt.day, 23)
        self.assertEqual(dt.hour, 23)
        self.assertEqual(dt.minute, 9)

    def test_parse_ts_falls_back_to_wrapper(self) -> None:
        dt = parse_ts("Sunday, Aug 9, 2026, 5:38 PM (UTC+5:30)")
        assert dt is not None
        self.assertEqual(dt.hour, 17)


class ComposerMetaLookupTests(unittest.TestCase):
    def test_lookup_refreshes_after_a_wal_update(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.vscdb"
            conn = sqlite3.connect(str(db_path))
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute(
                "CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value BLOB)"
            )
            cid = "meta-wal"
            conn.execute(
                "INSERT INTO cursorDiskKV (key, value) VALUES (?, ?)",
                (
                    f"composerData:{cid}",
                    json.dumps({"modelConfig": {"modelName": "gpt-5.5"}}),
                ),
            )
            conn.commit()
            _clear()
            self.assertEqual(
                lookup_composer_meta(cid, state_db=db_path).model, "gpt-5.5"
            )

            conn.execute(
                "UPDATE cursorDiskKV SET value = ? WHERE key = ?",
                (
                    json.dumps({"modelConfig": {"modelName": "grok-4.5"}}),
                    f"composerData:{cid}",
                ),
            )
            conn.commit()
            self.assertEqual(
                lookup_composer_meta(cid, state_db=db_path).model, "grok-4.5"
            )
            conn.close()
            _clear()

    def test_ended_at_from_last_updated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.vscdb"
            conn = sqlite3.connect(str(db_path))
            conn.execute(
                "CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value BLOB)"
            )
            cid = "meta-1"
            conn.execute(
                "INSERT INTO cursorDiskKV (key, value) VALUES (?, ?)",
                (
                    f"composerData:{cid}",
                    json.dumps(
                        {
                            "status": "completed",
                            "createdAt": 1700000000000,
                            "lastUpdatedAt": 1700009999000,
                            "modelConfig": {"modelName": "default"},
                            "fullConversationHeadersOnly": [],
                        }
                    ),
                ),
            )
            conn.commit()
            conn.close()
            _clear()
            meta = lookup_composer_meta(cid, state_db=db_path)
            self.assertFalse(meta.still_open)
            self.assertIsNotNone(meta.ended_at)
            _clear()


if __name__ == "__main__":
    unittest.main()
