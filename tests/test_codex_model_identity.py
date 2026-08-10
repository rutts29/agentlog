from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agentlog.ingest.codex import CodexAdapter
from agentlog.normalize.models import Harness
from agentlog.normalize.model_identity import resolve_model_identity


def _line(kind: str, payload: dict, ts: str = "2026-06-20T12:00:00Z") -> bytes:
    return (json.dumps({"type": kind, "timestamp": ts, "payload": payload}) + "\n").encode()


class CodexModelIdentityIngestTests(unittest.TestCase):
    def test_t3code_originator_keeps_harness_separate_from_execution_identity(self) -> None:
        data = b"".join(
            [
                _line(
                    "session_meta",
                    {
                        "id": "t3-root",
                        "originator": "t3code_desktop",
                        "model_provider": "openai",
                    },
                ),
                _line(
                    "turn_context",
                    {"model": "gpt-5.6-terra", "effort": "high"},
                    ts="2026-06-20T12:00:01Z",
                ),
                _line(
                    "event_msg",
                    {"type": "user_message", "message": "root request"},
                    ts="2026-06-20T12:00:01Z",
                ),
                _line(
                    "event_msg",
                    {"type": "agent_message", "message": "worker done"},
                    ts="2026-06-20T12:00:02Z",
                ),
            ]
        )
        result = CodexAdapter().parse_chunk(
            Path("/tmp/t3code-root.jsonl"), data, start_offset=0
        )
        self.assertEqual(result.session.harness, Harness.CODEX)
        self.assertEqual(result.session.provider, "openai")
        self.assertIsNone(result.session.agent_profile)
        self.assertEqual(result.session.model, "gpt-5.6-terra")
        self.assertEqual(result.extras["originator"], "t3code_desktop")
        self.assertFalse(result.messages[0].authored_by_agent)

    def test_t3code_worker_brief_is_agent_authored_without_role_metadata(self) -> None:
        data = b"".join(
            [
                _line(
                    "session_meta",
                    {
                        "id": "worker-1",
                        "originator": "t3code_desktop",
                        "thread_source": "subagent",
                        "source": {
                            "subagent": {
                                "thread_spawn": {
                                    "agent_nickname": "Bacon",
                                }
                            }
                        },
                    },
                ),
                _line(
                    "event_msg",
                    {"type": "user_message", "message": "inspect this slice"},
                    ts="2026-06-20T12:00:01Z",
                ),
            ]
        )
        result = CodexAdapter().parse_chunk(
            Path("/tmp/t3code-worker.jsonl"), data, start_offset=0
        )
        self.assertTrue(result.messages[0].authored_by_agent)
        self.assertEqual(result.session.agent_profile, "Bacon")

    def test_model_provider_not_used_as_model(self) -> None:
        data = b"".join(
            [
                _line(
                    "session_meta",
                    {
                        "id": "019ee519-9204-7bc2-9d72-7b2a73d8d6d7",
                        "model_provider": "openai",
                        "agent_role": "explorer",
                        "thread_source": "subagent",
                        "source": {
                            "subagent": {
                                "thread_spawn": {
                                    "agent_role": "explorer",
                                }
                            }
                        },
                    },
                ),
                _line(
                    "event_msg",
                    {"type": "user_message", "message": "hello"},
                    ts="2026-06-20T12:00:01Z",
                ),
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout.jsonl"
            path.write_bytes(data)
            result = CodexAdapter().parse_chunk(path, data, start_offset=0)
        self.assertIsNone(result.session.model)
        self.assertEqual(result.session.provider, "openai")
        self.assertEqual(result.session.agent_profile, "explorer")
        ident = resolve_model_identity(
            result.session.model,
            provider_hint=result.session.provider,
            agent_profile_hint=result.session.agent_profile,
        )
        self.assertIsNone(ident.canonical)
        self.assertEqual(ident.provider, "openai")

    def test_codex_auto_review_kept_as_raw_model(self) -> None:
        data = b"".join(
            [
                _line(
                    "session_meta",
                    {
                        "id": "019ee45a-b0b6-7b61-8151-bb0b3b3a2421",
                        "model_provider": "openai",
                        "thread_source": "subagent",
                        "source": {"subagent": {"other": "guardian"}},
                    },
                ),
                _line(
                    "turn_context",
                    {"model": "codex-auto-review", "effort": "low"},
                ),
                _line(
                    "event_msg",
                    {"type": "agent_message", "message": "review done"},
                    ts="2026-06-20T12:00:02Z",
                ),
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout.jsonl"
            path.write_bytes(data)
            result = CodexAdapter().parse_chunk(path, data, start_offset=0)
        self.assertEqual(result.session.model, "codex-auto-review")
        self.assertEqual(result.session.provider, "openai")
        self.assertEqual(result.session.agent_profile, "guardian")
        ident = resolve_model_identity(
            result.session.model,
            provider_hint=result.session.provider,
            agent_profile_hint=result.session.agent_profile,
        )
        self.assertIsNone(ident.canonical)
        self.assertEqual(ident.agent_profile, "codex-auto-review")


if __name__ == "__main__":
    unittest.main()
