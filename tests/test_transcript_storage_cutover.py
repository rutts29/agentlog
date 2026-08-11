from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agentlog.db.repository import (
    LEGACY_MATERIALIZED,
    SOURCE_BACKED,
    Repository,
    TranscriptStorageError,
)
from agentlog.db.schema import connect, init_db
from agentlog.ingest.codex import CodexAdapter
from agentlog.ingest.base import TranscriptAdapter, sqlite_fingerprint
from agentlog.ingest.pipeline import IngestStats, _ingest_one
from agentlog.normalize.models import (
    Harness,
    NormalizedMessage,
    NormalizedSession,
    ParseResult,
)


ROLLOUT = "rollout-2026-08-11T10-00-00-019fbdec-7065-7470-bb1e-dfa6c0d38237.jsonl"


def _line(role: str, text: str) -> bytes:
    payload = {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": role,
            "content": [{"type": "input_text", "text": text}],
        },
    }
    return (json.dumps(payload) + "\n").encode()


def _result(
    *texts: tuple[str, str],
    external_id: str = "storage-test",
    harness: Harness = Harness.CODEX,
) -> ParseResult:
    messages = [
        NormalizedMessage(
            seq=idx,
            role=role,
            text=text,
            content_hash=f"hash-{idx}-{text}",
        )
        for idx, (role, text) in enumerate(texts, start=1)
    ]
    return ParseResult(
        session=NormalizedSession(
            harness=harness, external_id=external_id
        ),
        messages=messages,
    )


