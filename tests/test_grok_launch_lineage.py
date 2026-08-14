from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agentlog.db.repository import Repository, SOURCE_BACKED
from agentlog.db.schema import connect, init_db
from agentlog.db.migrations.v039_grok_launch_observations import apply as apply_v039
from agentlog.ingest.codex import CodexAdapter
from agentlog.ingest.base import content_hash_text, hash_bytes
from agentlog.normalize.models import Harness, NormalizedMessage, NormalizedSession, ParseResult
from agentlog.session_identity import GROK_AUTONOMOUS_AGENT_UNLINKED_THREAD_SOURCE


def _line(timestamp: datetime, payload: dict) -> bytes:
    return (json.dumps({"timestamp": timestamp.isoformat(), "type": "response_item", "payload": payload}) + "\n").encode()


def _launch(call_id: str, prompt: str) -> dict:
    command = f'grok --cwd /repo --model grok-4.6 --single {json.dumps(prompt)}'
    return {"type": "custom_tool_call", "call_id": call_id, "name": "exec", "input": f'const r = await tools.exec_command({{cmd:{json.dumps(command)}}});'}


def _running(call_id: str, cell: str) -> dict:
    return {"type": "custom_tool_call_output", "call_id": call_id, "output": f"Script running with cell ID {cell}\n"}


def _wait(call_id: str, cell: str) -> dict:
    return {"type": "function_call", "call_id": call_id, "name": "wait", "arguments": json.dumps({"cell_id": cell})}


def _done(call_id: str, exit_code: int) -> dict:
    return {"type": "function_call_output", "call_id": call_id, "output": [{"type": "input_text", "text": "Script completed\n"}, {"type": "input_text", "text": json.dumps({"exit_code": exit_code})}]}


class GrokLaunchLineageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self.tmp.name) / "agentlog.db")
        init_db(self.conn)
        self.repo = Repository(self.conn)
        self.base = datetime(2026, 8, 14, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def _artifact(self, harness: str, name: str) -> int:
        return self.repo.upsert_artifact(
            harness=harness, path=name, size=1, mtime_ns=1, content_hash=name,
            parsed_offset=1, parser_version="test", transcript_storage=SOURCE_BACKED,
        )

    def _target(self, external_id: str, prompt: str, started: datetime) -> ParseResult:
        return ParseResult(
            session=NormalizedSession(
                harness=Harness.GROK, external_id=external_id,
                thread_source=GROK_AUTONOMOUS_AGENT_UNLINKED_THREAD_SOURCE,
                cwd="/repo", model="grok-4.6-build", started_at=started,
            ),
            messages=[NormalizedMessage(seq=1, role="user", timestamp=started, text=prompt, content_hash=content_hash_text(prompt), authored_by_agent=True)],
            bytes_consumed=1,
        )

    def _caller_data(self, count: int = 3, *, caller_id: str = "caller", offset: int = 0) -> bytes:
        prompt = "validate workflow"
        base = self.base + timedelta(seconds=offset)
        records = [(json.dumps({"timestamp": base.isoformat(), "type": "session_meta", "payload": {"id": caller_id, "cwd": "/repo"}}) + "\n").encode()]
        # One denied sandbox attempt must not become an observation.
        records += [_line(base + timedelta(seconds=1), _launch("failed", prompt)), _line(base + timedelta(seconds=2), _done("failed", 1))]
        for index in range(count):
            at = base + timedelta(seconds=10 + index * 10)
            records += [_line(at, _launch(f"launch-{index}", prompt)), _line(at + timedelta(seconds=1), _running(f"launch-{index}", str(index))), _line(at + timedelta(seconds=2), _wait(f"wait-{index}", str(index))), _line(at + timedelta(seconds=3), _done(f"wait-{index}", 0))]
        return b"".join(records)

    def _caller(self, count: int = 3, *, caller_id: str = "caller", offset: int = 0) -> ParseResult:
        result = CodexAdapter().parse_chunk(Path(f"rollout-{caller_id}.jsonl"), self._caller_data(count, caller_id=caller_id, offset=offset), start_offset=0)
        self.assertEqual(len(result.extras.get("grok_launches", [])), count)
        return result

    def test_repeated_prompts_link_in_order_and_rewrite_clears_stale_links(self) -> None:
        caller = self._caller()
        caller_artifact = self._artifact("codex", "caller")
        target_artifact = self._artifact("grok", "target")
        for index in range(3):
            self.repo.save_parse_result(artifact_id=target_artifact, result=self._target(f"target-{index}", "validate workflow", self.base + timedelta(seconds=12 + index * 10)), append=False, transcript_storage=SOURCE_BACKED)
        self.repo.save_parse_result(artifact_id=caller_artifact, result=caller, append=False, transcript_storage=SOURCE_BACKED)
        links = self.conn.execute("SELECT target_external_id FROM session_links WHERE link_type = 'agent_launch' ORDER BY target_external_id").fetchall()
        self.assertEqual([row["target_external_id"] for row in links], ["target-0", "target-1", "target-2"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM grok_launch_observations WHERE call_id = 'failed'").fetchone()[0], 0)
        caller.extras["grok_launches"] = []
        self.repo.save_parse_result(artifact_id=caller_artifact, result=caller, append=False, transcript_storage=SOURCE_BACKED)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM session_links WHERE link_type = 'agent_launch'").fetchone()[0], 0)

    def test_ambiguous_equal_timestamps_stay_unlinked_when_child_ingests_after_caller(self) -> None:
        caller = self._caller()
        caller_artifact = self._artifact("codex", "caller")
        target_artifact = self._artifact("grok", "target")
        self.repo.save_parse_result(artifact_id=caller_artifact, result=caller, append=False, transcript_storage=SOURCE_BACKED)
        for index in range(3):
            self.repo.save_parse_result(artifact_id=target_artifact, result=self._target(f"target-{index}", "validate workflow", self.base + timedelta(seconds=20)), append=False, transcript_storage=SOURCE_BACKED)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM session_links WHERE link_type = 'agent_launch'").fetchone()[0], 0)

    def test_unique_child_links_when_caller_ingests_first(self) -> None:
        caller = self._caller(count=1)
        caller_artifact = self._artifact("codex", "caller")
        target_artifact = self._artifact("grok", "target")
        self.repo.save_parse_result(artifact_id=caller_artifact, result=caller, append=False, transcript_storage=SOURCE_BACKED)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM session_links WHERE link_type = 'agent_launch'").fetchone()[0], 0)
        self.repo.save_parse_result(artifact_id=target_artifact, result=self._target("target", "validate workflow", self.base + timedelta(seconds=12)), append=False, transcript_storage=SOURCE_BACKED)
        link = self.conn.execute("SELECT target_external_id, confidence FROM session_links WHERE link_type = 'agent_launch'").fetchone()
        self.assertEqual(tuple(link), ("target", "high"))

    def test_repeated_prompt_calls_from_different_callers_stay_unlinked(self) -> None:
        first = self._caller(count=1, caller_id="first", offset=0)
        second = self._caller(count=1, caller_id="second", offset=10)
        target_artifact = self._artifact("grok", "target")
        self.repo.save_parse_result(artifact_id=self._artifact("codex", "first"), result=first, append=False, transcript_storage=SOURCE_BACKED)
        self.repo.save_parse_result(artifact_id=self._artifact("codex", "second"), result=second, append=False, transcript_storage=SOURCE_BACKED)
        self.repo.save_parse_result(artifact_id=target_artifact, result=self._target("first-target", "validate workflow", self.base + timedelta(seconds=21)), append=False, transcript_storage=SOURCE_BACKED)
        self.repo.save_parse_result(artifact_id=target_artifact, result=self._target("second-target", "validate workflow", self.base + timedelta(seconds=22)), append=False, transcript_storage=SOURCE_BACKED)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM session_links WHERE link_type = 'agent_launch'").fetchone()[0], 0)

    def test_v039_backfills_only_source_verified_matching_caller(self) -> None:
        data = self._caller_data()
        path = Path(self.tmp.name) / "caller.jsonl"
        path.write_bytes(data)
        stat = path.stat()
        caller = CodexAdapter().parse_chunk(path, data, start_offset=0)
        caller_artifact = self.repo.upsert_artifact(
            harness="codex", path=str(path), size=stat.st_size, mtime_ns=stat.st_mtime_ns,
            content_hash=hash_bytes(data), parsed_offset=len(data), parser_version="test",
            transcript_storage=SOURCE_BACKED,
        )
        target_artifact = self._artifact("grok", "target")
        for index in range(3):
            self.repo.save_parse_result(artifact_id=target_artifact, result=self._target(f"target-{index}", "validate workflow", self.base + timedelta(seconds=12 + index * 10)), append=False, transcript_storage=SOURCE_BACKED)
        self.repo.save_parse_result(artifact_id=caller_artifact, result=caller, append=False, transcript_storage=SOURCE_BACKED)
        self.conn.execute("DELETE FROM grok_launch_observations")
        self.conn.execute("DELETE FROM session_links WHERE link_type = 'agent_launch'")
        apply_v039(self.conn)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM grok_launch_observations").fetchone()[0], 3)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM session_links WHERE link_type = 'agent_launch'").fetchone()[0], 3)
