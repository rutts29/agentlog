from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agentlog.ingest.codex import CodexAdapter


PARENT_ID = "019ff310-46c8-7d82-85d2-a661e3a59008"
CHILD_ID = "019ff411-db9b-71c3-9171-aca94105fa49"
LOCAL_TURN_ID = "019ff411-dc0a-7d71-a0a3-c50a2a559166"


def _line(kind: str, payload: dict, timestamp: str) -> bytes:
    return (
        json.dumps({"timestamp": timestamp, "type": kind, "payload": payload})
        + "\n"
    ).encode()


def _message(message_id: str, role: str, text: str, turn_id: str) -> dict:
    return {
        "type": "message",
        "id": message_id,
        "role": role,
        "content": [{"type": "input_text", "text": text}],
        "internal_chat_message_metadata_passthrough": {"turn_id": turn_id},
    }


class CodexForkFilterTests(unittest.TestCase):
    def _fixture(self, *, inherited_id: str = "msg_parent_user") -> bytes:
        spawn_ts = "2026-08-12T03:43:58.977Z"
        return b"".join(
            [
                _line(
                    "session_meta",
                    {
                        "session_id": PARENT_ID,
                        "id": CHILD_ID,
                        "forked_from_id": PARENT_ID,
                        "parent_thread_id": PARENT_ID,
                        "timestamp": "2026-08-12T03:43:58.894Z",
                        "originator": "t3code_desktop",
                        "thread_source": "subagent",
                        "model_provider": "openai",
                        "source": {
                            "subagent": {
                                "thread_spawn": {
                                    "parent_thread_id": PARENT_ID,
                                    "agent_path": "/root/dependency_recheck",
                                    "agent_nickname": "Turing",
                                }
                            }
                        },
                    },
                    spawn_ts,
                ),
                _line(
                    "session_meta",
                    {"id": PARENT_ID, "thread_source": "user"},
                    spawn_ts,
                ),
                _line(
                    "event_msg",
                    {
                        "type": "task_started",
                        "turn_id": "053f73df-210d-4963-b8e5-f90c7665453f",
                        "started_at": 1786505327,
                    },
                    "2026-08-12T03:43:58.981Z",
                ),
                _line(
                    "response_item",
                    _message(
                        inherited_id,
                        "user",
                        "portfolio request copied from the parent",
                        "053f73df-210d-4963-b8e5-f90c7665453f",
                    ),
                    "2026-08-12T03:43:58.981Z",
                ),
                _line(
                    "response_item",
                    {
                        "type": "function_call",
                        "id": "fc_parent",
                        "call_id": "call_parent",
                        "name": "web_search",
                    },
                    "2026-08-12T03:43:58.981Z",
                ),
                _line(
                    "response_item",
                    {
                        "type": "function_call_output",
                        "id": "fco_parent",
                        "call_id": "call_parent",
                    },
                    "2026-08-12T03:43:58.981Z",
                ),
                _line(
                    "event_msg",
                    {
                        "type": "token_count",
                        "info": {
                            "last_token_usage": {"input_tokens": 999},
                            "total_token_usage": {"input_tokens": 9999},
                        },
                    },
                    "2026-08-12T03:43:58.981Z",
                ),
                _line(
                    "turn_context",
                    {"turn_id": "053f73df-210d-4963-b8e5-f90c7665453f"},
                    "2026-08-12T03:43:58.981Z",
                ),
                _line(
                    "inter_agent_communication_metadata",
                    {"trigger_turn": True},
                    "2026-08-12T03:43:58.981Z",
                ),
                _line(
                    "event_msg",
                    {
                        "type": "task_started",
                        "turn_id": LOCAL_TURN_ID,
                        "started_at": 1786506238,
                    },
                    "2026-08-12T03:43:58.988Z",
                ),
                _line(
                    "response_item",
                    _message(
                        "msg_local_developer",
                        "developer",
                        "worker runtime envelope",
                        LOCAL_TURN_ID,
                    ),
                    "2026-08-12T03:44:01.178Z",
                ),
                _line(
                    "response_item",
                    _message(
                        "msg_local_brief",
                        "user",
                        "Message Type: NEW_TASK\nPayload: verify dependencies",
                        LOCAL_TURN_ID,
                    ),
                    "2026-08-12T03:44:01.179Z",
                ),
                _line(
                    "turn_context",
                    {
                        "turn_id": LOCAL_TURN_ID,
                        "model": "gpt-5.6-sol",
                        "effort": "ultra",
                    },
                    "2026-08-12T03:44:01.180Z",
                ),
                _line(
                    "inter_agent_communication_metadata",
                    {"trigger_turn": True},
                    "2026-08-12T03:44:01.188Z",
                ),
                _line(
                    "response_item",
                    {
                        "type": "agent_message",
                        "id": "amsg_local_brief",
                        "author": "/root",
                        "recipient": "/root/dependency_recheck",
                        "content": [
                            {
                                "type": "input_text",
                                "text": (
                                    "Message Type: NEW_TASK\n"
                                    "Task name: /root/dependency_recheck\n"
                                    "Sender: /root\nPayload:\n"
                                ),
                            },
                            {"type": "encrypted_content", "encrypted_content": "x"},
                        ],
                    },
                    "2026-08-12T03:44:01.188Z",
                ),
                _line(
                    "response_item",
                    _message(
                        "msg_local_agent",
                        "assistant",
                        "Checking the dependency graph.",
                        LOCAL_TURN_ID,
                    ),
                    "2026-08-12T03:44:02Z",
                ),
                _line(
                    "response_item",
                    {
                        "type": "function_call",
                        "id": "fc_local",
                        "call_id": "call_local",
                        "name": "exec_command",
                    },
                    "2026-08-12T03:44:03Z",
                ),
                _line(
                    "event_msg",
                    {
                        "type": "token_count",
                        "info": {
                            "last_token_usage": {"input_tokens": 11},
                            "total_token_usage": {"input_tokens": 17},
                        },
                    },
                    "2026-08-12T03:44:04Z",
                ),
            ]
        )

    def test_full_history_fork_only_counts_child_local_activity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sessions = Path(tmp)
            parent = sessions / f"rollout-test-{PARENT_ID}.jsonl"
            parent.write_bytes(
                b"".join([_line(
                    "response_item",
                    _message(
                        "msg_parent_user",
                        "user",
                        "portfolio request copied from the parent",
                        "053f73df-210d-4963-b8e5-f90c7665453f",
                    ),
                    "2026-08-12T03:20:00Z",
                ),
                _line("response_item", {
                    "type": "function_call", "id": "fc_parent",
                    "call_id": "call_parent", "name": "web_search",
                }, "2026-08-12T03:20:01Z"),
                _line("response_item", {
                    "type": "function_call_output", "id": "fco_parent",
                    "call_id": "call_parent",
                }, "2026-08-12T03:20:02Z")])
            )
            with mock.patch(
                "agentlog.ingest.codex.CODEX_SESSIONS_DIR", sessions
            ):
                result = CodexAdapter().parse_chunk(
                    sessions / f"rollout-{CHILD_ID}.jsonl",
                    self._fixture(),
                    start_offset=0,
                )

        self.assertEqual(result.session.external_id, CHILD_ID)
        self.assertEqual(result.session.parent_session_id, PARENT_ID)
        self.assertEqual(result.session.originator, "t3code_desktop")
        self.assertEqual(result.session.thread_source, "subagent")
        self.assertEqual(
            [(m.seq, m.role, m.text) for m in result.messages],
            [
                (1, "system", "worker runtime envelope"),
                (2, "user", "Message Type: NEW_TASK\nPayload: verify dependencies"),
                (
                    3,
                    "user",
                    "Message Type: NEW_TASK\nTask name: /root/dependency_recheck\n"
                    "Sender: /root\nPayload:\n",
                ),
                (4, "assistant", "Checking the dependency graph."),
            ],
        )
        self.assertTrue(result.messages[1].authored_by_agent)
        self.assertTrue(result.messages[2].authored_by_agent)
        self.assertEqual([tool.tool_name for tool in result.tool_events], ["exec_command"])
        self.assertEqual([usage.input_tokens for usage in result.token_usages], [11, 17])
        self.assertEqual(result.extras["inherited_message_count"], 1)
        self.assertEqual(result.extras["inherited_record_count"], 8)
        self.assertEqual(result.extras["fork_context_status"], "verified_parent")
        self.assertEqual(result.extras["fork_context_boundary"], LOCAL_TURN_ID)

    def test_t3_full_history_worker_keeps_only_local_runtime_and_brief(self) -> None:
        parent_id = "019febd9-95f2-7ea3-82c7-f13290099c71"
        worker_id = "019ff658-ee6a-7590-ae91-a4e8b9fe8110"
        parent_turn = "019ff61d-0cd9-7270-91d6-86c9d85df833"
        worker_turn = "019ff658-eed0-7651-b675-1d6334c996c8"
        parent_owner_text = "parent-owner-only-marker"
        worker_brief = "Message Type: NEW_TASK\nPayload: local-worker-brief"
        data = b"".join(
            [
                _line(
                    "session_meta",
                    {
                        "id": worker_id,
                        "session_id": parent_id,
                        "forked_from_id": parent_id,
                        "parent_thread_id": parent_id,
                        "timestamp": "2026-08-12T19:50:51Z",
                        "originator": "t3code_desktop",
                        "thread_source": "subagent",
                        "source": {
                            "subagent": {
                                "thread_spawn": {
                                    "parent_thread_id": parent_id,
                                    "agent_path": "/root/local_worker",
                                    "agent_nickname": "Worker",
                                    "agent_role": None,
                                }
                            }
                        },
                    },
                    "2026-08-12T19:50:51Z",
                ),
                _line("session_meta", {"id": parent_id}, "2026-08-12T19:50:51Z"),
                _line(
                    "response_item",
                    _message("msg_parent_owner", "user", parent_owner_text, parent_turn),
                    "2026-08-12T19:50:50Z",
                ),
                _line(
                    "event_msg",
                    {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 999}}},
                    "2026-08-12T19:50:50Z",
                ),
                _line(
                    "event_msg",
                    {"type": "task_started", "turn_id": worker_turn, "started_at": 1786564251},
                    "2026-08-12T19:50:51Z",
                ),
                *[
                    _line(
                        "response_item",
                        _message(message_id, "developer", text, worker_turn),
                        "2026-08-12T19:50:52Z",
                    )
                    for message_id, text in (
                        ("msg_runtime", "runtime context"),
                        ("msg_permissions", "runtime permissions"),
                        ("msg_tools", "runtime tools"),
                    )
                ],
                _line(
                    "response_item",
                    _message(
                        "msg_goal_context",
                        "user",
                        '<codex_internal_context source="goal">runtime goal</codex_internal_context>',
                        worker_turn,
                    ),
                    "2026-08-12T19:50:52Z",
                ),
                _line("world_state", {}, "2026-08-12T19:50:52Z"),
                _line("turn_context", {"turn_id": worker_turn}, "2026-08-12T19:50:52Z"),
                _line(
                    "inter_agent_communication_metadata",
                    {"trigger_turn": True},
                    "2026-08-12T19:50:52Z",
                ),
                _line(
                    "response_item",
                    {
                        "type": "agent_message",
                        "id": "amsg_local_brief",
                        "author": "/root",
                        "recipient": "/root/local_worker",
                        "content": [{"type": "input_text", "text": worker_brief}],
                    },
                    "2026-08-12T19:50:52Z",
                ),
                _line(
                    "response_item",
                    _message("msg_local_assistant", "assistant", "local assistant row", worker_turn),
                    "2026-08-12T19:50:53Z",
                ),
                _line(
                    "response_item",
                    {"type": "function_call", "id": "fc_local", "call_id": "call_local", "name": "exec_command"},
                    "2026-08-12T19:50:54Z",
                ),
                _line(
                    "response_item",
                    {"type": "function_call_output", "id": "fco_local", "call_id": "call_local"},
                    "2026-08-12T19:50:55Z",
                ),
                _line(
                    "event_msg",
                    {
                        "type": "token_count",
                        "info": {
                            "last_token_usage": {"input_tokens": 7},
                            "total_token_usage": {"input_tokens": 12},
                        },
                    },
                    "2026-08-12T19:50:56Z",
                ),
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            sessions = Path(tmp)
            (sessions / f"rollout-{parent_id}.jsonl").write_bytes(
                _line(
                    "response_item",
                    _message("msg_parent_owner", "user", "parent-copy", parent_turn),
                    "2026-08-12T19:40:00Z",
                )
            )
            with mock.patch("agentlog.ingest.codex.CODEX_SESSIONS_DIR", sessions):
                result = CodexAdapter().parse_chunk(
                    sessions / f"rollout-{worker_id}.jsonl", data, start_offset=0
                )

        self.assertEqual(
            [(message.role, message.text) for message in result.messages],
            [
                ("system", "runtime context"),
                ("system", "runtime permissions"),
                ("system", "runtime tools"),
                ("user", '<codex_internal_context source="goal">runtime goal</codex_internal_context>'),
                ("user", worker_brief),
                ("assistant", "local assistant row"),
            ],
        )
        self.assertTrue(all(message.authored_by_agent for message in result.messages[:5]))
        self.assertNotIn(parent_owner_text, [message.text for message in result.messages])
        self.assertEqual(
            [(tool.tool_name, tool.action) for tool in result.tool_events],
            [("exec_command", "call"), ("exec_command", "result")],
        )
        self.assertEqual([usage.input_tokens for usage in result.token_usages], [7, 12])
        self.assertEqual(result.extras["inherited_message_count"], 1)
        self.assertEqual(result.extras["inherited_record_count"], 3)
        self.assertEqual(result.extras["fork_context_status"], "verified_parent")
        self.assertEqual(result.extras["fork_context_boundary"], worker_turn)

    def test_parent_id_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sessions = Path(tmp)
            (sessions / f"rollout-test-{PARENT_ID}.jsonl").write_bytes(
                _line(
                    "response_item",
                    _message("different_id", "user", "other", "parent-turn"),
                    "2026-08-12T03:20:00Z",
                )
            )
            with mock.patch(
                "agentlog.ingest.codex.CODEX_SESSIONS_DIR", sessions
            ):
                result = CodexAdapter().parse_chunk(
                    sessions / f"rollout-{CHILD_ID}.jsonl",
                    self._fixture(inherited_id="not_in_parent"),
                    start_offset=0,
                )

        self.assertEqual(result.messages, [])
        self.assertEqual(result.tool_events, [])
        self.assertEqual(result.token_usages, [])
        self.assertEqual(result.extras["fork_context_status"], "ambiguous")
        self.assertTrue(result.extras["checkpoint_blocked"])
        self.assertTrue(any("activity omitted" in warning for warning in result.warnings))

    def test_no_replay_child_keeps_first_local_turn(self) -> None:
        data = b"".join(
            [
                _line(
                    "session_meta",
                    {
                        "id": CHILD_ID,
                        "forked_from_id": PARENT_ID,
                        "parent_thread_id": PARENT_ID,
                        "timestamp": "2026-08-12T03:43:58Z",
                        "thread_source": "subagent",
                        "source": {
                            "subagent": {
                                "thread_spawn": {"agent_nickname": "Worker"}
                            }
                        },
                    },
                    "2026-08-12T03:43:58Z",
                ),
                _line(
                    "event_msg",
                    {
                        "type": "task_started",
                        "turn_id": LOCAL_TURN_ID,
                        "started_at": 1786506238,
                    },
                    "2026-08-12T03:43:58Z",
                ),
                _line(
                    "response_item",
                    _message(
                        "msg_brief", "user", "worker brief", LOCAL_TURN_ID
                    ),
                    "2026-08-12T03:44:00Z",
                ),
                _line(
                    "turn_context",
                    {"turn_id": LOCAL_TURN_ID},
                    "2026-08-12T03:44:00Z",
                ),
                _line(
                    "inter_agent_communication_metadata",
                    {"trigger_turn": True},
                    "2026-08-12T03:44:00Z",
                ),
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            sessions = Path(tmp)
            with mock.patch(
                "agentlog.ingest.codex.CODEX_SESSIONS_DIR", sessions
            ):
                result = CodexAdapter().parse_chunk(
                    sessions / f"rollout-{CHILD_ID}.jsonl",
                    data,
                    start_offset=0,
                )

        self.assertEqual([m.text for m in result.messages], ["worker brief"])
        self.assertTrue(result.messages[0].authored_by_agent)
        self.assertEqual(result.extras["inherited_message_count"], 0)
        self.assertEqual(result.extras["inherited_record_count"], 0)
        self.assertIsNone(result.extras["fork_context_status"])
        self.assertFalse(result.extras["checkpoint_blocked"])

    def test_later_local_human_turn_is_not_trimmed(self) -> None:
        second_turn = "019ff412-0000-7000-8000-000000000000"
        data = self._fixture() + b"".join(
            [
                _line(
                    "event_msg",
                    {
                        "type": "task_started",
                        "turn_id": second_turn,
                        "started_at": 1786506300,
                    },
                    "2026-08-12T03:45:00Z",
                ),
                _line(
                    "response_item",
                    _message(
                        "msg_followup", "user", "actual human follow-up", second_turn
                    ),
                    "2026-08-12T03:45:01Z",
                ),
                _line(
                    "turn_context",
                    {"turn_id": second_turn},
                    "2026-08-12T03:45:01Z",
                ),
                _line(
                    "inter_agent_communication_metadata",
                    {"trigger_turn": True},
                    "2026-08-12T03:45:01Z",
                ),
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            sessions = Path(tmp)
            parent = sessions / f"rollout-test-{PARENT_ID}.jsonl"
            parent.write_bytes(
                b"".join(
                    [
                        _line(
                            "response_item",
                            _message(
                                "msg_parent_user",
                                "user",
                                "portfolio request copied from the parent",
                                "053f73df-210d-4963-b8e5-f90c7665453f",
                            ),
                            "2026-08-12T03:20:00Z",
                        ),
                        _line(
                            "response_item",
                            {"type": "function_call", "id": "fc_parent"},
                            "2026-08-12T03:20:01Z",
                        ),
                        _line(
                            "response_item",
                            {"type": "function_call_output", "id": "fco_parent"},
                            "2026-08-12T03:20:02Z",
                        ),
                    ]
                )
            )
            with mock.patch(
                "agentlog.ingest.codex.CODEX_SESSIONS_DIR", sessions
            ):
                result = CodexAdapter().parse_chunk(
                    sessions / f"rollout-{CHILD_ID}.jsonl",
                    data,
                    start_offset=0,
                )

        followup = next(m for m in result.messages if m.text == "actual human follow-up")
        self.assertFalse(followup.authored_by_agent)
        self.assertEqual(result.extras["fork_context_boundary"], LOCAL_TURN_ID)

    def test_native_guardian_without_trigger_metadata_is_entirely_local(self) -> None:
        native_id = "019e8917-690d-7730-a6cb-3d362449ecae"
        turn_id = "019e8917-695f-7312-8681-6b9cd4444236"
        data = b"".join(
            [
                _line(
                    "session_meta",
                    {
                        "id": native_id,
                        "forked_from_id": PARENT_ID,
                        "timestamp": "2026-06-02T16:07:53.443Z",
                        "originator": "Codex Desktop",
                        "thread_source": "subagent",
                        "source": {"subagent": {"other": "guardian"}},
                    },
                    "2026-06-02T16:07:53.443Z",
                ),
                _line(
                    "event_msg",
                    {
                        "type": "task_started",
                        "turn_id": turn_id,
                        "started_at": 1780416473,
                    },
                    "2026-06-02T16:07:53.447Z",
                ),
                _line(
                    "response_item",
                    _message("msg_guardian", "user", "review this", turn_id),
                    "2026-06-02T16:07:54.906Z",
                ),
                _line(
                    "turn_context",
                    {"turn_id": turn_id, "model": "codex-auto-review"},
                    "2026-06-02T16:07:54.906Z",
                ),
                _line(
                    "response_item",
                    _message("msg_review", "assistant", "looks safe", turn_id),
                    "2026-06-02T16:07:58.252Z",
                ),
            ]
        )
        result = CodexAdapter().parse_chunk(
            Path(f"/tmp/rollout-{native_id}.jsonl"), data, start_offset=0
        )

        self.assertEqual(
            [m.text for m in result.messages], ["review this", "looks safe"]
        )
        self.assertTrue(result.messages[0].authored_by_agent)
        self.assertEqual(result.session.parent_session_id, PARENT_ID)
        self.assertIsNone(result.extras["fork_context_status"])
        self.assertFalse(result.extras["checkpoint_blocked"])
        self.assertEqual(result.extras["inherited_message_count"], 0)
        self.assertEqual(result.extras["inherited_record_count"], 0)

    def test_outgoing_worker_message_is_not_a_root_user_turn(self) -> None:
        data = b"".join(
            [
                _line(
                    "session_meta",
                    {"id": "root", "thread_source": "user"},
                    "2026-08-12T03:43:58Z",
                ),
                _line(
                    "inter_agent_communication_metadata",
                    {"trigger_turn": False},
                    "2026-08-12T03:44:00Z",
                ),
                _line(
                    "response_item",
                    {
                        "type": "agent_message",
                        "id": "amsg_worker_reply",
                        "author": "/root/worker",
                        "recipient": "/root",
                        "content": [
                            {
                                "type": "input_text",
                                "text": "Message Type: FINAL_ANSWER\nPayload: done",
                            }
                        ],
                    },
                    "2026-08-12T03:44:00Z",
                ),
                _line(
                    "response_item",
                    _message("msg_root", "assistant", "Acknowledged.", "root-turn"),
                    "2026-08-12T03:44:01Z",
                ),
            ]
        )
        result = CodexAdapter().parse_chunk(
            Path("/tmp/rollout-root.jsonl"), data, start_offset=0
        )

        self.assertEqual([m.text for m in result.messages], ["Acknowledged."])

    def test_sibling_or_unrecognized_agent_message_is_not_a_worker_brief(self) -> None:
        data = b"".join(
            [
                _line(
                    "session_meta",
                    {
                        "id": "child",
                        "thread_source": "subagent",
                        "source": {
                            "subagent": {
                                "thread_spawn": {"agent_path": "/root/child"}
                            }
                        },
                    },
                    "2026-08-12T03:43:58Z",
                ),
                _line(
                    "inter_agent_communication_metadata",
                    {"trigger_turn": True},
                    "2026-08-12T03:44:00Z",
                ),
                _line(
                    "response_item",
                    {
                        "type": "agent_message",
                        "id": "amsg_sibling",
                        "author": "/root",
                        "recipient": "/root/sibling",
                        "content": [
                            {"type": "input_text", "text": "Message Type: NEW_TASK"}
                        ],
                    },
                    "2026-08-12T03:44:00Z",
                ),
                _line(
                    "inter_agent_communication_metadata",
                    {"trigger_turn": True},
                    "2026-08-12T03:44:01Z",
                ),
                _line(
                    "response_item",
                    {
                        "type": "agent_message",
                        "id": "amsg_crafted",
                        "author": "/root",
                        "recipient": "/root/child",
                        "content": [
                            {"type": "input_text", "text": "arbitrary injected text"}
                        ],
                    },
                    "2026-08-12T03:44:01Z",
                ),
                _line(
                    "response_item",
                    _message("msg_child", "assistant", "Working.", "child-turn"),
                    "2026-08-12T03:44:02Z",
                ),
            ]
        )
        result = CodexAdapter().parse_chunk(
            Path("/tmp/rollout-child.jsonl"), data, start_offset=0
        )

        self.assertEqual([m.text for m in result.messages], ["Working."])

    def test_incomplete_trailing_line_is_not_checkpointed(self) -> None:
        complete = _line(
            "session_meta",
            {"id": "root", "thread_source": "user"},
            "2026-08-12T03:43:58Z",
        )
        data = complete + b'{"type":"response_item","payload":'
        result = CodexAdapter().parse_chunk(
            Path("/tmp/rollout-root.jsonl"), data, start_offset=0
        )

        self.assertEqual(result.bytes_consumed, len(complete))
        self.assertTrue(any("incomplete trailing line" in w for w in result.warnings))


if __name__ == "__main__":
    unittest.main()
