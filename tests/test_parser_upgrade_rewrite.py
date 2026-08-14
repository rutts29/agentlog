from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from agentlog.config import PARSER_VERSION
from agentlog.analysis.owner_notes import reset_owner_insight_session
from agentlog.db.repository import SOURCE_BACKED, Repository, TranscriptStorageError
from agentlog.db.schema import connect, init_db
from agentlog.ingest.base import TranscriptAdapter, content_hash_text, hash_prefix
from agentlog.ingest.codex import CodexAdapter
from agentlog.ingest.pipeline import (
    IngestStats,
    _ingest_one,
    _windows_from_source_result,
    ingest_harness,
)
from agentlog.normalize.models import (
    Harness,
    NormalizedMessage,
    NormalizedSession,
    ParseResult,
    SkillExposure,
    TokenUsage,
    ToolEvent,
)
from agentlog.session_identity import INTERNAL_APPROVAL_GUARDIAN_THREAD_SOURCE


class StaticAdapter(TranscriptAdapter):
    harness = Harness.CODEX
    supports_byte_append = False

    def __init__(self, path: Path, result: ParseResult) -> None:
        self.path = path
        self.result = result

    def discover(self) -> list[Path]:
        return [self.path]

    def parse_chunk(self, path, data, *, start_offset):
        raise NotImplementedError

    def parse_path(self, path, data, *, start_offset):
        return [
            self.result.model_copy(
                deep=True,
                update={"bytes_consumed": path.stat().st_size},
            )
        ]


class AppendBoundaryAdapter(StaticAdapter):
    supports_byte_append = True

    def parse_path(self, path, data, *, start_offset):
        result = super().parse_path(path, data, start_offset=start_offset)[0]
        if start_offset > 0:
            result.extras["requires_full_reparse"] = True
        return [result]


def _message(seq: int, role: str, text: str) -> NormalizedMessage:
    return NormalizedMessage(
        seq=seq,
        role=role,
        text=text,
        content_hash=content_hash_text(text),
    )


def _result(
    *turns: tuple[str, str],
    inherited_message_count: int,
    marker: str,
) -> ParseResult:
    return ParseResult(
        session=NormalizedSession(
            harness=Harness.CODEX,
            external_id="worker",
            parent_session_id="codex:parent",
            originator="t3code_desktop",
            thread_source="subagent",
        ),
        messages=[
            _message(seq, role, text)
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
                total_tokens=len(turns) * 10,
            )
        ],
        extras={
            "inherited_message_count": inherited_message_count,
            "inherited_record_count": inherited_message_count + 2,
            "fork_context_status": "trimmed",
            "fork_context_boundary": f"boundary-{marker}",
            "session_links": [
                {
                    "link_type": "provider_backing",
                    "target_harness": "t3code",
                    "target_external_id": "owner",
                    "link_role": "worker",
                }
            ],
        },
    )


class ParserUpgradeRewriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root / "rollout-worker.jsonl"
        self.path.write_text("source", encoding="utf-8")
        self.conn = connect(self.root / "agentlog.db")
        init_db(self.conn)
        self.repo = Repository(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def _old_result(self) -> ParseResult:
        old = _result(
            ("user", "copied parent request"),
            ("assistant", "copied parent answer"),
            ("user", "worker-local request"),
            ("assistant", "old worker answer"),
            inherited_message_count=2,
            marker="old",
        )
        old.session.started_at = datetime(
            2026, 8, 12, 3, 0, tzinfo=timezone.utc
        )
        old.session.ended_at = datetime(
            2026, 8, 12, 3, 30, tzinfo=timezone.utc
        )
        old.session.repo = "old/repo"
        old.session.cwd = "/old/repo"
        old.session.branch = "old-branch"
        old.session.commit_sha = "old-commit"
        old.session.model = "old-model"
        old.session.provider = "old-provider"
        old.session.agent_profile = "old-profile"
        old.session.effort = "high"
        old.session.effort_source = "old-source"
        return old

    def _seed_old(self) -> tuple[int, str]:
        size = self.path.stat().st_size
        artifact_id = self.repo.upsert_artifact(
            harness="codex",
            path=str(self.path),
            size=size,
            mtime_ns=self.path.stat().st_mtime_ns,
            content_hash=hash_prefix(self.path, size),
            parsed_offset=size,
            parser_version="old-parser",
            transcript_storage=SOURCE_BACKED,
        )
        old = self._old_result()
        session_id = self.repo.save_parse_result(
            artifact_id=artifact_id,
            result=old,
            append=False,
            transcript_storage=SOURCE_BACKED,
        )
        self.repo.replace_exchange_windows(
            session_id, _windows_from_source_result(session_id, old)
        )
        self.conn.execute(
            "INSERT INTO session_commits "
            "(session_id, commit_sha, join_method) VALUES (?, 'abc123', 'explicit')",
            (session_id,),
        )
        self.conn.commit()
        return artifact_id, session_id

    def _snapshot(self) -> dict[str, list[tuple[object, ...]]]:
        tables = (
            "artifacts",
            "sessions",
            "messages",
            "tool_events",
            "skill_exposures",
            "token_usage",
            "exchange_windows",
            "session_links",
            "session_commits",
            "task_clusters",
            "claims",
            "claim_evidence",
            "owner_insight_batches",
            "owner_insight_batch_messages",
            "owner_insight_seen_messages",
            "owner_insight_session_state",
            "parser_upgrade_freezes",
        )
        return {
            table: [
                tuple(row)
                for row in self.conn.execute(
                    f"SELECT * FROM {table} ORDER BY 1"
                )
            ]
            for table in tables
        }

    def test_parser_upgrade_exactly_rewrites_source_backed_shape(self) -> None:
        artifact_id, session_id = self._seed_old()
        local_only = _result(
            ("user", "worker-local request"),
            ("assistant", "new worker answer"),
            inherited_message_count=2,
            marker="new",
        )
        local_only.session.parent_session_id = None
        local_only.session.started_at = datetime(
            2026, 8, 12, 4, 0, tzinfo=timezone.utc
        )
        local_only.session.repo = "new/repo"
        local_only.session.cwd = "/new/repo"

        _ingest_one(
            self.repo,
            StaticAdapter(self.path, local_only),
            self.path,
            IngestStats(),
        )
        self.conn.commit()

        session = self.conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        assert session is not None
        artifact = self.repo.get_artifact_by_path(str(self.path))
        assert artifact is not None
        self.assertEqual(artifact.id, artifact_id)
        self.assertEqual(artifact.parser_version, PARSER_VERSION)
        self.assertEqual(session["artifact_id"], artifact_id)
        self.assertEqual(session["transcript_storage"], SOURCE_BACKED)
        self.assertEqual(session["originator"], "t3code_desktop")
        self.assertEqual(session["thread_source"], "subagent")
        self.assertIsNone(session["parent_session_id"])
        self.assertEqual(session["started_at"], "2026-08-12T04:00:00+00:00")
        self.assertIsNone(session["ended_at"])
        self.assertEqual(session["repo"], "new/repo")
        self.assertEqual(session["cwd"], "/new/repo")
        for field in (
            "branch",
            "commit_sha",
            "model",
            "model_canonical",
            "provider",
            "agent_profile",
            "effort",
            "effort_source",
        ):
            self.assertIsNone(session[field])
        self.assertEqual(session["inherited_message_count"], 2)
        self.assertEqual(session["fork_context_boundary"], "boundary-new")
        self.assertEqual(
            [
                (row["seq"], row["role"], row["text"], row["content_hash"])
                for row in self.conn.execute(
                    "SELECT seq, role, text, content_hash FROM messages "
                    "WHERE session_id = ? ORDER BY seq",
                    (session_id,),
                )
            ],
            [
                (1, "user", "", content_hash_text("worker-local request")),
                (2, "assistant", "", content_hash_text("new worker answer")),
            ],
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT tool_name FROM tool_events WHERE session_id = ?",
                (session_id,),
            ).fetchone()["tool_name"],
            "tool-new",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT skill_name FROM skill_exposures WHERE session_id = ?",
                (session_id,),
            ).fetchone()["skill_name"],
            "skill-new",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT usage_source FROM token_usage WHERE session_id = ?",
                (session_id,),
            ).fetchone()["usage_source"],
            "new",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) AS c FROM exchange_windows WHERE session_id = ?",
                (session_id,),
            ).fetchone()["c"],
            1,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) AS c FROM session_links "
                "WHERE source_session_id = ?",
                (session_id,),
            ).fetchone()["c"],
            1,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) AS c FROM session_commits WHERE session_id = ?",
                (session_id,),
            ).fetchone()["c"],
            1,
        )

    def test_exact_parser_upgrade_advances_evidence_bearing_checkpoint(self) -> None:
        artifact_id, session_id = self._seed_old()
        self.conn.execute(
            "UPDATE artifacts SET parser_version = '15' WHERE id = ?",
            (artifact_id,),
        )
        self.conn.execute(
            """
            INSERT INTO claims(
                id, kind, subject, predicate, value_json, scope_type,
                derivation, observed_at, extractor_name, extractor_version,
                created_at, updated_at
            ) VALUES(
                'claim', 'fact', 'session', 'observed', '{}', 'global',
                'deterministic', '2026-08-12T00:00:00+00:00', 'test', '1',
                '2026-08-12T00:00:00+00:00', '2026-08-12T00:00:00+00:00'
            )
            """
        )
        self.conn.execute(
            """
            INSERT INTO claim_evidence(id, claim_id, session_id, created_at)
            VALUES('evidence', 'claim', ?, '2026-08-12T00:00:00+00:00')
            """,
            (session_id,),
        )
        self.conn.commit()
        before = self._snapshot()

        with mock.patch(
            "agentlog.ingest.pipeline.adapter_for",
            return_value=StaticAdapter(self.path, self._old_result()),
        ):
            stats = ingest_harness(self.repo, "codex")

        self.assertEqual(stats.failed, 0)
        self.assertEqual(stats.skipped, 0)
        artifact = self.repo.get_artifact_by_path(str(self.path))
        assert artifact is not None
        self.assertEqual(artifact.parser_version, PARSER_VERSION)
        after = self._snapshot()
        for table in after:
            if table != "artifacts":
                self.assertEqual(after[table], before[table])

    def test_v16_repairs_frozen_byte_append_message_shape(self) -> None:
        artifact_id, session_id = self._seed_old()
        turns = [
            ("developer", "runtime envelope"),
            ("user", "parent task"),
            ("assistant", "initial analysis"),
            ("user", "first follow-up"),
            ("assistant", "first response"),
            ("user", "second follow-up"),
            ("assistant", "second response"),
            ("user", "third follow-up"),
            ("assistant", "third response"),
            ("user", "fourth follow-up"),
            ("assistant", "fourth response"),
            ("user", "inserted missed user turn"),
            ("assistant", "canonical thirteenth"),
            ("user", "canonical fourteenth"),
            ("user", "later missed user turn"),
            ("assistant", "canonical sixteenth"),
            ("assistant", "canonical seventeenth"),
        ]
        canonical = _result(*turns, inherited_message_count=0, marker="v16")
        frozen = _result(
            *(turns[:11] + turns[12:14]),
            inherited_message_count=0,
            marker="v15",
        )
        for table in (
            "exchange_windows",
            "tool_events",
            "skill_exposures",
            "token_usage",
            "messages",
        ):
            self.conn.execute(f"DELETE FROM {table} WHERE session_id = ?", (session_id,))
        self.repo.save_parse_result(
            artifact_id=artifact_id,
            result=frozen,
            append=False,
            transcript_storage=SOURCE_BACKED,
        )
        self.conn.execute(
            "UPDATE artifacts SET parser_version = '15' WHERE id = ?",
            (artifact_id,),
        )
        self.conn.commit()

        frozen_hashes = [
            row["content_hash"]
            for row in self.conn.execute(
                "SELECT content_hash FROM messages WHERE session_id = ? ORDER BY seq",
                (session_id,),
            )
        ]
        self.assertEqual(len(frozen_hashes), 13)
        self.assertEqual(
            frozen_hashes[11:],
            [content_hash_text(text) for _, text in turns[12:14]],
        )

        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM claim_evidence WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM task_clusters WHERE root_session_id = ?",
                (session_id,),
            ).fetchone()[0],
            0,
        )

        _ingest_one(
            self.repo,
            StaticAdapter(self.path, canonical),
            self.path,
            IngestStats(),
        )
        self.conn.commit()

        artifact = self.repo.get_artifact_by_path(str(self.path))
        assert artifact is not None
        self.assertEqual(artifact.parser_version, "16")
        self.assertEqual(
            [
                (row["seq"], row["role"], row["content_hash"])
                for row in self.conn.execute(
                    "SELECT seq, role, content_hash FROM messages "
                    "WHERE session_id = ? ORDER BY seq",
                    (session_id,),
                )
            ],
            [
                (seq, role, content_hash_text(text))
                for seq, (role, text) in enumerate(turns, start=1)
            ],
        )

    def test_parser_upgrade_removes_internal_approval_guardian_activity(self) -> None:
        artifact_id, session_id = self._seed_old()
        self.path.write_text(
            "\n".join(
                json.dumps(item)
                for item in (
                    {
                        "type": "session_meta",
                        "payload": {
                            "id": "worker",
                            "parent_thread_id": "parent",
                            "thread_source": "subagent",
                            "source": {"subagent": {"other": "guardian"}},
                        },
                    },
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "user_message",
                            "message": (
                                "The following is the Codex agent history added "
                                "since your last approval assessment."
                            ),
                        },
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "name": "read_history",
                            "call_id": "guardian-call",
                        },
                    },
                )
            )
            + "\n",
            encoding="utf-8",
        )

        stats = IngestStats()
        _ingest_one(self.repo, CodexAdapter(), self.path, stats)
        self.conn.commit()

        self.assertEqual(stats.failed, 0)
        self.assertEqual(stats.sessions_updated, 1)
        self.assertEqual(
            self.conn.execute(
                "SELECT thread_source FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()["thread_source"],
            INTERNAL_APPROVAL_GUARDIAN_THREAD_SOURCE,
        )
        for table in (
            "messages",
            "tool_events",
            "skill_exposures",
            "token_usage",
            "exchange_windows",
        ):
            with self.subTest(table=table):
                self.assertEqual(
                    self.conn.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE session_id = ?",
                        (session_id,),
                    ).fetchone()[0],
                    0,
                )
        artifact = self.repo.get_artifact_by_path(str(self.path))
        assert artifact is not None
        self.assertEqual(artifact.id, artifact_id)
        self.assertEqual(artifact.parser_version, PARSER_VERSION)
        self.assertEqual(self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_parser_upgrade_rebases_non_null_end_time(self) -> None:
        _, session_id = self._seed_old()
        local_only = _result(
            ("user", "worker-local request"),
            ("assistant", "new worker answer"),
            inherited_message_count=2,
            marker="new",
        )
        local_only.session.ended_at = datetime(
            2026, 8, 12, 4, 30, tzinfo=timezone.utc
        )

        _ingest_one(
            self.repo,
            StaticAdapter(self.path, local_only),
            self.path,
            IngestStats(),
        )

        ended_at = self.conn.execute(
            "SELECT ended_at FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()["ended_at"]
        self.assertEqual(ended_at, "2026-08-12T04:30:00+00:00")

    def test_append_keeps_existing_parent_and_start_metadata(self) -> None:
        artifact_id, session_id = self._seed_old()
        appended = _result(
            ("assistant", "append"),
            inherited_message_count=0,
            marker="append",
        )
        appended.session.parent_session_id = None
        appended.session.started_at = datetime(
            2026, 8, 12, 5, 0, tzinfo=timezone.utc
        )

        self.repo.save_parse_result(
            artifact_id=artifact_id,
            result=appended,
            append=True,
            base_seq=4,
            base_tool_seq=1,
            base_token_seq=1,
            transcript_storage=SOURCE_BACKED,
        )

        session = self.conn.execute(
            "SELECT parent_session_id, started_at, ended_at, repo, model "
            "FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        assert session is not None
        self.assertEqual(session["parent_session_id"], "codex:parent")
        self.assertEqual(session["started_at"], "2026-08-12T03:00:00+00:00")
        self.assertEqual(session["ended_at"], "2026-08-12T03:30:00+00:00")
        self.assertEqual(session["repo"], "old/repo")
        self.assertEqual(session["model"], "old-model")

    def test_rewrite_failure_rolls_back_artifact_and_all_session_rows(self) -> None:
        self._seed_old()
        before = self._snapshot()
        local_only = _result(
            ("user", "worker-local request"),
            ("assistant", "new worker answer"),
            inherited_message_count=2,
            marker="new",
        )
        adapter = StaticAdapter(self.path, local_only)
        replace_windows = self.repo.replace_exchange_windows

        def fail_after_window_replace(session_id, windows):
            replace_windows(session_id, windows)
            raise RuntimeError("injected rewrite failure")

        with (
            mock.patch(
                "agentlog.ingest.pipeline.adapter_for", return_value=adapter
            ),
            mock.patch.object(
                self.repo,
                "replace_exchange_windows",
                side_effect=fail_after_window_replace,
            ),
        ):
            stats = ingest_harness(self.repo, "codex")

        self.assertEqual(stats.failed, 1)
        self.assertIn("injected rewrite failure", stats.warnings[0])
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_rewrite_requires_a_parser_version_change(self) -> None:
        artifact_id, session_id = self._seed_old()
        local_only = _result(
            ("user", "worker-local request"),
            ("assistant", "new worker answer"),
            inherited_message_count=2,
            marker="new",
        )
        before = self._snapshot()

        with self.assertRaisesRegex(
            TranscriptStorageError, "requires a parser version change"
        ):
            self.repo.rewrite_source_backed_parse_result(
                artifact_id=artifact_id,
                result=local_only,
                windows=_windows_from_source_result(session_id, local_only),
                previous_parser_version=PARSER_VERSION,
                current_parser_version=PARSER_VERSION,
            )

        self.assertEqual(self._snapshot(), before)

    def test_task_cluster_blocks_rewrite_without_mutation(self) -> None:
        _, session_id = self._seed_old()
        self.conn.execute(
            "INSERT INTO task_clusters "
            "(id, root_session_id, segment_start_message_id, "
            "segment_end_message_id, cluster_kind) VALUES (?, ?, ?, ?, 'segment')",
            (
                "cluster",
                session_id,
                f"{session_id}:m:1",
                f"{session_id}:m:4",
            ),
        )
        self.conn.commit()
        before = self._snapshot()
        local_only = _result(
            ("user", "worker-local request"),
            ("assistant", "new worker answer"),
            inherited_message_count=2,
            marker="new",
        )

        with mock.patch(
            "agentlog.ingest.pipeline.adapter_for",
            return_value=StaticAdapter(self.path, local_only),
        ):
            stats = ingest_harness(self.repo, "codex")

        self.assertEqual(stats.failed, 0)
        self.assertEqual(stats.skipped, 1)
        self.assertIn("derived task cluster", stats.warnings[0])
        after = self._snapshot()
        for table in (
            "artifacts",
            "messages",
            "tool_events",
            "skill_exposures",
            "token_usage",
            "exchange_windows",
            "session_links",
            "task_clusters",
        ):
            self.assertEqual(after[table], before[table])
        self.assertEqual(
            self.conn.execute(
                "SELECT source_sync_status FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()["source_sync_status"],
            "frozen_parser_upgrade",
        )
        frozen = self._snapshot()
        adapter = StaticAdapter(self.path, local_only)
        with (
            mock.patch(
                "agentlog.ingest.pipeline.adapter_for", return_value=adapter
            ),
            mock.patch.object(
                adapter,
                "parse_path",
                side_effect=AssertionError("frozen parser upgrade re-parsed"),
            ),
        ):
            repeated = ingest_harness(self.repo, "codex")
        self.assertEqual(repeated.failed, 0)
        self.assertEqual(repeated.skipped, 1)
        self.assertEqual(self._snapshot(), frozen)

    def test_root_task_cluster_without_endpoints_blocks_rewrite(self) -> None:
        _, session_id = self._seed_old()
        self.conn.execute(
            "INSERT INTO task_clusters "
            "(id, root_session_id, cluster_kind) VALUES (?, ?, 'root')",
            ("root-cluster", session_id),
        )
        self.conn.commit()
        before = self._snapshot()
        local_only = _result(
            ("user", "worker-local request"),
            ("assistant", "new worker answer"),
            inherited_message_count=2,
            marker="new",
        )

        with mock.patch(
            "agentlog.ingest.pipeline.adapter_for",
            return_value=StaticAdapter(self.path, local_only),
        ):
            stats = ingest_harness(self.repo, "codex")

        self.assertEqual(stats.failed, 0)
        self.assertEqual(stats.skipped, 1)
        self.assertIn("derived task cluster", stats.warnings[0])
        after = self._snapshot()
        for table in (
            "artifacts",
            "messages",
            "tool_events",
            "skill_exposures",
            "token_usage",
            "exchange_windows",
            "session_links",
            "task_clusters",
        ):
            self.assertEqual(after[table], before[table])
        self.assertEqual(
            self.conn.execute(
                "SELECT source_sync_status FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()["source_sync_status"],
            "frozen_parser_upgrade",
        )

    def test_claim_evidence_references_block_rewrite_without_mutation(self) -> None:
        _, session_id = self._seed_old()
        window_id = self.conn.execute(
            "SELECT id FROM exchange_windows WHERE session_id = ? LIMIT 1",
            (session_id,),
        ).fetchone()["id"]
        self.conn.execute(
            """
            INSERT INTO claims (
                id, kind, subject, predicate, value_json, scope_type,
                derivation, observed_at, extractor_name, extractor_version,
                created_at, updated_at
            ) VALUES (
                'claim', 'fact', 'session', 'observed', '{}', 'global',
                'deterministic', '2026-08-12T00:00:00+00:00', 'test', '1',
                '2026-08-12T00:00:00+00:00', '2026-08-12T00:00:00+00:00'
            )
            """
        )
        local_only = _result(
            ("user", "worker-local request"),
            ("assistant", "new worker answer"),
            inherited_message_count=2,
            marker="new",
        )
        references = (
            ("session_id", session_id),
            ("message_id", f"{session_id}:m:999"),
            ("window_id", window_id),
        )

        for column, value in references:
            with self.subTest(column=column):
                self.conn.execute("DELETE FROM claim_evidence")
                self.conn.execute(
                    f"INSERT INTO claim_evidence "
                    f"(id, claim_id, {column}, created_at) "
                    "VALUES ('evidence', 'claim', ?, "
                    "'2026-08-12T00:00:00+00:00')",
                    (value,),
                )
                self.conn.commit()
                before = self._snapshot()

                with self.assertRaisesRegex(TranscriptStorageError, "has claim evidence"):
                    self.repo.rewrite_source_backed_parse_result(
                        artifact_id=self.repo.get_artifact_by_path(str(self.path)).id,
                        result=local_only,
                        windows=_windows_from_source_result(session_id, local_only),
                        previous_parser_version="old-parser",
                        current_parser_version=PARSER_VERSION,
                    )

                self.assertEqual(self._snapshot(), before)

    def test_owner_reset_unblocks_prepared_provenance_for_parser_upgrade(self) -> None:
        _, session_id = self._seed_old()
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
        self.conn.execute(
            """
            INSERT INTO owner_insight_seen_messages(
                session_id, message_id, generation, content_hash, seq, role,
                first_batch_id, status
            ) VALUES (?, ?, 1, 'hash', 1, 'user', 'owner-batch', 'prepared')
            """,
            (session_id, f"{session_id}:m:1"),
        )
        self.conn.execute(
            """
            INSERT INTO owner_insight_session_state(session_id, checked_at)
            VALUES (?, '2026-08-12T00:00:00+00:00')
            """,
            (session_id,),
        )
        self.conn.commit()
        local_only = _result(
            ("user", "worker-local request"),
            ("assistant", "new worker answer"),
            inherited_message_count=2,
            marker="new",
        )

        with mock.patch(
            "agentlog.ingest.pipeline.adapter_for",
            return_value=StaticAdapter(self.path, local_only),
        ):
            stats = ingest_harness(self.repo, "codex")

        self.assertEqual(stats.failed, 0)
        self.assertEqual(stats.skipped, 1)
        self.assertIn("owner insight provenance", stats.warnings[0])
        artifact = self.repo.get_artifact_by_path(str(self.path))
        assert artifact is not None
        self.assertEqual(artifact.parser_version, "old-parser")
        reset_owner_insight_session(self.conn, session_id)
        self.conn.commit()

        with mock.patch(
            "agentlog.ingest.pipeline.adapter_for",
            return_value=StaticAdapter(self.path, local_only),
        ):
            resumed = ingest_harness(self.repo, "codex")

        self.assertEqual(resumed.failed, 0)
        artifact = self.repo.get_artifact_by_path(str(self.path))
        assert artifact is not None
        self.assertEqual(artifact.parser_version, PARSER_VERSION)

    def test_owner_reset_preserves_imported_provenance_guard(self) -> None:
        artifact_id, session_id = self._seed_old()
        self.conn.execute(
            """
            INSERT INTO owner_insight_batches(
                id, content_hash, prompt_hash, prompt_version, redaction_version,
                status, prepared_at, provenance_json
            ) VALUES ('owner-imported', 'content', 'prompt', 'v2', 'v1',
                      'imported', '2026-08-12T00:00:00+00:00', '{}')
            """
        )
        self.conn.execute(
            """
            INSERT INTO owner_insight_batch_messages(
                batch_id, session_id, message_id, seq, content_hash, role,
                source_snapshot_json, source_role
            ) VALUES ('owner-imported', ?, ?, 1, 'hash', 'user', '{}', 'new')
            """,
            (session_id, f"{session_id}:m:1"),
        )
        self.conn.commit()
        reset_owner_insight_session(self.conn, session_id)
        changed = _result(
            ("user", "worker-local request"),
            ("assistant", "new worker answer"),
            inherited_message_count=2,
            marker="new",
        )

        with self.assertRaisesRegex(TranscriptStorageError, "owner insight provenance"):
            self.repo.rewrite_source_backed_parse_result(
                artifact_id=artifact_id,
                result=changed,
                windows=_windows_from_source_result(session_id, changed),
                previous_parser_version="old-parser",
                current_parser_version=PARSER_VERSION,
            )

    def test_checkpoint_blocked_parse_does_not_advance_or_mutate(self) -> None:
        self._seed_old()
        before = self._snapshot()
        blocked = _result(
            ("user", "worker-local request"),
            inherited_message_count=2,
            marker="blocked",
        )
        blocked.extras.update(
            {
                "checkpoint_blocked": True,
                "checkpoint_blocked_reason": "ambiguous fork boundary",
            }
        )

        with mock.patch(
            "agentlog.ingest.pipeline.adapter_for",
            return_value=StaticAdapter(self.path, blocked),
        ):
            stats = ingest_harness(self.repo, "codex")

        self.assertEqual(stats.failed, 1)
        self.assertIn("ambiguous fork boundary", stats.warnings[0])
        self.assertEqual(self._snapshot(), before)
        artifact = self.repo.get_artifact_by_path(str(self.path))
        assert artifact is not None
        self.assertEqual(artifact.parser_version, "old-parser")
        self.assertEqual(self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_ambiguous_codex_fork_keeps_old_checkpoint_and_evidence(self) -> None:
        _, session_id = self._seed_old()
        before = self._snapshot()
        parent_id = "019ff310-46c8-7d82-85d2-a661e3a59008"
        child_id = "worker"

        def line(kind: str, payload: dict[str, object]) -> str:
            return json.dumps(
                {
                    "timestamp": "2026-08-12T03:43:58.981Z",
                    "type": kind,
                    "payload": payload,
                }
            )

        self.path.write_text(
            "\n".join(
                [
                    line(
                        "session_meta",
                        {
                            "id": child_id,
                            "session_id": parent_id,
                            "forked_from_id": parent_id,
                            "parent_thread_id": parent_id,
                            "thread_source": "subagent",
                            "originator": "t3code_desktop",
                            "timestamp": "2026-08-12T03:43:58.894Z",
                        },
                    ),
                    line(
                        "event_msg",
                        {
                            "type": "task_started",
                            "turn_id": "inherited",
                            "started_at": 1,
                        },
                    ),
                    line(
                        "response_item",
                        {
                            "type": "message",
                            "id": "not-in-parent",
                            "role": "user",
                            "content": [
                                {"type": "input_text", "text": "copied context"}
                            ],
                        },
                    ),
                    line("turn_context", {"turn_id": "inherited"}),
                    line(
                        "inter_agent_communication_metadata",
                        {"trigger_turn": True},
                    ),
                    line(
                        "event_msg",
                        {
                            "type": "task_started",
                            "turn_id": "candidate",
                            "started_at": 1786506238,
                        },
                    ),
                    line("turn_context", {"turn_id": "candidate"}),
                    line(
                        "inter_agent_communication_metadata",
                        {"trigger_turn": True},
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        parent = self.root / f"rollout-{parent_id}.jsonl"
        parent.write_text(
            line(
                "response_item",
                {
                    "type": "message",
                    "id": "different-parent-id",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "parent"}],
                },
            )
            + "\n",
            encoding="utf-8",
        )

        with mock.patch(
            "agentlog.ingest.codex.CODEX_SESSIONS_DIR", self.root
        ):
            stats = IngestStats()
            with self.assertRaisesRegex(RuntimeError, "ambiguous full-history fork"):
                _ingest_one(self.repo, CodexAdapter(), self.path, stats)
            self.conn.rollback()

        self.assertEqual(self._snapshot(), before)
        artifact = self.repo.get_artifact_by_path(str(self.path))
        assert artifact is not None
        self.assertEqual(artifact.parser_version, "old-parser")
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) AS c FROM messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()["c"],
            4,
        )

    def test_parser_upgrade_refuses_an_omitted_source_backed_identity(self) -> None:
        artifact_id, _ = self._seed_old()
        omitted = _result(
            ("user", "second session"),
            inherited_message_count=0,
            marker="second",
        )
        omitted.session.external_id = "second"
        self.repo.save_parse_result(
            artifact_id=artifact_id,
            result=omitted,
            append=False,
            transcript_storage=SOURCE_BACKED,
        )
        self.conn.commit()
        before = self._snapshot()
        only_worker = _result(
            ("user", "worker-local request"),
            inherited_message_count=2,
            marker="new",
        )

        with mock.patch(
            "agentlog.ingest.pipeline.adapter_for",
            return_value=StaticAdapter(self.path, only_worker),
        ):
            stats = ingest_harness(self.repo, "codex")

        self.assertEqual(stats.failed, 1)
        self.assertIn("omitted source-backed sessions", stats.warnings[0])
        self.assertIn("codex:second", stats.warnings[0])
        self.assertEqual(self._snapshot(), before)

    def test_append_requiring_full_reparse_does_not_advance_checkpoint(self) -> None:
        artifact_id, _ = self._seed_old()
        size = self.path.stat().st_size
        self.repo.upsert_artifact(
            harness="codex",
            path=str(self.path),
            size=size,
            mtime_ns=self.path.stat().st_mtime_ns,
            content_hash=hash_prefix(self.path, size),
            parsed_offset=size,
            parser_version=PARSER_VERSION,
            transcript_storage=SOURCE_BACKED,
        )
        self.conn.commit()
        before = self._snapshot()
        self.path.write_text("source append", encoding="utf-8")
        result = _result(
            ("user", "worker-local request"),
            inherited_message_count=2,
            marker="append",
        )

        with self.assertRaisesRegex(RuntimeError, "requires an exact full reparse"):
            _ingest_one(
                self.repo,
                AppendBoundaryAdapter(self.path, result),
                self.path,
                IngestStats(),
            )
        self.conn.rollback()

        self.assertEqual(self._snapshot(), before)
        artifact = self.repo.get_artifact_by_path(str(self.path))
        assert artifact is not None
        self.assertEqual(artifact.id, artifact_id)
        self.assertEqual(artifact.parsed_offset, size)
        self.assertEqual(artifact.content_hash, before["artifacts"][0][5])


if __name__ == "__main__":
    unittest.main()
