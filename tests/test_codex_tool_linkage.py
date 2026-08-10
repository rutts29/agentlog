from __future__ import annotations

import json
import unittest
from pathlib import Path

from agentlog.ingest.codex import CodexAdapter


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


class CodexToolLinkageTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
