from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agentlog.analysis.coach import emit_coach_packets
from agentlog.analysis.coach.proof import (
    is_failed_tool_result,
    is_successful_artifact_result,
    is_successful_tool_result,
    is_verification_result,
    supports_successful_result,
)
from agentlog.db.migrations.v026_tool_operation_kind import apply as apply_v026
from agentlog.db.migrations import MIGRATIONS
from agentlog.db.repository import Repository
from agentlog.db.schema import init_db
from agentlog.ingest.codex import CodexAdapter
from agentlog.ingest.claude import ClaudeAdapter
from agentlog.ingest.cursor import CursorAdapter
from agentlog.normalize.tool_ops import classify_operation


def _line(value: dict) -> bytes:
    return (json.dumps(value) + "\n").encode()


def _message(role: str, text: str) -> dict:
    return {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": role,
            "content": [{"type": "input_text", "text": text}],
        },
    }


def _call(call_id: str, arguments: object) -> dict:
    return {
        "type": "response_item",
        "payload": {
            "type": "function_call",
            "call_id": call_id,
            "name": "exec_command",
            "arguments": json.dumps(arguments),
        },
    }


def _end(call_id: str, *, success: bool, exit_code: int | None = None) -> dict:
    return {
        "type": "event_msg",
        "payload": {
            "type": "exec_command_end",
            "call_id": call_id,
            "success": success,
            "exit_code": (0 if success else 1) if exit_code is None else exit_code,
        },
    }