class TranscriptStorageCutoverTests(unittest.TestCase):
    def _repo(self, root: Path) -> Repository:
        conn = connect(root / "agentlog.db")
        init_db(conn)
        self.addCleanup(conn.close)
        return Repository(conn)

    def test_source_backed_append_keeps_text_out_of_fts_and_pairs_turns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / ROLLOUT
            repo = self._repo(root)
            path.write_bytes(_line("user", "first half"))
            _ingest_one(repo, CodexAdapter(), path, IngestStats())
            repo.conn.commit()

            path.write_bytes(path.read_bytes() + _line("assistant", "second half"))
            _ingest_one(repo, CodexAdapter(), path, IngestStats())
            repo.conn.commit()

            artifact = repo.get_artifact_by_path(str(path))
            session = repo.conn.execute(
                "SELECT id, transcript_storage FROM sessions"
            ).fetchone()
            messages = repo.conn.execute(
                "SELECT role, text FROM messages ORDER BY seq"
            ).fetchall()
            fts_count = repo.conn.execute(
                "SELECT COUNT(*) AS c FROM messages_fts_idx"
            ).fetchone()["c"]
            windows = repo.conn.execute(
                "SELECT COUNT(*) AS c FROM exchange_windows"
            ).fetchone()["c"]

        self.assertIsNotNone(artifact)
        assert artifact is not None
        self.assertEqual(artifact.transcript_storage, SOURCE_BACKED)
        self.assertEqual(session["transcript_storage"], SOURCE_BACKED)
        self.assertEqual(
            [(row["role"], row["text"]) for row in messages],
            [("user", ""), ("assistant", "")],
        )
        self.assertEqual(fts_count, 0)
        self.assertEqual(windows, 1)

    def test_source_reparse_preserves_session_and_rejects_divergence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(Path(tmp))
            artifact_id = repo.upsert_artifact(
                harness="codex",
                path="/tmp/source.jsonl",
                size=1,
                mtime_ns=1,
                content_hash="source",
                parsed_offset=1,
                parser_version="test",
                transcript_storage=SOURCE_BACKED,
            )
            first = _result(("user", "request"), ("assistant", "response"))
            session_id = repo.save_parse_result(
                artifact_id=artifact_id,
                result=first,
                append=False,
                transcript_storage=SOURCE_BACKED,
            )
            repo.conn.execute(
                "INSERT INTO session_links (source_session_id, link_type, "
                "target_harness, target_external_id) VALUES (?, ?, ?, ?)",
                (session_id, "handoff", "codex", "other"),
            )
            repo.save_parse_result(
                artifact_id=artifact_id,
                result=first,
                append=False,
                transcript_storage=SOURCE_BACKED,
            )
            self.assertEqual(
                repo.conn.execute(
                    "SELECT COUNT(*) AS c FROM session_links WHERE source_session_id = ?",
                    (session_id,),
                ).fetchone()["c"],
                1,
            )
            self.assertEqual(
                repo.conn.execute(
                    "SELECT COUNT(*) AS c FROM messages_fts_idx"
                ).fetchone()["c"],
                0,
            )
            changed = _result(("user", "rewritten"), ("assistant", "response"))
            with self.assertRaisesRegex(TranscriptStorageError, "diverged"):
                repo.save_parse_result(
                    artifact_id=artifact_id,
                    result=changed,
                    append=False,
                    transcript_storage=SOURCE_BACKED,
                )

    def test_existing_legacy_session_is_not_mutated_by_ingest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / ROLLOUT
            path.write_bytes(_line("user", "old") + _line("assistant", "new"))
            repo = self._repo(root)
            artifact_id = repo.upsert_artifact(
                harness="codex",
                path=str(path),
                size=1,
                mtime_ns=1,
                content_hash="old",
                parsed_offset=1,
                parser_version="old",
                transcript_storage=LEGACY_MATERIALIZED,
            )
            legacy = _result(
                ("user", "legacy text"),
                external_id="019fbdec-7065-7470-bb1e-dfa6c0d38237",
            )
            repo.save_parse_result(
                artifact_id=artifact_id,
                result=legacy,
                append=False,
                transcript_storage=LEGACY_MATERIALIZED,
            )
            before = repo.conn.execute(
                "SELECT text FROM messages WHERE session_id = ?",
                ("codex:019fbdec-7065-7470-bb1e-dfa6c0d38237",),
            ).fetchone()["text"]

            _ingest_one(repo, CodexAdapter(), path, IngestStats())

            after = repo.conn.execute(
                "SELECT text FROM messages WHERE session_id = ?",
                ("codex:019fbdec-7065-7470-bb1e-dfa6c0d38237",),
            ).fetchone()["text"]

        self.assertEqual(before, "legacy text")
        self.assertEqual(after, "legacy text")

    def test_legacy_sqlite_artifact_can_add_a_source_backed_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "cursor.sqlite"
            with sqlite3.connect(path) as source:
                source.execute("CREATE TABLE entries (value TEXT)")
                source.execute("INSERT INTO entries VALUES ('old')")
            repo = self._repo(root)
            artifact_id = repo.upsert_artifact(
                harness="t3code",
                path=str(path),
                size=path.stat().st_size,
                mtime_ns=path.stat().st_mtime_ns,
                content_hash=sqlite_fingerprint(path),
                parsed_offset=path.stat().st_size,
                parser_version="old",
                transcript_storage=LEGACY_MATERIALIZED,
            )
            legacy = _result(
                ("user", "preserve"), external_id="old", harness=Harness.T3CODE
            )
            repo.save_parse_result(
                artifact_id=artifact_id,
                result=legacy,
                append=False,
                transcript_storage=LEGACY_MATERIALIZED,
            )

            class Adapter(TranscriptAdapter):
                harness = Harness.T3CODE
                supports_byte_append = False

                def discover(self) -> list[Path]:
                    return []

                def parse_chunk(self, path, data, *, start_offset):
                    raise NotImplementedError

                def parse_path(self, path, data, *, start_offset):
                    return [
                        _result(
                            ("user", "preserve"),
                            external_id="old",
                            harness=self.harness,
                        ),
                        _result(
                            ("user", "new text"),
                            external_id="new",
                            harness=self.harness,
                        ),
                    ]

            _ingest_one(repo, Adapter(), path, IngestStats())
            rows = repo.conn.execute(
                "SELECT external_id, transcript_storage FROM sessions ORDER BY external_id"
            ).fetchall()
            new_text = repo.conn.execute(
                "SELECT text FROM messages WHERE session_id = ?",
                ("t3code:new",),
            ).fetchone()["text"]

        self.assertEqual(
            [(row["external_id"], row["transcript_storage"]) for row in rows],
            [("new", SOURCE_BACKED), ("old", LEGACY_MATERIALIZED)],
        )
        self.assertEqual(new_text, "")
