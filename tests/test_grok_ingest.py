from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agentlog.ingest.grok import GrokAdapter
from agentlog.normalize.models import Harness
from agentlog.registry.harnesses import get_harness


def _line(value: dict, *, newline: bool = True) -> bytes:
    raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
    return raw + (b"\n" if newline else b"")


def _fixture() -> bytes:
    return b"".join(
        [
            _line({"type": "system", "content": "internal policy"}),
            _line(
                {
                    "type": "user",
                    "synthetic_reason": "compaction_meta",
                    "content": [{"type": "text", "text": "old summary"}],
                }
            ),
            _line(
                {
                    "type": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "<user_query>please inspect the adapter</user_query>",
                        }
                    ],
                }
            ),
            _line(
                {
                    "type": "assistant",
                    "model_id": "grok-4.6-build",
                    "reasoning_effort": "xhigh",
                    "content": "I will inspect it.",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "name": "use_tool",
                            "arguments": json.dumps(
                                {
                                    "tool_name": "run_terminal_command",
                                    "tool_input": {"command": "git status"},
                                }
                            ),
                        }
                    ],
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 4,
                        "total_tokens": 14,
                    },
                }
            ),
            _line(
                {
                    "type": "tool_result",
                    "tool_call_id": "call-1",
                    "content": "clean",
                }
            ),
            _line(
                {
                    "type": "user",
                    "synthetic_reason": "system_reminder",
                    "content": [{"type": "text", "text": "do not expose this"}],
                }
            ),
            _line(
                {
                    "type": "assistant",
                    "model_id": "grok-4.6-build",
                    "reasoning_effort": "xhigh",
                    "content": "The adapter is covered.",
                },
                newline=False,
            ),
        ]
    )


