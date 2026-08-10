from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agentlog.ingest.cursor import (
    CursorAdapter,
    _composer_meta_cached,
    _composer_model_effort_map,
    lookup_composer_model_effort,
)


def _clear() -> None:
    _composer_meta_cached.cache_clear()


class CursorModelEffortLookupTests(unittest.TestCase):
    def test_reads_model_and_effort_from_composer_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.vscdb"
            conn = sqlite3.connect(str(db_path))
            conn.execute(
                "CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value BLOB)"
            )
            payload = {
                "composerId": "abc-123",
                "modelConfig": {
                    "modelName": "grok-4.5",
                    "maxMode": False,
                    "selectedModels": [
                        {
                            "modelId": "grok-4.5",
                            "parameters": [
                                {"id": "effort", "value": "high"},
                                {"id": "fast", "value": "true"},
                            ],
                        }
                    ],
                },
            }
            conn.execute(
                "INSERT INTO cursorDiskKV (key, value) VALUES (?, ?)",
                ("composerData:abc-123", json.dumps(payload)),
            )
            conn.execute(
                "INSERT INTO cursorDiskKV (key, value) VALUES (?, ?)",
                (
                    "composerData:def-456",
                    json.dumps(
                        {
                            "modelConfig": {
                                "modelName": "default",
                                "selectedModels": [
                                    {"modelId": "default", "parameters": []}
                                ],
                            }
                        }
                    ),
                ),
            )
            conn.commit()
            conn.close()

            _clear()
            model, effort = lookup_composer_model_effort(
                "abc-123", state_db=db_path
            )
            self.assertEqual(model, "grok-4.5")
            self.assertEqual(effort, "high")

            model2, effort2 = lookup_composer_model_effort(
                "def-456", state_db=db_path
            )
            self.assertIsNone(model2)
            self.assertIsNone(effort2)
            scanned = _composer_model_effort_map(str(db_path))
            self.assertEqual(scanned["abc-123"], ("grok-4.5", "high"))
            _clear()

    def test_parse_chunk_attaches_session_model_effort(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.vscdb"
            conn = sqlite3.connect(str(db_path))
            conn.execute(
                "CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value BLOB)"
            )
            composer_id = "11111111-2222-3333-4444-555555555555"
            conn.execute(
                "INSERT INTO cursorDiskKV (key, value) VALUES (?, ?)",
                (
                    f"composerData:{composer_id}",
                    json.dumps(
                        {
                            "status": "completed",
                            "lastUpdatedAt": 1700001000000,
                            "modelConfig": {
                                "modelName": "claude-fable-5",
                                "selectedModels": [
                                    {
                                        "modelId": "claude-fable-5",
                                        "parameters": [
                                            {"id": "effort", "value": "medium"}
                                        ],
                                    }
                                ],
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
                            "role": "user",
                            "message": {
                                "content": [{"type": "text", "text": "hi"}]
                            },
                        }
                    )
                    + "\n"
                ).encode()
                path = (
                    Path("/tmp/proj/agent-transcripts")
                    / composer_id
                    / f"{composer_id}.jsonl"
                )
                result = CursorAdapter().parse_chunk(
                    path, data, start_offset=0
                )
                self.assertEqual(result.session.model, "claude-fable-5")
                self.assertEqual(result.session.effort, "medium")
                self.assertEqual(result.session.effort_source, "medium")
                # Session model must not be stamped onto messages without
                # per-generation modelInfo.
                self.assertIsNone(result.messages[0].model)
            finally:
                cursor_mod.CURSOR_STATE_VSCDB = original
                _clear()


if __name__ == "__main__":
    unittest.main()
