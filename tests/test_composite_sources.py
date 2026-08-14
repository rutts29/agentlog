from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentlog import source_reader
from agentlog.db.repository import Repository
from agentlog.db.schema import connect, init_db
from agentlog.ingest.base import SourceSnapshot, TranscriptAdapter, content_hash_text, hash_bytes
from agentlog.ingest.pipeline import IngestStats, _ingest_one
from agentlog.normalize.models import Harness, NormalizedMessage, NormalizedSession, ParseResult
from agentlog.source_reader import CachedSourceTranscriptReader, read_source_transcript


class _CompositeAdapter(TranscriptAdapter):
    harness = Harness.GROK
    supports_byte_append = False
    uses_composite_source = True
    mutate_once = False

    def discover(self) -> list[Path]:
        return []

    def parse_chunk(self, path: Path, data: bytes, *, start_offset: int) -> ParseResult:
        raise NotImplementedError

    @staticmethod
    def _dependency(path: Path) -> Path:
        return path.with_suffix(".summary")

    def capture_source(self, path: Path) -> SourceSnapshot:
        dependency = self._dependency(path)
        before = tuple(item.stat() for item in (path, dependency))
        data = path.read_bytes() + b"\0" + dependency.read_bytes()
        after = tuple(item.stat() for item in (path, dependency))
        if any(
            left.st_size != right.st_size or left.st_mtime_ns != right.st_mtime_ns
            for left, right in zip(before, after)
        ):
            raise OSError("composite source changed while being captured")
        content_hash = hash_bytes(data)
        revision = (len(data), int(content_hash[:15], 16))
        return SourceSnapshot(data, revision, content_hash)

    def parse_source_snapshot(self, path: Path, snapshot: SourceSnapshot) -> list[ParseResult]:
        primary, _dependency = snapshot.data.split(b"\0", 1)
        if type(self).mutate_once:
            type(self).mutate_once = False
            self._dependency(path).write_bytes(b"changed during parse")
        messages = [
            NormalizedMessage(
                seq=1,
                role="user",
                text=primary.decode(),
                content_hash=content_hash_text(primary.decode()),
            ),
            NormalizedMessage(
                seq=2,
                role="assistant",
                text="reply",
                content_hash=content_hash_text("reply"),
            ),
        ]
        return [
            ParseResult(
                session=NormalizedSession(harness=Harness.GROK, external_id="root"),
                messages=messages,
                bytes_consumed=snapshot.revision[0],
            )
        ]


class CompositeSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.path = root / "chat_history.jsonl"
        self.path.write_bytes(b"request")
        self.path.with_suffix(".summary").write_bytes(b"initial summary")
        self.conn = connect(root / "agentlog.db")
        init_db(self.conn)
        self.repo = Repository(self.conn)
        self.adapter_patch = patch.dict(
            source_reader._ADAPTERS, {"grok": _CompositeAdapter}
        )
        self.adapter_patch.start()

    def tearDown(self) -> None:
        self.adapter_patch.stop()
        self.conn.close()
        self.tmp.cleanup()

    def _ingest(self) -> None:
        _ingest_one(self.repo, _CompositeAdapter(), self.path, IngestStats())

    def test_dependency_change_forces_reparse_and_never_uses_append(self) -> None:
        self._ingest()
        original = self.conn.execute("SELECT id FROM artifacts").fetchone()["id"]
        dependency = self.path.with_suffix(".summary")
        original_stat = dependency.stat()
        dependency.write_bytes(b"other summary")
        os.utime(dependency, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

        stats = IngestStats()
        _ingest_one(self.repo, _CompositeAdapter(), self.path, stats)

        self.assertEqual(stats.parsed, 1)
        self.assertEqual(stats.appended, 0)
        self.assertEqual(
            self.conn.execute("SELECT id FROM artifacts").fetchone()["id"], original
        )
        result = read_source_transcript(self.conn, "grok:root")
        self.assertTrue(result.ready)
        self.assertEqual(result.messages[-1]["text"], "reply")

    def test_change_during_parse_retries_captured_snapshot(self) -> None:
        _CompositeAdapter.mutate_once = True
        self._ingest()

        result = read_source_transcript(self.conn, "grok:root")
        self.assertTrue(result.ready)
        self.assertEqual(result.messages[-1]["text"], "reply")

    def test_rewrite_or_missing_dependency_fails_closed_in_source_reader(self) -> None:
        self._ingest()
        dependency = self.path.with_suffix(".summary")
        dependency.write_bytes(b"rewritten")

        changed = read_source_transcript(self.conn, "grok:root")
        self.assertEqual(changed.status, "source_changed")

        dependency.unlink()
        missing = read_source_transcript(self.conn, "grok:root")
        self.assertEqual(missing.status, "source_changed")

    def test_reader_parses_only_the_stable_capture(self) -> None:
        self._ingest()
        result = read_source_transcript(self.conn, "grok:root")
        self.assertTrue(result.ready)
        self.assertEqual([item["text"] for item in result.messages], ["request", "reply"])

    def test_operation_verification_uses_the_whole_composite_source(self) -> None:
        self._ingest()
        reader = CachedSourceTranscriptReader()
        try:
            self.assertTrue(reader(self.conn, "grok:root").ready)
            self.assertTrue(reader.verify_current())
            self.path.with_suffix(".summary").write_bytes(b"later summary")
            self.assertFalse(reader.verify_current())
        finally:
            reader.close()
