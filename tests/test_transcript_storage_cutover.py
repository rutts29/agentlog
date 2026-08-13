from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agentlog.db.repository import (
    LEGACY_MATERIALIZED,
    SOURCE_BACKED,
    Repository,
    TranscriptStorageError,
)
from agentlog.db.schema import connect, init_db
from agentlog.ingest.codex import CodexAdapter
from agentlog.ingest.base import TranscriptAdapter, sqlite_fingerprint
from agentlog.ingest.pipeline import (
    IngestStats,
    _ingest_one,
    ingest_all,
    ingest_harness,
)
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
            # Simulate a source-backed row created before the v030 tail-fact
            # migration. The next ordinary append must backfill its facts.
            repo.conn.execute(
                """
                UPDATE sessions SET
                    attention_final_question = NULL,
                    attention_tail_revision = NULL
                """
            )
            repo.conn.commit()

            path.write_bytes(
                path.read_bytes() + _line("assistant", "Should I continue?")
            )
            with mock.patch("agentlog.ingest.codex.CODEX_SESSIONS_DIR", root):
                stats = ingest_harness(repo, "codex", changed_paths=[path])
            repo.conn.commit()

            artifact = repo.get_artifact_by_path(str(path))
            session = repo.conn.execute(
                """
                SELECT id, transcript_storage, attention_final_question,
                       attention_tail_revision
                FROM sessions
                """
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
        self.assertEqual(session["attention_final_question"], 1)
        self.assertEqual(session["attention_tail_revision"], 1)
        self.assertEqual(stats.appended, 1)
        self.assertEqual(
            [(row["role"], row["text"]) for row in messages],
            [("user", ""), ("assistant", "")],
        )
        self.assertEqual(fts_count, 0)
        self.assertEqual(windows, 1)

    def test_source_backed_ingest_persists_non_text_attention_tail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._repo(root)
            artifact = repo.upsert_artifact(
                harness="codex",
                path=str(root / "tail.jsonl"),
                size=1,
                mtime_ns=1,
                content_hash="tail",
                parsed_offset=1,
                parser_version="test",
                transcript_storage=SOURCE_BACKED,
            )
            repo.save_parse_result(
                artifact_id=artifact,
                result=_result(
                    ("user", "Please inspect this."),
                    ("assistant", "Should I continue?"),
                    external_id="attention-tail",
                ),
                append=False,
                transcript_storage=SOURCE_BACKED,
            )
            row = repo.conn.execute(
                """
                SELECT attention_last_substantive_role,
                       attention_final_question,
                       attention_incomplete_todo,
                       attention_tail_revision
                FROM sessions WHERE id = 'codex:attention-tail'
                """
            ).fetchone()
            stored_text = repo.conn.execute(
                "SELECT text FROM messages WHERE session_id = 'codex:attention-tail'"
            ).fetchall()

        assert row is not None
        self.assertEqual(row["attention_last_substantive_role"], "assistant")
        self.assertEqual(row["attention_final_question"], 1)
        self.assertEqual(row["attention_incomplete_todo"], 0)
        self.assertEqual(row["attention_tail_revision"], 1)
        self.assertEqual([item["text"] for item in stored_text], ["", ""])

    def test_source_backed_append_reconciles_full_metadata_before_windows(self) -> None:
        class OffsetRelativeAppendAdapter(TranscriptAdapter):
            harness = Harness.CODEX

            def discover(self) -> list[Path]:
                return []

            def parse_chunk(self, path, data, *, start_offset):
                raise NotImplementedError

            def parse_path(self, path, data, *, start_offset):
                if start_offset:
                    result = _result(
                        ("assistant", "follow-up"),
                        external_id="offset-relative",
                    )
                elif path.stat().st_size == 1:
                    result = _result(
                        ("user", "request"),
                        ("assistant", "response"),
                        external_id="offset-relative",
                    )
                else:
                    result = _result(
                        ("user", "request"),
                        ("assistant", "response"),
                        ("user", "next request"),
                        ("assistant", "follow-up"),
                        external_id="offset-relative",
                    )
                return [
                    result.model_copy(
                        update={"bytes_consumed": path.stat().st_size}
                    )
                ]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "offset-relative.jsonl"
            repo = self._repo(root)
            adapter = OffsetRelativeAppendAdapter()
            path.write_bytes(b"a")
            _ingest_one(repo, adapter, path, IngestStats())
            repo.conn.commit()

            path.write_bytes(b"ab")
            _ingest_one(repo, adapter, path, IngestStats())
            repo.conn.commit()

            messages = repo.conn.execute(
                "SELECT seq, role FROM messages ORDER BY seq"
            ).fetchall()
            windows = repo.conn.execute(
                "SELECT request_message_id, response_message_id "
                "FROM exchange_windows ORDER BY request_message_id"
            ).fetchall()
            fk_errors = repo.conn.execute("PRAGMA foreign_key_check").fetchall()

        self.assertEqual(
            [(row["seq"], row["role"]) for row in messages],
            [(1, "user"), (2, "assistant"), (3, "user"), (4, "assistant")],
        )
        self.assertEqual(
            [(row["request_message_id"], row["response_message_id"]) for row in windows],
            [
                ("codex:offset-relative:m:1", "codex:offset-relative:m:2"),
                ("codex:offset-relative:m:3", "codex:offset-relative:m:4"),
            ],
        )
        self.assertEqual(fk_errors, [])

    def test_divergent_source_backed_append_is_frozen_without_retry_failure(self) -> None:
        class OffsetRelativeAppendAdapter(TranscriptAdapter):
            harness = Harness.CODEX

            def discover(self) -> list[Path]:
                return [path]

            def parse_chunk(self, path, data, *, start_offset):
                raise NotImplementedError

            def parse_path(self, path, data, *, start_offset):
                if start_offset:
                    result = _result(
                        ("assistant", "follow-up"), external_id="divergent"
                    )
                elif path.stat().st_size == 1:
                    result = _result(
                        ("user", "request"),
                        ("assistant", "response"),
                        external_id="divergent",
                    )
                else:
                    result = _result(
                        ("user", "request"),
                        ("assistant", "response"),
                        ("user", "next request"),
                        ("assistant", "follow-up"),
                        external_id="divergent",
                    )
                return [
                    result.model_copy(
                        update={"bytes_consumed": path.stat().st_size}
                    )
                ]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "divergent.jsonl"
            repo = self._repo(root)
            adapter = OffsetRelativeAppendAdapter()
            path.write_bytes(b"a")
            _ingest_one(repo, adapter, path, IngestStats())
            repo.conn.commit()
            repo.conn.execute(
                "UPDATE messages SET content_hash = 'stale' WHERE "
                "id = 'codex:divergent:m:1'"
            )
            repo.conn.commit()

            path.write_bytes(b"ab")
            with mock.patch("agentlog.ingest.pipeline.adapter_for", return_value=adapter):
                stats = ingest_harness(repo, "codex", changed_paths=[path])
            session = repo.conn.execute(
                "SELECT source_sync_status FROM sessions WHERE id = 'codex:divergent'"
            ).fetchone()

        self.assertEqual(stats.failed, 0)
        self.assertEqual(stats.skipped, 1)
        self.assertIn("identity frozen", stats.warnings[0])
        self.assertEqual(session["source_sync_status"], "frozen_diverged")

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

    def test_unchanged_source_backed_tail_requires_explicit_maintenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / ROLLOUT
            repo = self._repo(root)
            path.write_bytes(
                _line("user", "first half")
                + _line("assistant", "Should I continue?")
            )
            _ingest_one(repo, CodexAdapter(), path, IngestStats())
            repo.conn.execute(
                """
                UPDATE sessions SET
                    attention_final_question = NULL,
                    attention_tail_revision = NULL
                """
            )
            repo.conn.commit()

            with mock.patch(
                "agentlog.ingest.codex.CODEX_SESSIONS_DIR", root
            ), mock.patch(
                "agentlog.ingest.pipeline.adapters",
                return_value=[CodexAdapter()],
            ):
                event_stats = ingest_harness(
                    repo,
                    "codex",
                    changed_paths=[path],
                    catch_up_attention_tails=True,
                )
                pending = repo.conn.execute(
                    "SELECT attention_tail_revision FROM sessions"
                ).fetchone()
                default_stats = ingest_all(repo)
                default_pending = repo.conn.execute(
                    "SELECT attention_tail_revision FROM sessions"
                ).fetchone()
                maintenance_stats = ingest_harness(
                    repo, "codex", catch_up_attention_tails=True
                )
                current = repo.conn.execute(
                    """
                    SELECT attention_final_question, attention_tail_revision
                    FROM sessions
                    """
                ).fetchone()
                stored_text = repo.conn.execute(
                    "SELECT text FROM messages ORDER BY seq"
                ).fetchall()
                before_repeat = repo.conn.total_changes
                repeat_stats = ingest_harness(
                    repo, "codex", catch_up_attention_tails=True
                )

        assert pending is not None
        assert default_pending is not None
        assert current is not None
        self.assertGreaterEqual(event_stats.skipped, 1)
        self.assertIsNone(pending["attention_tail_revision"])
        self.assertGreaterEqual(default_stats.skipped, 1)
        self.assertIsNone(default_pending["attention_tail_revision"])
        self.assertEqual(maintenance_stats.parsed, 1)
        self.assertEqual(current["attention_final_question"], 1)
        self.assertEqual(current["attention_tail_revision"], 1)
        self.assertEqual([row["text"] for row in stored_text], ["", ""])
        self.assertEqual(repeat_stats.parsed, 0)
        self.assertGreaterEqual(repeat_stats.skipped, 1)
        self.assertEqual(repo.conn.total_changes, before_repeat)
