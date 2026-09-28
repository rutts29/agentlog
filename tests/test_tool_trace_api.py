from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from agentlog.api.app import create_app
from agentlog.api.tool_trace import tool_trace
from agentlog.db.schema import connect, init_db


class ToolTraceApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.db_path = self.root / "agentlog.db"
        self.source_path = self.root / "rollout.jsonl"
        self.conn = connect(self.db_path)
        init_db(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def seed_codex_trace(self) -> None:
        records = [
            {
                "timestamp": "2026-09-28T10:00:01Z",
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "id": "call-record",
                    "call_id": "call-1",
                    "name": "exec",
                    "input": "const r = await tools.exec_command({\"cmd\":\"pytest -q\"});",
                },
            },
            {
                "timestamp": "2026-09-28T10:00:03Z",
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "call-1",
                    "output": [{"type": "input_text", "text": "64 passed"}],
                },
            },
        ]
        self.source_path.write_text(
            "".join(json.dumps(record) + "\n" for record in records),
            encoding="utf-8",
        )
        cursor = self.conn.execute(
            """
            INSERT INTO artifacts
              (harness, path, size, mtime_ns, content_hash, parsed_offset,
               parser_version, transcript_storage)
            VALUES ('codex', ?, 1, 1, 'hash', 0, 'test', 'legacy_materialized')
            """,
            (str(self.source_path),),
        )
        artifact_id = int(cursor.lastrowid)
        self.conn.execute(
            """
            INSERT INTO sessions
              (id, harness, external_id, artifact_id, model, model_canonical)
            VALUES ('codex:trace', 'codex', 'trace', ?, 'gpt-6-astra', 'gpt-6-astra')
            """,
            (artifact_id,),
        )
        self.conn.execute(
            """
            INSERT INTO messages
              (id, session_id, seq, role, timestamp, model, model_canonical,
               effort, text, content_hash)
            VALUES ('m1', 'codex:trace', 1, 'assistant',
                    '2026-09-28T10:00:01Z', 'gpt-7-research',
                    'gpt-7-research', 'high', 'Running tests', 'm1')
            """
        )
        self.conn.executemany(
            """
            INSERT INTO tool_events
              (id, session_id, message_id, seq, tool_name, action, success,
               duration_ms, operation_kind)
            VALUES (?, 'codex:trace', 'm1', ?, 'exec', ?, ?, ?, 'verification')
            """,
            [
                ("t1", 1, "call", None, None),
                ("t2", 2, "result", 1, 2000),
            ],
        )
        self.conn.commit()

    def test_pairs_normalized_rows_with_local_source_input_and_output(self) -> None:
        self.seed_codex_trace()

        trace = tool_trace(self.conn, "codex:trace", start_seq=1, end_seq=2)

        assert trace is not None
        self.assertTrue(trace["capture"]["source_backed"])
        self.assertEqual(trace["capture"]["detailed_items"], 1)
        self.assertEqual(len(trace["items"]), 1)
        item = trace["items"][0]
        self.assertIn("pytest -q", item["input"])
        self.assertEqual(item["output"], "64 passed")
        self.assertEqual(item["success"], 1)
        self.assertEqual(item["duration_ms"], 2000)
        self.assertEqual(item["model"], "gpt-7-research")
        self.assertEqual(item["effort"], "high")
        self.assertEqual(item["call_seq"], 1)
        self.assertEqual(item["result_seq"], 2)

    def test_http_endpoint_returns_phase_scoped_detail(self) -> None:
        self.seed_codex_trace()
        self.conn.close()
        with TestClient(create_app(self.db_path)) as client:
            response = client.get(
                "/api/sessions/codex%3Atrace/tool-trace",
                params={"start_seq": 1, "end_seq": 2},
            )
        self.conn = connect(self.db_path)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"][0]["output"], "64 passed")


if __name__ == "__main__":
    unittest.main()
