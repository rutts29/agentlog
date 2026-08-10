from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agentlog.ingest.codex import CodexAdapter
from agentlog.db.repository import Repository
from agentlog.db.schema import connect, init_db
from agentlog.normalize.synthetic import is_codex_internal_context_goal


def _line(obj: dict) -> bytes:
    return (json.dumps(obj) + "\n").encode()


def _msg(role: str, text: str) -> dict:
    return {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": role,
            "content": [{"type": "input_text", "text": text}],
        },
    }


def _assistant(text: str) -> dict:
    return {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text}],
        },
    }


def _call(call_id: str, name: str = "exec_command") -> dict:
    return {
        "type": "response_item",
        "payload": {
            "type": "function_call",
            "call_id": call_id,
            "name": name,
        },
    }


def _result(call_id: str, exit_code: int = 0) -> dict:
    return {
        "type": "response_item",
        "payload": {
            "type": "function_call_output",
            "call_id": call_id,
            "exit_code": exit_code,
        },
    }


def _terminal(kind: str, call_id: str, **fields: object) -> dict:
    return {
        "type": "event_msg",
        "payload": {"type": kind, "call_id": call_id, **fields},
    }


class CodexToolLinkageTests(unittest.TestCase):
    def test_goal_internal_context_is_agent_authored_and_retained(self) -> None:
        text = (
            '<codex_internal_context source="goal">\n'
            "Continue the active task from the previous turn.\n"
            "</codex_internal_context>"
        )
        data = b"".join(
            [
                _line(_msg("user", text)),
                _line(_assistant("Continuing.")),
            ]
        )
        result = CodexAdapter().parse_chunk(
            Path("/tmp/rollout-goal-context.jsonl"), data, start_offset=0
        )
        message = result.messages[0]
        self.assertEqual(message.role, "user")
        self.assertEqual(message.text, text)
        self.assertTrue(message.authored_by_agent)
        self.assertTrue(is_codex_internal_context_goal(text))

    def test_goal_internal_context_authored_flag_survives_repository_save(self) -> None:
        text = '<codex_internal_context source="goal">continue</codex_internal_context>'
        result = CodexAdapter().parse_chunk(
            Path("/tmp/rollout-goal-repository.jsonl"),
            _line(_msg("user", text)),
            start_offset=0,
        )
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "agentlog.db"
            conn = connect(db_path)
            init_db(conn)
            repo = Repository(conn)
            artifact_id = repo.upsert_artifact(
                harness="codex",
                path="goal.jsonl",
                size=1,
                mtime_ns=1,
                content_hash="goal",
                parsed_offset=1,
                parser_version="test",
            )
            repo.save_parse_result(
                artifact_id=artifact_id,
                result=result,
                append=False,
            )
            row = conn.execute(
                "SELECT role, text, authored_by_agent FROM messages"
            ).fetchone()
            self.assertEqual(row["role"], "user")
            self.assertEqual(row["text"], text)
            self.assertEqual(row["authored_by_agent"], 1)
            conn.close()

    def test_event_only_goal_internal_context_is_agent_authored(self) -> None:
        text = '<codex_internal_context source="goal">continue</codex_internal_context>'
        data = _line(
            {
                "type": "event_msg",
                "payload": {"type": "user_message", "message": text},
            }
        )
        result = CodexAdapter().parse_chunk(
            Path("/tmp/rollout-event-goal.jsonl"), data, start_offset=0
        )
        self.assertEqual(result.messages[0].role, "user")
        self.assertEqual(result.messages[0].text, text)
        self.assertTrue(result.messages[0].authored_by_agent)

    def test_goal_context_surrounded_by_human_text_is_not_synthetic(self) -> None:
        text = (
            'Please summarize this literal tag: '
            '<codex_internal_context source="goal">continue</codex_internal_context>'
        )
        self.assertFalse(is_codex_internal_context_goal(text))
        result = CodexAdapter().parse_chunk(
            Path("/tmp/rollout-human-goal-quote.jsonl"),
            _line(_msg("user", text)),
            start_offset=0,
        )
        self.assertFalse(result.messages[0].authored_by_agent)

    def test_tools_attach_to_preceding_assistant(self) -> None:
        data = b"".join(
            [
                _line(_msg("developer", "<permissions instructions>\nsandbox")),
                _line(_msg("user", "<recommended_plugins>\nplugin list")),
                _line(_msg("user", "Repository: /tmp/proj. READ-ONLY audit.")),
                _line(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "user_message",
                            "message": "Repository: /tmp/proj. READ-ONLY audit.",
                        },
                    }
                ),
                _line(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "agent_message",
                            "message": "I'll start reading files.",
                        },
                    }
                ),
                _line(_assistant("I'll start reading files.")),
                _line(_call("call_1")),
                _line(_call("call_2")),
                _line(_result("call_1")),
                _line(_result("call_2")),
                _line(_assistant("Done with the first pass.")),
                _line(_call("call_3")),
                _line(_result("call_3")),
            ]
        )
        result = CodexAdapter().parse_chunk(
            Path("/tmp/rollout-019f4cba-8a2e-7713-a257-a4d269d276a8.jsonl"),
            data,
            start_offset=0,
        )
        roles = [m.role for m in result.messages]
        self.assertEqual(
            roles, ["system", "user", "user", "assistant", "assistant"]
        )
        self.assertTrue(result.messages[0].authored_by_agent)
        self.assertEqual(result.messages[0].text[:12], "<permissions")
        self.assertIn("recommended_plugins", result.messages[1].text)

        self.assertEqual(len(result.tool_events), 6)
        first_assistant_seq = result.messages[3].seq
        second_assistant_seq = result.messages[4].seq
        for te in result.tool_events[:4]:
            self.assertEqual(te.message_seq, first_assistant_seq)
        for te in result.tool_events[4:]:
            self.assertEqual(te.message_seq, second_assistant_seq)
        self.assertEqual(result.tool_events[1].tool_name, "exec_command")
        self.assertEqual(result.tool_events[2].tool_name, "exec_command")
        self.assertTrue(result.tool_events[2].success)

    def test_tools_before_first_assistant_attach_to_it(self) -> None:
        data = b"".join(
            [
                _line(_msg("user", "audit this repo")),
                _line(_call("call_early")),
                _line(_result("call_early")),
                _line(_assistant("I ran a quick scan first.")),
            ]
        )
        result = CodexAdapter().parse_chunk(
            Path("/tmp/rollout-early-tools.jsonl"), data, start_offset=0
        )
        self.assertEqual(result.tool_events[0].message_seq, 2)
        self.assertEqual(result.messages[1].role, "assistant")

    def test_event_only_stream_still_links_tools(self) -> None:
        data = b"".join(
            [
                _line(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "user_message",
                            "message": "hello",
                        },
                    }
                ),
                _line(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "agent_message",
                            "message": "working",
                        },
                    }
                ),
                _line(_call("call_x", name="shell")),
                _line(_result("call_x")),
            ]
        )
        result = CodexAdapter().parse_chunk(
            Path("/tmp/rollout-event-only.jsonl"), data, start_offset=0
        )
        self.assertEqual([m.role for m in result.messages], ["user", "assistant"])
        self.assertEqual(len(result.tool_events), 2)
        self.assertEqual(result.tool_events[0].message_seq, 2)
        self.assertEqual(result.tool_events[0].tool_name, "shell")

    def test_event_msg_terminals_pair_and_preserve_outcomes(self) -> None:
        data = b"".join(
            [
                _line(_assistant("I will apply and test the change.")),
                _line(_call("patch-1", name="apply_patch")),
                _line(_terminal("patch_apply_end", "patch-1", success=True)),
                _line(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "custom_tool_call_output",
                            "call_id": "patch-1",
                            "success": True,
                        },
                    }
                ),
                _line(_call("test-1", name="exec_command")),
                _line(
                    _terminal(
                        "exec_command_end",
                        "test-1",
                        success=True,
                        exit_code=0,
                    )
                ),
                _line(_call("failed-1", name="exec_command")),
                _line(
                    _terminal(
                        "exec_command_end",
                        "failed-1",
                        success=False,
                        exit_code=1,
                    )
                ),
                _line(
                    _terminal(
                        "mcp_tool_call_end",
                        "mcp-ok",
                        invocation={"server": "search", "tool": "lookup"},
                        result={"Ok": {"count": 1}},
                    )
                ),
                _line(
                    _terminal(
                        "mcp_tool_call_end",
                        "mcp-fail",
                        invocation={"server": "files", "tool": "write"},
                        result={"Err": {"message": "denied"}},
                    )
                ),
            ]
        )
        result = CodexAdapter().parse_chunk(
            Path("/tmp/rollout-terminal-events.jsonl"), data, start_offset=0
        )

        self.assertEqual(len(result.tool_events), 8)
        terminals = [tool for tool in result.tool_events if tool.action != "call"]
        self.assertEqual(
            [tool.tool_name for tool in terminals],
            ["apply_patch", "exec_command", "exec_command", "lookup", "write"],
        )
        self.assertEqual(
            [tool.success for tool in terminals], [True, True, False, True, False]
        )

    def test_conflicting_success_signals_fail_closed(self) -> None:
        data = b"".join(
            [
                _line(_assistant("Run the check.")),
                _line(_call("single")),
                _line(_terminal("exec_command_end", "single", success=True, exit_code=1)),
                _line(_call("paired")),
                _line(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "function_call_output",
                            "call_id": "paired",
                            "success": True,
                        },
                    }
                ),
                _line(_terminal("exec_command_end", "paired", success=False, exit_code=1)),
            ]
        )
        result = CodexAdapter().parse_chunk(
            Path("/tmp/rollout-conflicting-outcomes.jsonl"), data, start_offset=0
        )
        terminals = [tool for tool in result.tool_events if tool.action != "call"]
        self.assertEqual([tool.success for tool in terminals], [None, None])

    def test_mcp_outcome_conflicts_fail_closed(self) -> None:
        data = b"".join(
            [
                _line(
                    _terminal(
                        "mcp_tool_call_end",
                        "mcp-both",
                        invocation={"server": "search", "tool": "lookup"},
                        result={"Ok": {}, "Err": {}},
                    )
                ),
                _line(
                    _terminal(
                        "mcp_tool_call_end",
                        "mcp-explicit",
                        success=True,
                        invocation={"server": "search", "tool": "lookup"},
                        result={"Err": {"message": "denied"}},
                    )
                ),
            ]
        )
        result = CodexAdapter().parse_chunk(
            Path("/tmp/rollout-conflicting-mcp.jsonl"), data, start_offset=0
        )
        self.assertEqual([tool.success for tool in result.tool_events], [None, None])

    def test_response_output_before_terminal_is_not_double_counted(self) -> None:
        data = b"".join(
            [
                _line(_assistant("Run the check.")),
                _line(_call("test-1", name="exec_command")),
                _line(_result("test-1", exit_code=0)),
                _line(_terminal("exec_command_end", "test-1", success=True)),
            ]
        )
        result = CodexAdapter().parse_chunk(
            Path("/tmp/rollout-terminal-order.jsonl"), data, start_offset=0
        )
        self.assertEqual(len(result.tool_events), 2)
        self.assertEqual([tool.action for tool in result.tool_events], ["call", "result"])
        self.assertTrue(result.tool_events[1].success)


if __name__ == "__main__":
    unittest.main()
