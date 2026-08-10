from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agentlog.db.repository import Repository
from agentlog.db.schema import connect, init_db
from agentlog.ingest.base import iter_jsonl_bytes
from agentlog.ingest.codex import CodexAdapter
from agentlog.ingest.pipeline import IngestStats, _ingest_one

ROLLOUT = "rollout-2026-08-09T10-00-00-019fbdec-7065-7470-bb1e-dfa6c0d38237.jsonl"


def _msg_line(role: str, text: str) -> bytes:
    obj = {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": role,
            "content": [
                {
                    "type": "input_text" if role == "user" else "output_text",
                    "text": text,
                }
            ],
        },
    }
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")


def _is_complete_json(chunk: bytes) -> bool:
    try:
        json.loads(chunk.decode("utf-8", errors="replace"))
    except ValueError:
        return False
    return True


class PartialJsonlFramingTests(unittest.TestCase):
    def test_unterminated_bad_line_reports_its_own_start(self) -> None:
        data = b'{"a": 1}\n{"type":'
        events = list(iter_jsonl_bytes(data, source="t"))
        self.assertEqual(events[0][:3], (0, 9, {"a": 1}))
        start, safe, obj, err = events[1]
        self.assertEqual(start, 9)
        self.assertEqual(safe, 9)
        self.assertIsNone(obj)
        self.assertIn("incomplete trailing line", err or "")

    def test_malformed_complete_line_advances(self) -> None:
        data = b'{"type":\n{"a": 1}\n'
        events = list(iter_jsonl_bytes(data, source="t"))
        start, safe, obj, err = events[0]
        self.assertEqual((start, safe), (0, 9))
        self.assertIsNone(obj)
        self.assertNotIn("incomplete", err or "")
        self.assertEqual(events[1][2], {"a": 1})

    def test_split_multibyte_tail_is_not_consumed(self) -> None:
        record = json.dumps({"text": "日本語"}, ensure_ascii=False).encode("utf-8")
        for cut in range(1, len(record)):
            with self.subTest(cut=cut):
                events = list(iter_jsonl_bytes(record[:cut], source="t"))
                if not events:
                    continue
                _start, safe, obj, _err = events[-1]
                if obj is None:
                    self.assertEqual(safe, 0)

    def test_unterminated_valid_line_is_still_parsed(self) -> None:
        data = b'{"a": 1}'
        events = list(iter_jsonl_bytes(data, source="t"))
        self.assertEqual(events, [(0, 8, {"a": 1}, None)])


class PartialTailIngestTests(unittest.TestCase):
    """Append ingest must recover a record truncated at any byte position."""

    def _ingest(self, repo: Repository, path: Path) -> IngestStats:
        stats = IngestStats()
        _ingest_one(repo, CodexAdapter(), path, stats)
        repo.conn.commit()
        return stats

    def test_recovers_partial_tail_at_every_byte_position(self) -> None:
        first = _msg_line("user", "please summarise")
        second = _msg_line("assistant", "réponse: 日本語 ✅ done")
        session_id = "codex:019fbdec-7065-7470-bb1e-dfa6c0d38237"

        for cut in range(1, len(second)):
            with self.subTest(cut=cut):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    db = root / "a.db"
                    path = root / ROLLOUT
                    path.write_bytes(first + second[:cut])
                    conn = connect(db)
                    init_db(conn)
                    repo = Repository(conn)

                    self._ingest(repo, path)
                    art = repo.get_artifact_by_path(str(path))
                    assert art is not None
                    if not _is_complete_json(second[:cut]):
                        self.assertLessEqual(art.parsed_offset, len(first))

                    path.write_bytes(first + second)
                    self._ingest(repo, path)

                    rows = conn.execute(
                        "SELECT role, text FROM messages "
                        "WHERE session_id = ? ORDER BY seq",
                        (session_id,),
                    ).fetchall()
                    conn.close()
                    self.assertEqual(
                        [(r["role"], r["text"]) for r in rows],
                        [
                            ("user", "please summarise"),
                            ("assistant", "réponse: 日本語 ✅ done"),
                        ],
                    )

    def test_complete_file_without_trailing_newline_is_ingested(self) -> None:
        first = _msg_line("user", "hello")
        second = _msg_line("assistant", "hi").rstrip(b"\n")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / ROLLOUT
            path.write_bytes(first + second)
            conn = connect(root / "a.db")
            init_db(conn)
            repo = Repository(conn)
            self._ingest(repo, path)
            n = conn.execute("SELECT COUNT(*) AS c FROM messages").fetchone()["c"]
            art = repo.get_artifact_by_path(str(path))
            conn.close()
        self.assertEqual(n, 2)
        assert art is not None
        self.assertEqual(art.parsed_offset, len(first) + len(second))

    def test_malformed_complete_line_does_not_stall_ingest(self) -> None:
        first = _msg_line("user", "hello")
        broken = b'{"type": "response_item", "payload": {\n'
        third = _msg_line("assistant", "recovered")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / ROLLOUT
            path.write_bytes(first + broken + third)
            conn = connect(root / "a.db")
            init_db(conn)
            repo = Repository(conn)
            self._ingest(repo, path)
            art = repo.get_artifact_by_path(str(path))
            n = conn.execute("SELECT COUNT(*) AS c FROM messages").fetchone()["c"]
            conn.close()
        self.assertEqual(n, 2)
        assert art is not None
        self.assertEqual(art.parsed_offset, len(first) + len(broken) + len(third))


if __name__ == "__main__":
    unittest.main()
