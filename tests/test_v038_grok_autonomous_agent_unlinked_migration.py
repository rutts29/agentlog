from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agentlog.db.migrations.v038_grok_autonomous_agent_unlinked import apply
from agentlog.db.schema import connect, init_db
from agentlog.ingest.grok import GrokAdapter
from agentlog.session_identity import GROK_AUTONOMOUS_AGENT_UNLINKED_THREAD_SOURCE


def _line(value: dict[str, object]) -> bytes:
    return json.dumps(value).encode() + b"\n"


def _data() -> bytes:
    return b"".join(
        [
            _line({
                "type": "system",
                "content": "You are Grok 4.6 released by xAI. You are an autonomous agent that completes software engineering tasks. There is no human operator in this session.",
            }),
            _line({"type": "user", "content": "<user_info>workspace</user_info>"}),
            _line({"type": "user", "synthetic_reason": "system_reminder", "content": "<system-reminder>skills"}),
            _line({"type": "user", "content": "<user_query>inspect this workflow</user_query>"}),
            _line({"type": "assistant", "content": "validated"}),
        ]
    )


class GrokAutonomousMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "sessions"
        self.path = self.root / "%2Ftmp%2Fagentlog" / "run" / "chat_history.jsonl"
        self.path.parent.mkdir(parents=True)
        self.data = _data()
        self.path.write_bytes(self.data)
        (self.path.parent / "summary.json").write_text(
            json.dumps({"agent_name": "grok-build-plan"})
        )
        self.conn = connect(Path(self.tmp.name) / "agentlog.db")
        init_db(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def _seed(self, session_id: str = "grok:run", *, mismatch: bool = False) -> None:
        adapter = GrokAdapter()
        snapshot = adapter.capture_source(self.path)
        artifact = self.conn.execute(
            """
            INSERT INTO artifacts(harness,path,size,mtime_ns,content_hash,parsed_offset,parser_version,transcript_storage)
            VALUES('grok',?,?,?,?,?,?, 'source_backed')
            """,
            (str(self.path), snapshot.revision[0], snapshot.revision[1], snapshot.content_hash, len(self.data), "16"),
        )
        result = adapter.parse_source_snapshot(self.path, snapshot)[0]
        self.conn.execute(
            """
            INSERT INTO sessions(id,harness,external_id,artifact_id,agent_profile,transcript_storage)
            VALUES(?,?,?,?,?,'source_backed')
            """,
            (session_id, "grok", "run", artifact.lastrowid, "grok-build-plan"),
        )
        rows = [
            (f"{session_id}:m:{message.seq}", session_id, message.seq, message.role, message.content_hash)
            for message in result.messages
        ]
        if mismatch:
            rows[-1] = (*rows[-1][:4], "changed")
        self.conn.executemany(
            "INSERT INTO messages(id,session_id,seq,role,content_hash,authored_by_agent) VALUES(?,?,?,?,?,?)",
            [(*row, int(message.authored_by_agent) if message.role != "user" or message.seq != 4 else 0)
             for row, message in zip(rows, result.messages)],
        )
        self.conn.commit()

    def test_updates_only_source_verified_matching_rows(self) -> None:
        self._seed()
        apply(self.conn)
        apply(self.conn)
        session = self.conn.execute("SELECT thread_source FROM sessions WHERE id='grok:run'").fetchone()
        query = self.conn.execute("SELECT authored_by_agent FROM messages WHERE session_id='grok:run' AND seq=4").fetchone()
        self.assertEqual(session["thread_source"], GROK_AUTONOMOUS_AGENT_UNLINKED_THREAD_SOURCE)
        self.assertEqual(query["authored_by_agent"], 1)

    def test_leaves_hash_mismatch_unchanged(self) -> None:
        self._seed(mismatch=True)
        apply(self.conn)
        self.assertIsNone(self.conn.execute("SELECT thread_source FROM sessions WHERE id='grok:run'").fetchone()["thread_source"])
        self.assertEqual(self.conn.execute("SELECT authored_by_agent FROM messages WHERE session_id='grok:run' AND seq=4").fetchone()["authored_by_agent"], 0)

    def test_skips_source_changed_during_parse(self) -> None:
        self._seed()
        original = GrokAdapter.parse_source_snapshot

        def mutate(adapter: GrokAdapter, path: Path, snapshot):
            path.write_bytes(self.data + b"\n")
            return original(adapter, path, snapshot)

        with mock.patch.object(GrokAdapter, "parse_source_snapshot", mutate):
            apply(self.conn)
        self.assertIsNone(self.conn.execute("SELECT thread_source FROM sessions WHERE id='grok:run'").fetchone()["thread_source"])


if __name__ == "__main__":
    unittest.main()