class ToolOperationKindTests(unittest.TestCase):
    def test_claude_and_cursor_results_retain_call_category(self) -> None:
        claude_data = b"".join(
            [
                _line({
                    "type": "user",
                    "sessionId": "operation-link",
                    "message": {"role": "user", "content": [{"type": "text", "text": "Apply the patch."}]},
                }),
                _line({
                    "type": "assistant",
                    "sessionId": "operation-link",
                    "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "tool-1", "name": "Bash", "input": {"command": "apply_patch < patch.diff"}}]},
                }),
                _line({
                    "type": "user",
                    "sessionId": "operation-link",
                    "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tool-1", "content": "ok"}]},
                }),
            ]
        )
        cursor_data = b"".join(
            [
                _line({
                    "role": "user",
                    "message": {"content": [{"type": "text", "text": "Apply the patch."}]},
                }),
                _line({
                    "role": "assistant",
                    "message": {"content": [{"type": "tool_use", "id": "tool-1", "name": "Bash", "input": {"command": "apply_patch < patch.diff"}}]},
                }),
                _line({
                    "role": "user",
                    "message": {"content": [{"type": "tool_result", "tool_use_id": "tool-1", "content": "ok"}]},
                }),
            ]
        )
        for adapter, path, data in (
            (ClaudeAdapter(), Path("/tmp/operation-link-claude.jsonl"), claude_data),
            (CursorAdapter(), Path("/tmp/operation-link-cursor.jsonl"), cursor_data),
        ):
            result = adapter.parse_chunk(path, data, start_offset=0)
            terminal = next(event for event in result.tool_events if event.action == "result")
            self.assertEqual(terminal.tool_name, "Bash")
            self.assertEqual(terminal.operation_kind, "artifact_write")

    def test_quoted_search_patterns_remain_read_only(self) -> None:
        self.assertEqual(
            classify_operation("exec_command", 'rg "; pytest " logs'),
            "read_only",
        )

    def test_common_verification_wrappers_are_classified_transiently(self) -> None:
        for command in (
            ".venv/bin/python -m unittest",
            "/usr/bin/python3 -m pytest",
            "PYTHONPATH=src pytest",
            "npx tsc --noEmit",
            "uv run pytest",
            "poetry run pytest",
            "cd repo && .venv/bin/python -m unittest",
        ):
            self.assertEqual(classify_operation("exec_command", command), "verification")

    def test_shell_masking_chains_fail_closed(self) -> None:
        for command in (
            "pytest || true",
            "pytest; true",
            "pytest | tee out",
            "cd repo && pytest && echo done",
            "cd repo && true && pytest",
        ):
            self.assertNotEqual(
                classify_operation("exec_command", command),
                "verification",
            )
        self.assertEqual(
            classify_operation("exec_command", "cd repo && pytest"),
            "verification",
        )

    def test_shell_masking_cannot_prove_verification_after_db_preprocess(self) -> None:
        data = b"".join(
            [
                _line(_message("user", "Run the tests.")),
                _line(_message("assistant", "I will run the requested command.")),
                _line(_call("masked", {"cmd": "pytest || true"})),
                _line(_end("masked", success=True)),
            ]
        )
        path = Path("/tmp/rollout-operation-masking.jsonl")
        parsed = CodexAdapter().parse_chunk(path, data, start_offset=0)
        terminal = next(event for event in parsed.tool_events if event.action == "end")
        self.assertEqual(terminal.operation_kind, "execute_other")

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        repo = Repository(conn)
        artifact_id = repo.upsert_artifact(
            harness="codex",
            path=str(path),
            size=len(data),
            mtime_ns=1,
            content_hash="operation-masking-fixture",
            parsed_offset=len(data),
            parser_version="12",
        )
        session_id = repo.save_parse_result(
            artifact_id=artifact_id, result=parsed, append=False
        )
        messages = repo.list_messages(session_id)
        assistant_id = next(row["id"] for row in messages if row["role"] == "assistant")
        user_id = next(row["id"] for row in messages if row["role"] == "user")
        conn.execute(
            "INSERT INTO exchange_windows(id,session_id,request_message_id,response_message_id,input_hash,content_hash) "
            "VALUES(?,?,?,?,?,?)",
            ("masking-window", session_id, user_id, assistant_id, "input", "content"),
        )
        conn.commit()
        with tempfile.TemporaryDirectory() as temp:
            manifest = emit_coach_packets(conn, Path(temp))
            packet_path = Path(temp) / manifest["packets"][0]["path"]
            packet = json.loads(packet_path.read_text())
            timeline = packet["windows"][0]["tool_timeline"]
            fact = next(
                json.loads(item["fact"])
                for item in timeline
                if json.loads(item["fact"])["action"] == "end"
            )
            evidence = {"evidence_type": "tool", "fact": json.dumps(fact)}
            self.assertEqual(fact["operation_kind"], "execute_other")
            self.assertFalse(is_successful_tool_result(evidence))
            self.assertFalse(is_verification_result(evidence))
        conn.close()

    def test_raw_command_cannot_link_a_private_request_to_verification(self) -> None:
        evidence = {
            "evidence_type": "tool",
            "window_id": "w",
            "fact": json.dumps({
                "tool_name": "exec_command",
                "command": "pytest -q",
                "action": "end",
                "success": True,
                "operation_kind": "verification",
            }),
        }
        request = {
            "evidence_type": "message",
            "window_id": "w",
            "role": "user",
            "quote": "Keep this private",
        }
        self.assertFalse(
            supports_successful_result(
                [evidence, request],
                request_window_ids={"w"},
                window_order={"w": 0},
                request_evidence=[request],
            )
        )
        self.assertEqual(
            classify_operation("exec_command", 'grep "| npm test " x'),
            "read_only",
        )

    def test_v026_adds_column_to_legacy_tool_events(self) -> None:
        self.assertEqual(MIGRATIONS[-1][0], 26)
        conn = sqlite3.connect(":memory:")
        conn.executescript(
            """
            CREATE TABLE tool_events (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                message_id TEXT,
                seq INTEGER NOT NULL,
                tool_name TEXT NOT NULL,
                action TEXT NOT NULL,
                success INTEGER,
                duration_ms INTEGER
            );
            """
        )
        apply_v026(conn)
        columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(tool_events)")
        }
        self.assertIn("operation_kind", columns)
        conn.execute(
            "INSERT INTO tool_events(id,session_id,seq,tool_name,action) VALUES(?,?,?,?,?)",
            ("legacy", "session", 1, "exec_command", "end"),
        )
        self.assertEqual(
            conn.execute(
                "SELECT operation_kind FROM tool_events WHERE id='legacy'"
            ).fetchone()[0],
            "unknown",
        )

    def test_codex_db_preprocess_keeps_safe_operation_proof(self) -> None:
        data = b"".join(
            [
                _line(_message("user", "Run the tests and apply the patch.")),
                _line(_message("assistant", "I will run the requested work.")),
                _line(_call("verify", {"cmd": "pytest -q"})),
                _line(_end("verify", success=True)),
                _line(_call("read", {"cmd": "cat README.md"})),
                _line(_end("read", success=True)),
                _line(_call("patch", {"cmd": "apply_patch < patch.diff"})),
                _line(_end("patch", success=True)),
                _line(_call("failed", {"cmd": "pytest -q"})),
                _line(_end("failed", success=False)),
                _line(_call("conflict", {"cmd": "pytest -q"})),
                _line(_end("conflict", success=True, exit_code=1)),
                _line(_call("malformed", "[REDACTED_ARGS]")),
                _line(_end("malformed", success=True)),
            ]
        )
        parsed = CodexAdapter().parse_chunk(
            Path("/tmp/rollout-operation-kind.jsonl"), data, start_offset=0
        )
        self.assertEqual(
            [event.operation_kind for event in parsed.tool_events if event.action == "end"],
            ["verification", "read_only", "artifact_write", "verification", "verification", "execute_other"],
        )
        parsed_json = parsed.model_dump_json()
        self.assertNotIn("pytest -q", parsed_json)
        self.assertNotIn("REDACTED_ARGS", parsed_json)

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        repo = Repository(conn)
        artifact_id = repo.upsert_artifact(
            harness="codex",
            path="/tmp/rollout-operation-kind.jsonl",
            size=len(data),
            mtime_ns=1,
            content_hash="operation-kind-fixture",
            parsed_offset=len(data),
            parser_version="12",
        )
        session_id = repo.save_parse_result(
            artifact_id=artifact_id, result=parsed, append=False
        )
        conn.commit()
        rows = conn.execute(
            "SELECT tool_name, action, operation_kind FROM tool_events "
            "WHERE session_id = ? ORDER BY seq",
            (session_id,),
        ).fetchall()
        self.assertTrue(rows)
        tool_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(tool_events)")
        }
        self.assertNotIn("command", tool_columns)
        self.assertNotIn("arguments", tool_columns)
        self.assertNotIn("pytest -q", json.dumps([dict(row) for row in rows]))

        messages = repo.list_messages(session_id)
        assistant_id = next(row["id"] for row in messages if row["role"] == "assistant")
        user_id = next(row["id"] for row in messages if row["role"] == "user")
        conn.execute(
            "INSERT INTO exchange_windows(id,session_id,request_message_id,response_message_id,input_hash,content_hash) "
            "VALUES(?,?,?,?,?,?)",
            ("operation-window", session_id, user_id, assistant_id, "input", "content"),
        )
        conn.commit()
        with tempfile.TemporaryDirectory() as temp:
            packet_root = Path(temp)
            manifest = emit_coach_packets(conn, packet_root)
            self.assertTrue(manifest["packets"])
            packet_path = packet_root / manifest["packets"][0]["path"]
            packet = json.loads(packet_path.read_text())
            timeline = packet["windows"][0]["tool_timeline"]

            def evidence(fact: dict) -> dict:
                return {"evidence_type": "tool", "fact": json.dumps(fact)}

            call_fact = next(
                json.loads(item["fact"])
                for item in timeline
                if json.loads(item["fact"])["action"] == "call"
                and json.loads(item["fact"])["operation_kind"] == "verification"
            )
            self.assertFalse(is_successful_tool_result(evidence(call_fact)))
            terminal_facts = [
                json.loads(item["fact"])
                for item in timeline
                if json.loads(item["fact"])["action"] == "end"
            ]

            read = next(fact for fact in terminal_facts if fact["operation_kind"] == "read_only")
            verified = next(
                fact for fact in terminal_facts
                if fact["operation_kind"] == "verification" and fact["success"] in (True, 1)
            )
            artifact = next(fact for fact in terminal_facts if fact["operation_kind"] == "artifact_write")
            conflict = next(
                fact for fact in terminal_facts
                if fact["operation_kind"] == "verification" and fact["success"] is None
            )
            self.assertFalse(is_successful_tool_result(evidence(read)))
            self.assertTrue(is_verification_result(evidence(verified)))
            self.assertTrue(is_successful_artifact_result(evidence(artifact)))
            self.assertFalse(is_verification_result(evidence(conflict)))
            failed = next(
                evidence(fact)
                for fact in terminal_facts
                if fact["success"] in (False, 0) and fact["operation_kind"] == "verification"
            )
            self.assertTrue(is_failed_tool_result(failed))
        conn.close()


if __name__ == "__main__":
    unittest.main()
