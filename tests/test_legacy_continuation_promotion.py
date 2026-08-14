from __future__ import annotations

import tempfile
import unittest
import sqlite3
from pathlib import Path
from unittest import mock

from agentlog.db.repository import (
    LEGACY_MATERIALIZED,
    SOURCE_BACKED,
    Repository,
    TranscriptStorageError,
)
from agentlog.db.schema import connect, init_db
from agentlog.config import PARSER_VERSION
from agentlog.ingest.base import (
    TranscriptAdapter,
    content_hash_text,
    hash_prefix,
    sqlite_fingerprint,
)
from agentlog.ingest.pipeline import IngestStats, _ingest_one
from agentlog.normalize.models import (
    Harness,
    NormalizedMessage,
    NormalizedSession,
    ParseResult,
    SkillExposure,
    TokenUsage,
    ToolEvent,
)
from agentlog.session_identity import logical_projection


def _result(
    harness: Harness,
    external_id: str,
    *turns: tuple[str, str],
    marker: str = "current",
) -> ParseResult:
    return ParseResult(
        session=NormalizedSession(
            harness=harness,
            external_id=external_id,
            model="gpt-5.6-sol",
            provider="openai",
            agent_profile="codex" if harness == Harness.T3CODE else None,
        ),
        messages=[
            NormalizedMessage(
                seq=seq,
                role=role,
                text=text,
                content_hash=content_hash_text(text),
            )
            for seq, (role, text) in enumerate(turns, start=1)
        ],
        tool_events=[
            ToolEvent(
                seq=1,
                message_seq=len(turns),
                tool_name=f"tool-{marker}",
                action="call",
            )
        ],
        skill_exposures=[
            SkillExposure(
                message_seq=1,
                skill_name=f"skill-{marker}",
                exposure_type="matched",
            )
        ],
        token_usages=[
            TokenUsage(
                seq=1,
                message_seq=len(turns),
                granularity="turn",
                usage_source=marker,
                total_tokens=10,
            )
        ],
    )


class StaticAdapter(TranscriptAdapter):
    supports_byte_append = False

    def __init__(
        self, harness: Harness, path: Path, results: list[ParseResult]
    ) -> None:
        self.harness = harness
        self.path = path
        self.results = results

    def discover(self) -> list[Path]:
        return [self.path]

    def parse_chunk(self, path, data, *, start_offset):
        raise NotImplementedError

    def parse_path(self, path, data, *, start_offset):
        return [
            result.model_copy(
                deep=True,
                update={"bytes_consumed": path.stat().st_size},
            )
            for result in self.results
        ]


class BlockedFullReplayAdapter(StaticAdapter):
    supports_byte_append = True

    def parse_path(self, path, data, *, start_offset):
        result = super().parse_path(path, data, start_offset=start_offset)[0]
        if start_offset == 0:
            result.extras.update(
                {
                    "checkpoint_blocked": True,
                    "checkpoint_blocked_reason": "ambiguous full replay",
                }
            )
        return [result]


class LegacyContinuationPromotionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.conn = connect(self.root / "agentlog.db")
        init_db(self.conn)
        self.repo = Repository(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def _seed(
        self,
        harness: Harness,
        external_id: str,
        path: Path,
        result: ParseResult,
    ) -> tuple[int, str]:
        path.write_text("source", encoding="utf-8")
        artifact_id = self.repo.upsert_artifact(
            harness=harness.value,
            path=str(path),
            size=path.stat().st_size,
            mtime_ns=path.stat().st_mtime_ns,
            content_hash=hash_prefix(path, path.stat().st_size),
            parsed_offset=path.stat().st_size,
            parser_version="legacy-parser",
            transcript_storage=LEGACY_MATERIALIZED,
        )
        session_id = self.repo.save_parse_result(
            artifact_id=artifact_id,
            result=result,
            append=False,
            transcript_storage=LEGACY_MATERIALIZED,
        )
        self.conn.commit()
        return artifact_id, session_id

    def _rows(self, table: str, session_id: str) -> list[tuple[object, ...]]:
        return [
            tuple(row)
            for row in self.conn.execute(
                f"SELECT * FROM {table} WHERE session_id = ? ORDER BY 1",
                (session_id,),
            )
        ]

    def test_strict_extension_promotes_same_session_without_db_text(self) -> None:
        path = self.root / "t3.source"
        old = _result(
            Harness.T3CODE,
            "root",
            ("user", "legacyuniqueterm request"),
            ("assistant", "old response"),
            marker="old",
        )
        artifact_id, session_id = self._seed(
            Harness.T3CODE, "root", path, old
        )
        current = _result(
            Harness.T3CODE,
            "root",
            ("user", "legacyuniqueterm request"),
            ("assistant", "old response"),
            ("user", "continued request"),
            ("assistant", "continued response"),
        )

        _ingest_one(
            self.repo,
            StaticAdapter(Harness.T3CODE, path, [current]),
            path,
            IngestStats(),
        )
        self.conn.commit()

        session = self.conn.execute(
            "SELECT artifact_id, transcript_storage FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        self.assertEqual(session["artifact_id"], artifact_id)
        self.assertEqual(session["transcript_storage"], SOURCE_BACKED)
        messages = self.conn.execute(
            "SELECT id, seq, role, text, content_hash FROM messages "
            "WHERE session_id = ? ORDER BY seq",
            (session_id,),
        ).fetchall()
        self.assertEqual([row["seq"] for row in messages], [1, 2, 3, 4])
        self.assertEqual([row["text"] for row in messages], ["", "", "", ""])
        self.assertEqual(
            [row["id"] for row in messages],
            [f"{session_id}:m:{seq}" for seq in range(1, 5)],
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM messages_fts "
                "WHERE messages_fts MATCH 'legacyuniqueterm'"
            ).fetchone()[0],
            0,
        )
        self.conn.execute(
            "INSERT INTO messages_fts(messages_fts) VALUES('integrity-check')"
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT tool_name FROM tool_events WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0],
            "tool-current",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM exchange_windows WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0],
            2,
        )

    def test_unchanged_legacy_session_is_an_idempotent_no_op(self) -> None:
        path = self.root / "same.source"
        unchanged = _result(
            Harness.T3CODE,
            "same",
            ("user", "same request"),
            ("assistant", "same response"),
        )
        _, session_id = self._seed(
            Harness.T3CODE, "same", path, unchanged
        )
        before = self._rows("messages", session_id)

        adapter = StaticAdapter(Harness.T3CODE, path, [unchanged])
        _ingest_one(self.repo, adapter, path, IngestStats())
        self.conn.commit()
        _ingest_one(self.repo, adapter, path, IngestStats())
        self.conn.commit()

        self.assertEqual(
            self.repo.session_transcript_storage(session_id),
            LEGACY_MATERIALIZED,
        )
        self.assertEqual(self._rows("messages", session_id), before)

    def test_divergence_and_shrink_fail_without_mutation(self) -> None:
        for label, current, error in (
            (
                "diverge",
                _result(
                    Harness.T3CODE,
                    "root",
                    ("user", "rewritten"),
                    ("assistant", "old response"),
                    ("user", "append"),
                ),
                "diverged",
            ),
            (
                "shrink",
                _result(Harness.T3CODE, "root", ("user", "old request")),
                "shrank",
            ),
        ):
            with self.subTest(label=label):
                path = self.root / f"{label}.source"
                old = _result(
                    Harness.T3CODE,
                    "root",
                    ("user", "old request"),
                    ("assistant", "old response"),
                )
                _, session_id = self._seed(
                    Harness.T3CODE, "root", path, old
                )
                before = self._rows("messages", session_id)
                with self.assertRaisesRegex(TranscriptStorageError, error):
                    _ingest_one(
                        self.repo,
                        StaticAdapter(Harness.T3CODE, path, [current]),
                        path,
                        IngestStats(),
                    )
                self.conn.rollback()
                self.assertEqual(self._rows("messages", session_id), before)
                self.assertEqual(
                    self.repo.session_transcript_storage(session_id),
                    LEGACY_MATERIALIZED,
                )
                self.conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
                self.conn.execute("DELETE FROM artifacts WHERE path = ?", (str(path),))
                self.conn.commit()

    def test_claim_or_task_cluster_blocks_promotion(self) -> None:
        for blocker in ("claim", "cluster"):
            with self.subTest(blocker=blocker):
                path = self.root / f"{blocker}.source"
                old = _result(
                    Harness.T3CODE,
                    blocker,
                    ("user", "request"),
                    ("assistant", "response"),
                )
                _, session_id = self._seed(
                    Harness.T3CODE, blocker, path, old
                )
                if blocker == "claim":
                    self.conn.execute(
                        "INSERT INTO claims "
                        "(id,kind,subject,predicate,value_json,scope_type,derivation,"
                        "observed_at,extractor_name,extractor_version,created_at,updated_at) "
                        "VALUES ('claim','fact','s','p','{}','session','llm',"
                        "'now','e','1','now','now')"
                    )
                    self.conn.execute(
                        "INSERT INTO claim_evidence "
                        "(id,claim_id,session_id,message_id,created_at) "
                        "VALUES ('e','claim',?,?, 'now')",
                        (session_id, f"{session_id}:m:1"),
                    )
                else:
                    self.conn.execute(
                        "INSERT INTO task_clusters "
                        "(id,root_session_id,cluster_kind) "
                        "VALUES ('cluster',?,'root')",
                        (session_id,),
                    )
                self.conn.commit()
                before = self._rows("messages", session_id)
                current = _result(
                    Harness.T3CODE,
                    blocker,
                    ("user", "request"),
                    ("assistant", "response"),
                    ("user", "append"),
                )
                with self.assertRaises(TranscriptStorageError):
                    _ingest_one(
                        self.repo,
                        StaticAdapter(Harness.T3CODE, path, [current]),
                        path,
                        IngestStats(),
                    )
                self.conn.rollback()
                self.assertEqual(self._rows("messages", session_id), before)
                self.assertEqual(
                    self.repo.session_transcript_storage(session_id),
                    LEGACY_MATERIALIZED,
                )
                self.conn.execute("DELETE FROM claim_evidence")
                self.conn.execute("DELETE FROM claims")
                self.conn.execute("DELETE FROM task_clusters")
                self.conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
                self.conn.execute("DELETE FROM artifacts WHERE path = ?", (str(path),))
                self.conn.commit()

    def test_owner_insight_provenance_survives_strict_legacy_extension(self) -> None:
        path = self.root / "owner-insight.source"
        old = _result(
            Harness.T3CODE,
            "owner-insight",
            ("user", "request"),
            ("assistant", "response"),
        )
        _, session_id = self._seed(Harness.T3CODE, "owner-insight", path, old)
        self.conn.execute(
            """
            INSERT INTO owner_insight_batches(
                id, content_hash, prompt_hash, prompt_version, redaction_version,
                status, prepared_at, provenance_json
            ) VALUES ('owner-batch', 'content', 'prompt', 'v2', 'v1',
                      'prepared', '2026-08-12T00:00:00+00:00', '{}')
            """
        )
        self.conn.execute(
            """
            INSERT INTO owner_insight_batch_messages(
                batch_id, session_id, message_id, seq, content_hash, role,
                source_snapshot_json, source_role
            ) VALUES ('owner-batch', ?, ?, 1, 'hash', 'user', '{}', 'new')
            """,
            (session_id, f"{session_id}:m:1"),
        )
        self.conn.commit()
        provenance_tables = (
            "owner_insight_batches",
            "owner_insight_batch_messages",
        )
        before = {
            table: [tuple(row) for row in self.conn.execute(f"SELECT * FROM {table} ORDER BY 1")]
            for table in provenance_tables
        }
        current = _result(
            Harness.T3CODE,
            "owner-insight",
            ("user", "request"),
            ("assistant", "response"),
            ("user", "append"),
        )

        _ingest_one(
            self.repo,
            StaticAdapter(Harness.T3CODE, path, [current]),
            path,
            IngestStats(),
        )
        self.conn.commit()
        after = {
            table: [tuple(row) for row in self.conn.execute(f"SELECT * FROM {table} ORDER BY 1")]
            for table in provenance_tables
        }
        self.assertEqual(after, before)
        self.assertEqual(
            self.repo.session_transcript_storage(session_id),
            SOURCE_BACKED,
        )

    def test_injected_failure_rolls_back_storage_text_and_windows(self) -> None:
        path = self.root / "rollback.source"
        old = _result(
            Harness.T3CODE,
            "rollback",
            ("user", "request"),
            ("assistant", "response"),
        )
        _, session_id = self._seed(
            Harness.T3CODE, "rollback", path, old
        )
        self.repo.replace_exchange_windows(
            session_id,
            [(f"{session_id}:m:1", f"{session_id}:m:2", "input")],
        )
        self.conn.commit()
        before_messages = self._rows("messages", session_id)
        before_windows = self._rows("exchange_windows", session_id)
        current = _result(
            Harness.T3CODE,
            "rollback",
            ("user", "request"),
            ("assistant", "response"),
            ("user", "append"),
        )
        replace = self.repo.replace_exchange_windows

        def fail_after_replace(sid, windows):
            replace(sid, windows)
            raise RuntimeError("injected failure")

        with mock.patch.object(
            self.repo, "replace_exchange_windows", side_effect=fail_after_replace
        ):
            with self.assertRaisesRegex(RuntimeError, "injected failure"):
                _ingest_one(
                    self.repo,
                    StaticAdapter(Harness.T3CODE, path, [current]),
                    path,
                    IngestStats(),
                )
        self.conn.rollback()
        self.assertEqual(self._rows("messages", session_id), before_messages)
        self.assertEqual(self._rows("exchange_windows", session_id), before_windows)
        self.assertEqual(
            self.repo.session_transcript_storage(session_id),
            LEGACY_MATERIALIZED,
        )

    def test_mixed_artifact_promotes_only_extended_session(self) -> None:
        path = self.root / "mixed.source"
        old_root = _result(
            Harness.T3CODE, "root", ("user", "root old")
        )
        artifact_id, root_id = self._seed(
            Harness.T3CODE, "root", path, old_root
        )
        unchanged = _result(
            Harness.T3CODE, "unchanged", ("user", "stay legacy")
        )
        unchanged_id = self.repo.save_parse_result(
            artifact_id=artifact_id,
            result=unchanged,
            append=False,
            transcript_storage=LEGACY_MATERIALIZED,
        )
        already_source = _result(
            Harness.T3CODE, "source", ("user", "already source")
        )
        source_id = self.repo.save_parse_result(
            artifact_id=artifact_id,
            result=already_source,
            append=False,
            transcript_storage=SOURCE_BACKED,
        )
        self.conn.commit()
        extended = _result(
            Harness.T3CODE,
            "root",
            ("user", "root old"),
            ("assistant", "root continued"),
        )

        _ingest_one(
            self.repo,
            StaticAdapter(
                Harness.T3CODE,
                path,
                [extended, unchanged, already_source],
            ),
            path,
            IngestStats(),
        )
        self.conn.commit()

        self.assertEqual(
            self.repo.session_transcript_storage(root_id), SOURCE_BACKED
        )
        self.assertEqual(
            self.repo.session_transcript_storage(unchanged_id),
            LEGACY_MATERIALIZED,
        )
        self.assertEqual(
            self.repo.session_transcript_storage(source_id), SOURCE_BACKED
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
            3,
        )

    def test_duplicate_parser_identity_fails_before_artifact_update(self) -> None:
        path = self.root / "duplicate.source"
        old = _result(Harness.T3CODE, "root", ("user", "old"))
        artifact_id, _ = self._seed(Harness.T3CODE, "root", path, old)
        before = self.conn.execute(
            "SELECT parser_version, content_hash FROM artifacts WHERE id = ?",
            (artifact_id,),
        ).fetchone()
        duplicate = _result(
            Harness.T3CODE,
            "root",
            ("user", "old"),
            ("assistant", "append"),
        )
        with self.assertRaisesRegex(RuntimeError, "duplicate session identity"):
            _ingest_one(
                self.repo,
                StaticAdapter(Harness.T3CODE, path, [duplicate, duplicate]),
                path,
                IngestStats(),
            )
        self.conn.rollback()
        after = self.conn.execute(
            "SELECT parser_version, content_hash FROM artifacts WHERE id = ?",
            (artifact_id,),
        ).fetchone()
        self.assertEqual(tuple(after), tuple(before))

    def test_raw_storage_flip_is_rejected_until_text_is_blank(self) -> None:
        path = self.root / "raw.source"
        old = _result(Harness.T3CODE, "raw", ("user", "retained"))
        _, session_id = self._seed(Harness.T3CODE, "raw", path, old)
        with self.assertRaisesRegex(Exception, "cannot move backward"):
            self.conn.execute(
                "UPDATE sessions SET transcript_storage = ? WHERE id = ?",
                (SOURCE_BACKED, session_id),
            )
        self.conn.rollback()
        self.assertEqual(
            self.repo.session_transcript_storage(session_id),
            LEGACY_MATERIALIZED,
        )

    def test_linked_t3_and_codex_roots_promote_in_either_order(self) -> None:
        for order in ((Harness.T3CODE, Harness.CODEX), (Harness.CODEX, Harness.T3CODE)):
            with self.subTest(order=[item.value for item in order]):
                suffix = "-".join(item.value for item in order)
                t3_path = self.root / f"{suffix}-t3.source"
                codex_path = self.root / f"{suffix}-codex.source"
                t3_old = _result(
                    Harness.T3CODE, f"root-{suffix}", ("user", "shared old")
                )
                codex_old = _result(
                    Harness.CODEX, f"backing-{suffix}", ("user", "shared old")
                )
                _, t3_id = self._seed(
                    Harness.T3CODE, f"root-{suffix}", t3_path, t3_old
                )
                _, codex_id = self._seed(
                    Harness.CODEX, f"backing-{suffix}", codex_path, codex_old
                )
                self.conn.execute(
                    "INSERT INTO session_links "
                    "(source_session_id,target_session_id,link_type,"
                    "target_harness,target_external_id,link_role) "
                    "VALUES (?,?,'provider_backing','codex',?,'root')",
                    (t3_id, codex_id, f"backing-{suffix}"),
                )
                self.conn.commit()
                current = {
                    Harness.T3CODE: _result(
                        Harness.T3CODE,
                        f"root-{suffix}",
                        ("user", "shared old"),
                        ("assistant", "t3 continuation"),
                    ),
                    Harness.CODEX: _result(
                        Harness.CODEX,
                        f"backing-{suffix}",
                        ("user", "shared old"),
                        ("assistant", "provider continuation"),
                    ),
                }
                paths = {Harness.T3CODE: t3_path, Harness.CODEX: codex_path}

                for harness in order:
                    _ingest_one(
                        self.repo,
                        StaticAdapter(harness, paths[harness], [current[harness]]),
                        paths[harness],
                        IngestStats(),
                    )
                    self.conn.commit()
                for harness in reversed(order):
                    _ingest_one(
                        self.repo,
                        StaticAdapter(harness, paths[harness], [current[harness]]),
                        paths[harness],
                        IngestStats(),
                    )
                    self.conn.commit()

                self.assertEqual(
                    self.repo.session_transcript_storage(t3_id), SOURCE_BACKED
                )
                self.assertEqual(
                    self.repo.session_transcript_storage(codex_id), SOURCE_BACKED
                )
                self.assertEqual(
                    self.conn.execute(
                        "SELECT COUNT(*) FROM sessions WHERE id IN (?, ?)",
                        (t3_id, codex_id),
                    ).fetchone()[0],
                    2,
                )
                projection = logical_projection(
                    self.conn, t3_id, Harness.T3CODE.value
                )
                self.assertIsNone(projection["transcript_session_id"])
                self.assertEqual(projection["runtime_harness"], "codex")

    def test_blocked_full_replay_cannot_advance_append_checkpoint(self) -> None:
        path = self.root / "blocked-full.jsonl"
        old = _result(Harness.CODEX, "blocked", ("user", "old"))
        artifact_id, session_id = self._seed(
            Harness.CODEX, "blocked", path, old
        )
        size = path.stat().st_size
        self.conn.execute(
            "UPDATE artifacts SET parser_version = ? WHERE id = ?",
            (PARSER_VERSION, artifact_id),
        )
        self.conn.commit()
        before = tuple(
            self.conn.execute(
                "SELECT size,mtime_ns,content_hash,parsed_offset,parser_version "
                "FROM artifacts WHERE id = ?",
                (artifact_id,),
            ).fetchone()
        )
        path.write_text("source append", encoding="utf-8")
        current = _result(
            Harness.CODEX,
            "blocked",
            ("user", "old"),
            ("assistant", "new"),
        )

        with self.assertRaisesRegex(RuntimeError, "ambiguous full replay"):
            _ingest_one(
                self.repo,
                BlockedFullReplayAdapter(Harness.CODEX, path, [current]),
                path,
                IngestStats(),
            )
        self.conn.rollback()

        after = tuple(
            self.conn.execute(
                "SELECT size,mtime_ns,content_hash,parsed_offset,parser_version "
                "FROM artifacts WHERE id = ?",
                (artifact_id,),
            ).fetchone()
        )
        self.assertEqual(after, before)
        self.assertEqual(
            self.repo.session_transcript_storage(session_id),
            LEGACY_MATERIALIZED,
        )
        self.assertEqual(before[0], size)

    def test_shared_sqlite_freezes_divergence_but_ingests_safe_siblings(self) -> None:
        path = self.root / "shared.sqlite"
        with sqlite3.connect(path) as source:
            source.execute("CREATE TABLE state (value TEXT)")
            source.execute("INSERT INTO state VALUES ('current')")
        fingerprint = sqlite_fingerprint(path)
        artifact_id = self.repo.upsert_artifact(
            harness="t3code",
            path=str(path),
            size=path.stat().st_size,
            mtime_ns=path.stat().st_mtime_ns,
            content_hash=fingerprint,
            parsed_offset=path.stat().st_size,
            parser_version="legacy-parser",
            transcript_storage=LEGACY_MATERIALIZED,
        )
        extended_old = _result(
            Harness.T3CODE, "extended", ("user", "extended old")
        )
        frozen_old = _result(
            Harness.T3CODE, "frozen", ("user", "frozen old")
        )
        extended_id = self.repo.save_parse_result(
            artifact_id=artifact_id,
            result=extended_old,
            append=False,
            transcript_storage=LEGACY_MATERIALIZED,
        )
        frozen_id = self.repo.save_parse_result(
            artifact_id=artifact_id,
            result=frozen_old,
            append=False,
            transcript_storage=LEGACY_MATERIALIZED,
        )
        self.conn.commit()
        frozen_before = self._rows("messages", frozen_id)
        results = [
            _result(
                Harness.T3CODE,
                "extended",
                ("user", "extended old"),
                ("assistant", "extended now"),
            ),
            _result(
                Harness.T3CODE,
                "frozen",
                ("user", "rewritten old"),
                ("assistant", "unsafe append"),
            ),
            _result(
                Harness.T3CODE,
                "new",
                ("user", "new session"),
                ("assistant", "new response"),
            ),
        ]
        stats = IngestStats()

        _ingest_one(
            self.repo,
            StaticAdapter(Harness.T3CODE, path, results),
            path,
            stats,
        )
        self.conn.commit()

        self.assertEqual(
            self.repo.session_transcript_storage(extended_id), SOURCE_BACKED
        )
        self.assertEqual(
            self.repo.session_transcript_storage(frozen_id),
            LEGACY_MATERIALIZED,
        )
        self.assertEqual(self._rows("messages", frozen_id), frozen_before)
        diagnostic = self.conn.execute(
            "SELECT source_sync_status,source_sync_warning "
            "FROM sessions WHERE id = ?",
            (frozen_id,),
        ).fetchone()
        self.assertEqual(diagnostic["source_sync_status"], "frozen_diverged")
        self.assertIn("diverged", diagnostic["source_sync_warning"])
        self.assertTrue(any("identity frozen" in item for item in stats.warnings))
        self.assertEqual(
            self.repo.session_transcript_storage("t3code:new"), SOURCE_BACKED
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
            3,
        )

        again = IngestStats()
        _ingest_one(
            self.repo,
            StaticAdapter(Harness.T3CODE, path, results),
            path,
            again,
        )
        self.conn.commit()
        self.assertEqual(again.skipped, 1)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
            3,
        )


if __name__ == "__main__":
    unittest.main()
