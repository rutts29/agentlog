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

    def test_divergent_legacy_session_fails_closed_without_mutation(self) -> None:
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
            repo.conn.commit()
            before = repo.conn.execute(
                "SELECT text FROM messages WHERE session_id = ?",
                ("codex:019fbdec-7065-7470-bb1e-dfa6c0d38237",),
            ).fetchone()["text"]

            with self.assertRaisesRegex(TranscriptStorageError, "diverged"):
                _ingest_one(repo, CodexAdapter(), path, IngestStats())
            repo.conn.rollback()

            after = repo.conn.execute(
                "SELECT text FROM messages WHERE session_id = ?",
                ("codex:019fbdec-7065-7470-bb1e-dfa6c0d38237",),
            ).fetchone()["text"]

        self.assertEqual(before, "legacy text")
        self.assertEqual(after, "legacy text")

    def test_legacy_session_refreshes_identity_without_replacing_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(Path(tmp))
            artifact_id = repo.upsert_artifact(
                harness="codex",
                path="/tmp/legacy-identity.jsonl",
                size=1,
                mtime_ns=1,
                content_hash="old",
                parsed_offset=1,
                parser_version="old",
                transcript_storage=LEGACY_MATERIALIZED,
            )
            legacy = _result(
                ("user", "legacy text"),
                ("assistant", "legacy answer"),
                external_id="continued",
            )
            session_id = repo.save_parse_result(
                artifact_id=artifact_id,
                result=legacy,
                append=False,
                transcript_storage=LEGACY_MATERIALIZED,
            )
            repo.conn.execute(
                """
                INSERT INTO exchange_windows
                  (id, session_id, request_message_id, response_message_id,
                   input_hash, content_hash)
                VALUES ('legacy-window', ?, ?, ?, 'legacy-input', 'legacy-window')
                """,
                (
                    session_id,
                    f"{session_id}:m:1",
                    f"{session_id}:m:2",
                ),
            )
            repo.conn.execute(
                """
                INSERT INTO tool_events
                  (id, session_id, message_id, seq, tool_name, action)
                VALUES ('legacy-tool', ?, ?, 1, 'exec_command', 'call')
                """,
                (session_id, f"{session_id}:m:2"),
            )
            refreshed = _result(("user", "new source text"), external_id="continued")
            refreshed.session.originator = "t3code_desktop"
            refreshed.session.thread_source = "subagent"
            refreshed.extras.update(
                {
                    "inherited_message_count": 4,
                    "inherited_record_count": 7,
                    "fork_context_status": "trimmed",
                }
            )

            repo.save_parse_result(
                artifact_id=artifact_id,
                result=refreshed,
                append=False,
                transcript_storage=LEGACY_MATERIALIZED,
                preserve_existing_legacy=True,
            )

            row = repo.conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            assert row is not None
            text = repo.conn.execute(
                "SELECT text FROM messages WHERE session_id = ?", (session_id,)
            ).fetchone()["text"]
            self.assertEqual(text, "legacy text")
            self.assertEqual(row["artifact_id"], artifact_id)
            self.assertEqual(row["transcript_storage"], LEGACY_MATERIALIZED)
            self.assertEqual(row["originator"], "t3code_desktop")
            self.assertEqual(row["thread_source"], "subagent")
            self.assertEqual(row["inherited_message_count"], 4)
            self.assertEqual(
                repo.conn.execute(
                    "SELECT id FROM exchange_windows WHERE session_id = ?",
                    (session_id,),
                ).fetchone()["id"],
                "legacy-window",
            )
            self.assertEqual(
                repo.conn.execute(
                    "SELECT id FROM tool_events WHERE session_id = ?",
                    (session_id,),
                ).fetchone()["id"],
                "legacy-tool",
            )

    def test_legacy_session_refreshes_present_and_later_provider_links(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(Path(tmp))
            t3_artifact = repo.upsert_artifact(
                harness="t3code",
                path="/tmp/legacy-t3.sqlite",
                size=1,
                mtime_ns=1,
                content_hash="old-t3",
                parsed_offset=1,
                parser_version="old",
                transcript_storage=LEGACY_MATERIALIZED,
            )
            legacy = _result(
                ("user", "legacy root text"),
                external_id="root",
                harness=Harness.T3CODE,
            )
            root_id = repo.save_parse_result(
                artifact_id=t3_artifact,
                result=legacy,
                append=False,
                transcript_storage=LEGACY_MATERIALIZED,
            )
            repo.conn.execute(
                "INSERT INTO sessions (id, harness, external_id) VALUES (?, ?, ?)",
                ("codex:present", "codex", "present"),
            )
            refreshed = _result(
                ("user", "new root text"),
                external_id="root",
                harness=Harness.T3CODE,
            )
            refreshed.extras["session_links"] = [
                {
                    "link_type": "provider_backing",
                    "target_harness": "codex",
                    "target_external_id": "present",
                    "link_role": "root",
                },
                {
                    "link_type": "provider_backing",
                    "target_harness": "codex",
                    "target_external_id": "later",
                    "link_role": "worker",
                },
            ]

            repo.save_parse_result(
                artifact_id=t3_artifact,
                result=refreshed,
                append=False,
                transcript_storage=LEGACY_MATERIALIZED,
                preserve_existing_legacy=True,
            )

            links = {
                row["target_external_id"]: row["target_session_id"]
                for row in repo.conn.execute(
                    "SELECT target_external_id, target_session_id "
                    "FROM session_links WHERE source_session_id = ?",
                    (root_id,),
                )
            }
            self.assertEqual(links["present"], "codex:present")
            self.assertIsNone(links["later"])
            codex_artifact = repo.upsert_artifact(
                harness="codex",
                path="/tmp/later.jsonl",
                size=1,
                mtime_ns=1,
                content_hash="later",
                parsed_offset=1,
                parser_version="28",
            )
            repo.save_parse_result(
                artifact_id=codex_artifact,
                result=_result(("assistant", "done"), external_id="later"),
                append=False,
            )
            resolved = repo.conn.execute(
                "SELECT target_session_id FROM session_links "
                "WHERE source_session_id = ? AND target_external_id = 'later'",
                (root_id,),
            ).fetchone()
            self.assertEqual(resolved["target_session_id"], "codex:later")
            text = repo.conn.execute(
                "SELECT text FROM messages WHERE session_id = ?", (root_id,)
            ).fetchone()["text"]
            self.assertEqual(text, "legacy root text")

    def test_alternate_artifact_cannot_refresh_legacy_identity_or_links(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(Path(tmp))
            canonical_artifact = repo.upsert_artifact(
                harness="codex",
                path="/tmp/canonical.jsonl",
                size=1,
                mtime_ns=1,
                content_hash="canonical",
                parsed_offset=1,
                parser_version="old",
                transcript_storage=LEGACY_MATERIALIZED,
            )
            alternate_artifact = repo.upsert_artifact(
                harness="codex",
                path="/tmp/alternate.jsonl",
                size=1,
                mtime_ns=1,
                content_hash="alternate",
                parsed_offset=1,
                parser_version="old",
                transcript_storage=LEGACY_MATERIALIZED,
            )
            session_id = repo.save_parse_result(
                artifact_id=canonical_artifact,
                result=_result(("user", "canonical"), external_id="same-id"),
                append=False,
                transcript_storage=LEGACY_MATERIALIZED,
            )
            injected = _result(("user", "alternate"), external_id="same-id")
            injected.session.originator = "t3code_desktop"
            injected.session.parent_session_id = "attacker-parent"
            injected.extras["session_links"] = [
                {
                    "link_type": "provider_backing",
                    "target_harness": "codex",
                    "target_external_id": "attacker",
                }
            ]

            repo.save_parse_result(
                artifact_id=alternate_artifact,
                result=injected,
                append=False,
                transcript_storage=LEGACY_MATERIALIZED,
                preserve_existing_legacy=True,
            )

            row = repo.conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            assert row is not None
            self.assertEqual(row["artifact_id"], canonical_artifact)
            self.assertIsNone(row["originator"])
            self.assertIsNone(row["parent_session_id"])
            self.assertEqual(
                repo.conn.execute(
                    "SELECT COUNT(*) AS c FROM session_links "
                    "WHERE source_session_id = ?",
                    (session_id,),
                ).fetchone()["c"],
                0,
            )
            self.assertEqual(
                repo.conn.execute(
                    "SELECT text FROM messages WHERE session_id = ?",
                    (session_id,),
                ).fetchone()["text"],
                "canonical",
            )

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