class GrokAdapterTests(unittest.TestCase):
    def test_discovers_safe_session_paths_and_decodes_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sessions"
            workspace = root / "%2Ftmp%2Fagentlog"
            session = workspace / "session-1"
            session.mkdir(parents=True)
            path = session / "chat_history.jsonl"
            path.write_bytes(_fixture())
            (session / "summary.json").write_text(
                json.dumps(
                    {
                        "created_at": "2026-08-14T00:00:00Z",
                        "updated_at": "2026-08-14T00:01:00Z",
                        "current_model_id": "grok-4.6",
                        "reasoning_effort": "xhigh",
                        "git_root_dir": "/tmp/agentlog",
                        "head_branch": "main",
                        "head_commit": "abc123",
                        "agent_name": "grok-build-plan",
                    }
                )
            )
            outside = Path(tmp) / "outside.jsonl"
            outside.write_bytes(_fixture())
            (root / "bad").mkdir()
            (root / "bad" / "not-a-session.jsonl").write_bytes(_fixture())
            with mock.patch("agentlog.ingest.grok.GROK_SESSIONS_DIR", root):
                adapter = GrokAdapter()
                self.assertEqual(adapter.discover(), [path])
                self.assertTrue(adapter.accepts_watch_path(path, root))
                result = adapter.parse_chunk(path, path.read_bytes(), start_offset=0)

        self.assertEqual(result.session.harness, Harness.GROK)
        self.assertEqual(result.session.external_id, "session-1")
        self.assertEqual(result.session.cwd, "/tmp/agentlog")
        self.assertEqual(result.session.repo, "/tmp/agentlog")
        self.assertEqual(result.session.model, "grok-4.6-build")
        self.assertEqual(result.session.provider, "xai")
        self.assertEqual(result.session.effort, "xhigh")
        self.assertEqual(result.session.branch, "main")
        self.assertEqual(result.session.commit_sha, "abc123")
        self.assertEqual(result.session.agent_profile, "grok-build-plan")

    def test_excludes_synthetic_context_and_preserves_tools_usage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sessions"
            path = root / "%2Ftmp%2Fagentlog" / "session-1" / "chat_history.jsonl"
            path.parent.mkdir(parents=True)
            data = _fixture()
            path.write_bytes(data)
            with mock.patch("agentlog.ingest.grok.GROK_SESSIONS_DIR", root):
                result = GrokAdapter().parse_chunk(path, data, start_offset=0)

        human = [
            (m.role, m.text)
            for m in result.messages
            if not m.is_tool_plumbing and not m.authored_by_agent
        ]
        self.assertEqual(
            human,
            [
                ("user", "please inspect the adapter"),
                ("assistant", "I will inspect it."),
                ("assistant", "The adapter is covered."),
            ],
        )
        self.assertGreaterEqual(sum(m.role == "system" for m in result.messages), 1)
        self.assertEqual(
            sum(m.is_tool_plumbing for m in result.messages),
            3,
        )
        self.assertEqual(
            [(event.tool_name, event.action, event.message_seq) for event in result.tool_events],
            [("run_terminal_command", "call", 4), ("run_terminal_command", "result", 4)],
        )
        self.assertEqual(result.token_usages[0].total_tokens, 14)
        self.assertEqual(result.bytes_consumed, len(data))

    def test_workflow_call_keeps_nested_tool_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sessions"
            path = root / "%2Ftmp%2Fagentlog" / "session-1" / "chat_history.jsonl"
            path.parent.mkdir(parents=True)
            data = _line(
                {
                    "type": "assistant",
                    "model_id": "grok-4.6-build",
                    "content": "Delegating bounded work.",
                    "tool_calls": [
                        {
                            "id": "workflow-1",
                            "name": "use_tool",
                            "arguments": json.dumps(
                                {
                                    "tool_name": "spawn_subagent",
                                    "tool_input": {
                                        "description": "review one packet",
                                        "model": "grok-4.6",
                                    },
                                }
                            ),
                        }
                    ],
                }
            )
            path.write_bytes(data)
            with mock.patch("agentlog.ingest.grok.GROK_SESSIONS_DIR", root):
                result = GrokAdapter().parse_chunk(path, data, start_offset=0)
        self.assertEqual(result.tool_events[0].tool_name, "spawn_subagent")
        self.assertEqual(result.tool_events[0].action, "call")

    def test_trailing_partial_record_is_not_checkpointed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sessions"
            path = root / "%2Ftmp%2Fagentlog" / "session-1" / "chat_history.jsonl"
            path.parent.mkdir(parents=True)
            complete = _line({"type": "user", "content": "hello"})
            path.write_bytes(complete + b'{"type":"assistant"')
            with mock.patch("agentlog.ingest.grok.GROK_SESSIONS_DIR", root):
                result = GrokAdapter().parse_chunk(path, path.read_bytes(), start_offset=0)
        self.assertEqual(result.bytes_consumed, len(complete))
        self.assertTrue(any("incomplete trailing line" in w for w in result.warnings))

    def test_registry_declares_grok_build(self) -> None:
        record = get_harness("grok")
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record["display_name"], "Grok Build")
        self.assertEqual(record["ingest_status"], "active")

    def test_child_capture_uses_parent_metadata_and_workflow_without_rereading(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sessions"
            workspace = root / "%2Ftmp%2Fagentlog"
            parent = workspace / "parent"
            child = workspace / "child"
            child.mkdir(parents=True)
            parent_meta = parent / "subagents" / "child"
            parent_meta.mkdir(parents=True)
            child_path = child / "chat_history.jsonl"
            child_path.write_bytes(
                b"".join(
                    [
                        _line({"type": "user", "content": "audit the source"}),
                        _line(
                            {
                                "type": "assistant",
                                "content": "checking the implementation",
                                "tool_calls": [{"id": "call-1", "name": "read_file"}],
                                "usage": {"input_tokens": 5, "output_tokens": 3},
                            }
                        ),
                        _line({"type": "tool_result", "tool_call_id": "call-1"}),
                        _line({"type": "assistant", "content": "completed review"}),
                    ]
                )
            )
            (child / "summary.json").write_text("{}")
            (parent_meta / "meta.json").write_text(json.dumps({
                "child_session_id": "child", "parent_session_id": "parent",
                "prompt": "audit the source", "model": "grok-4.6",
            }))
            (parent_meta / "output.json").write_text(json.dumps({"schema_version": 1, "output": "completed review"}))
            workflow = parent / "workflows" / "packet"
            workflow.mkdir(parents=True)
            (workflow / "state.json").write_text(json.dumps({"state": {
                "run_id": "workflow-1", "name": "Extract", "agents": [{"agent_id": "child"}]
            }}))
            with mock.patch("agentlog.ingest.grok.GROK_SESSIONS_DIR", root):
                adapter = GrokAdapter()
                snapshot = adapter.capture_source(child_path)
                (parent_meta / "output.json").write_text(json.dumps({"output": "rewritten"}))
                parsed = adapter.parse_source_snapshot(child_path, snapshot)[0]
                self.assertFalse(adapter.composite_snapshot_matches(
                    child_path, revision=snapshot.revision, content_hash=snapshot.content_hash
                ))

        self.assertEqual(parsed.session.parent_session_id, "grok:parent")
        self.assertEqual(parsed.session.thread_source, "workflow_subagent")
        self.assertEqual(
            [item.text for item in parsed.messages],
            ["audit the source", "checking the implementation", "completed review"],
        )
        self.assertTrue(parsed.messages[0].authored_by_agent)
        self.assertEqual([(event.tool_name, event.action) for event in parsed.tool_events], [("read_file", "call"), ("read_file", "result")])
        self.assertEqual(parsed.token_usages[0].input_tokens, 5)
        self.assertEqual(parsed.token_usages[0].output_tokens, 3)
        self.assertEqual(parsed.extras["workflow_group"]["id"], "workflow-1")

    def test_compacted_child_keeps_parent_metadata_and_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sessions"
            workspace = root / "%2Ftmp%2Fagentlog"
            parent = workspace / "parent"
            child = workspace / "child"
            child.mkdir(parents=True)
            parent_meta = parent / "subagents" / "child"
            parent_meta.mkdir(parents=True)
            child_path = child / "chat_history.jsonl"
            compacted_prefix = [
                {"type": "user", "synthetic_reason": "compaction_meta", "content": "compacted child state"},
            ]
            child_path.write_bytes(
                b"".join(
                    _line(value)
                    for value in [
                        *compacted_prefix,
                        {"type": "user", "content": "continue the child review"},
                        {"type": "assistant", "content": "child review complete"},
                    ]
                )
            )
            (child / "summary.json").write_text("{}")
            requests = child / "compaction_requests"
            checkpoints = child / "compaction_checkpoints"
            requests.mkdir()
            checkpoints.mkdir()
            (requests / "request-1.json").write_text(json.dumps({
                "created_at": "2026-08-14T00:00:00Z",
                "chat_history": [
                    {"type": "user", "content": "original child request"},
                    {"type": "assistant", "content": "initial child analysis"},
                ],
            }))
            (checkpoints / "checkpoint-1.json").write_text(json.dumps({
                "created_at": "2026-08-14T00:01:00Z",
                "request_id": "request-1",
                "compacted_history": compacted_prefix,
            }))
            (parent_meta / "meta.json").write_text(json.dumps({
                "child_session_id": "child", "parent_session_id": "parent",
                "prompt": "original child request", "model": "grok-4.6",
            }))
            (parent_meta / "output.json").write_text(json.dumps({"output": "child review complete"}))
            workflow = parent / "workflows" / "packet"
            workflow.mkdir(parents=True)
            (workflow / "state.json").write_text(json.dumps({"state": {
                "run_id": "workflow-1", "name": "Extract", "agents": [{"agent_id": "child"}]
            }}))

            with mock.patch("agentlog.ingest.grok.GROK_SESSIONS_DIR", root):
                adapter = GrokAdapter()
                snapshot = adapter.capture_source(child_path)
                parsed = adapter.parse_source_snapshot(child_path, snapshot)[0]

        self.assertEqual(parsed.session.parent_session_id, "grok:parent")
        self.assertEqual(parsed.session.thread_source, "workflow_subagent")
        self.assertEqual(parsed.extras["workflow_group"]["id"], "workflow-1")
        self.assertIn(str((parent_meta / "meta.json").resolve()), parsed.extras["source_dependencies"])
        self.assertIn(str((parent_meta / "output.json").resolve()), parsed.extras["source_dependencies"])
        self.assertIn(str((workflow / "state.json").resolve()), parsed.extras["source_dependencies"])
        self.assertEqual(
            [item.text for item in parsed.messages],
            ["original child request", "initial child analysis", "continue the child review", "child review complete"],
        )
        self.assertTrue(any("reconstructed compaction boundary" in warning for warning in parsed.warnings))

    def test_child_metadata_supplements_missing_local_brief_and_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sessions"
            workspace = root / "%2Ftmp%2Fagentlog"
            parent = workspace / "parent"
            child = workspace / "child"
            child.mkdir(parents=True)
            parent_meta = parent / "subagents" / "child"
            parent_meta.mkdir(parents=True)
            child_path = child / "chat_history.jsonl"
            child_path.write_bytes(
                b"".join(
                    [
                        _line({"type": "user", "content": "local follow-up"}),
                        _line({"type": "assistant", "content": "local conclusion"}),
                    ]
                )
            )
            (child / "summary.json").write_text("{}")
            (parent_meta / "meta.json").write_text(json.dumps({
                "child_session_id": "child", "parent_session_id": "parent",
                "prompt": "parent brief", "effective_model_id": "grok-4.6",
            }))
            (parent_meta / "output.json").write_text(json.dumps({"output": "final output"}))

            with mock.patch("agentlog.ingest.grok.GROK_SESSIONS_DIR", root):
                adapter = GrokAdapter()
                parsed = adapter.parse_source_snapshot(
                    child_path, adapter.capture_source(child_path)
                )[0]

        self.assertEqual(
            [item.text for item in parsed.messages],
            ["parent brief", "local follow-up", "local conclusion", "final output"],
        )
        self.assertTrue(parsed.messages[0].authored_by_agent)
        self.assertFalse(parsed.messages[1].authored_by_agent)

    def test_duplicate_child_parent_metadata_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sessions"
            workspace = root / "%2Ftmp%2Fagentlog"
            child = workspace / "child"
            child.mkdir(parents=True)
            (child / "compaction_requests").mkdir()
            child_path = child / "chat_history.jsonl"
            child_path.write_bytes(_line({"type": "user", "content": "review"}))
            (child / "summary.json").write_text("{}")
            for parent_id in ("parent-a", "parent-b"):
                metadata = workspace / parent_id / "subagents" / "child"
                metadata.mkdir(parents=True)
                (metadata / "meta.json").write_text(json.dumps({
                    "child_session_id": "child", "parent_session_id": parent_id,
                }))

            with mock.patch("agentlog.ingest.grok.GROK_SESSIONS_DIR", root):
                with self.assertRaisesRegex(OSError, "ambiguous Grok parent metadata"):
                    GrokAdapter().capture_source(child_path)


if __name__ == "__main__":
    unittest.main()
