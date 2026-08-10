from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agentlog.ingest.t3code import ModelSelection, T3CodeAdapter, discover_t3code_dbs
from agentlog.ingest.base import TranscriptAdapter, file_stat, hash_prefix, sqlite_fingerprint
from agentlog.db.repository import Repository
from agentlog.db.schema import connect, init_db
from agentlog.ingest.pipeline import IngestStats, _ingest_one
from agentlog.normalize.models import Harness, NormalizedMessage, NormalizedSession, ParseResult

PLAN_THREAD = "11111111-1111-4111-8111-111111111111"
MAIN_THREAD = "22222222-2222-4222-8222-222222222222"
IMPL_THREAD = "33333333-3333-4333-8333-333333333333"
PROJECT_ID = "99999999-9999-4999-8999-999999999999"

SCHEMA = """
CREATE TABLE projection_projects (
  project_id TEXT PRIMARY KEY, title TEXT NOT NULL, workspace_root TEXT NOT NULL,
  scripts_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  deleted_at TEXT, default_model_selection_json TEXT,
  default_thread_env_mode TEXT, favicon_path TEXT);
CREATE TABLE projection_threads (
  thread_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, title TEXT NOT NULL,
  branch TEXT, worktree_path TEXT, latest_turn_id TEXT, created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL, deleted_at TEXT,
  runtime_mode TEXT NOT NULL DEFAULT 'full-access',
  interaction_mode TEXT NOT NULL DEFAULT 'default', model_selection_json TEXT,
  archived_at TEXT, latest_user_message_at TEXT);
CREATE TABLE projection_thread_messages (
  message_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, turn_id TEXT,
  role TEXT NOT NULL, text TEXT NOT NULL, is_streaming INTEGER NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, attachments_json TEXT);
CREATE TABLE projection_thread_activities (
  activity_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, turn_id TEXT,
  tone TEXT NOT NULL, kind TEXT NOT NULL, summary TEXT NOT NULL,
  payload_json TEXT NOT NULL, created_at TEXT NOT NULL, sequence INTEGER);
CREATE TABLE projection_thread_sessions (
  thread_id TEXT PRIMARY KEY, status TEXT NOT NULL, provider_name TEXT,
  provider_session_id TEXT, provider_thread_id TEXT, active_turn_id TEXT,
  last_error TEXT, updated_at TEXT NOT NULL,
  runtime_mode TEXT NOT NULL DEFAULT 'full-access', provider_instance_id TEXT);
CREATE TABLE projection_turns (
  row_id INTEGER PRIMARY KEY AUTOINCREMENT, thread_id TEXT NOT NULL, turn_id TEXT,
  pending_message_id TEXT, assistant_message_id TEXT, state TEXT NOT NULL,
  requested_at TEXT NOT NULL, started_at TEXT, completed_at TEXT,
  checkpoint_turn_count INTEGER, checkpoint_ref TEXT, checkpoint_status TEXT,
  checkpoint_files_json TEXT NOT NULL, source_proposed_plan_thread_id TEXT,
  source_proposed_plan_id TEXT);
CREATE TABLE projection_thread_proposed_plans (
  plan_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, turn_id TEXT,
  plan_markdown TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  implemented_at TEXT, implementation_thread_id TEXT);
CREATE TABLE orchestration_events (
  sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
  aggregate_kind TEXT NOT NULL, stream_id TEXT NOT NULL,
  stream_version INTEGER NOT NULL, event_type TEXT NOT NULL,
  occurred_at TEXT NOT NULL, command_id TEXT, causation_event_id TEXT,
  correlation_id TEXT, actor_kind TEXT NOT NULL, payload_json TEXT NOT NULL,
  metadata_json TEXT NOT NULL);
"""


def _sel(instance: str, model: str, effort: str | None = None) -> str:
    payload: dict[str, object] = {"instanceId": instance, "model": model}
    if effort is not None:
        payload["options"] = {"effort": effort}
    return json.dumps(payload)


def build_fixture(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO projection_projects VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            PROJECT_ID,
            "demo",
            "/Users/dev/demo",
            "[]",
            "2026-08-09T10:00:00.000Z",
            "2026-08-09T12:00:00.000Z",
            None,
            _sel("cursor", "default"),
            None,
            None,
        ),
    )

    def add_thread(thread_id: str, title: str, selection: str) -> None:
        conn.execute(
            """INSERT INTO projection_threads
               (thread_id, project_id, title, branch, worktree_path,
                latest_turn_id, created_at, updated_at, deleted_at,
                runtime_mode, interaction_mode, model_selection_json,
                archived_at, latest_user_message_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                thread_id,
                PROJECT_ID,
                title,
                "feature/demo",
                "/Users/dev/demo",
                None,
                "2026-08-09T10:05:00.000Z",
                "2026-08-09T11:00:00.000Z",
                None,
                "full-access",
                "default",
                selection,
                None,
                None,
            ),
        )

    add_thread(PLAN_THREAD, "plan", _sel("claudeAgent", "claude-opus-5", "high"))
    add_thread(MAIN_THREAD, "main", _sel("cursor", "gpt-5.6-sol", "medium"))
    add_thread(IMPL_THREAD, "impl", _sel("cursor", "gpt-5.6-sol"))

    messages = [
        # (id, thread, turn, role, text, created_at)
        ("m1", MAIN_THREAD, "t1", "user", "add a retry", "2026-08-09T10:06:00.000Z"),
        ("m2", MAIN_THREAD, "t1", "assistant", "on it", "2026-08-09T10:06:30.000Z"),
        ("m3", MAIN_THREAD, "t2", "system", "context refreshed", "2026-08-09T10:07:00.000Z"),
        ("m4", MAIN_THREAD, "t2", "assistant", "", "2026-08-09T10:07:10.000Z"),
        ("m5", MAIN_THREAD, "t3", "user", "orchestrator brief", "2026-08-09T10:08:00.000Z"),
        ("m6", MAIN_THREAD, "t3", "assistant", "done", "2026-08-09T10:09:00.000Z"),
        ("m7", MAIN_THREAD, "t4", "oracle", "weird role", "2026-08-09T10:10:00.000Z"),
        ("p1", PLAN_THREAD, "pt1", "user", "plan this", "2026-08-09T10:05:30.000Z"),
        ("i1", IMPL_THREAD, "it1", "user", "implement the plan", "2026-08-09T10:20:00.000Z"),
        ("i2", IMPL_THREAD, "it1", "assistant", "implementing", "2026-08-09T10:21:00.000Z"),
    ]
    for mid, thread, turn, role, text, created in messages:
        conn.execute(
            """INSERT INTO projection_thread_messages
               (message_id, thread_id, turn_id, role, text, is_streaming,
                created_at, updated_at, attachments_json)
               VALUES (?,?,?,?,?,0,?,?,NULL)""",
            (mid, thread, turn, role, text, created, created),
        )

    turns = [
        (MAIN_THREAD, "t1", "m1", "m2"),
        (MAIN_THREAD, "t2", None, "m4"),
        (MAIN_THREAD, "t3", "m5", "m6"),
        (IMPL_THREAD, "it1", "i1", "i2"),
    ]
    for thread, turn, pending, assistant in turns:
        conn.execute(
            """INSERT INTO projection_turns
               (thread_id, turn_id, pending_message_id, assistant_message_id,
                state, requested_at, checkpoint_files_json,
                source_proposed_plan_thread_id)
               VALUES (?,?,?,?, 'completed', '2026-08-09T10:06:00.000Z', '[]', ?)""",
            (
                thread,
                turn,
                pending,
                assistant,
                PLAN_THREAD if thread == IMPL_THREAD else None,
            ),
        )

    activities = [
        ("a1", MAIN_THREAD, "t1", "tool", "tool-call", "Read file",
         json.dumps({"toolName": "Read"}), "2026-08-09T10:06:10.000Z", 1),
        ("a2", MAIN_THREAD, "t1", "tool", "tool-result", "Read ok",
         json.dumps({"toolName": "Read"}), "2026-08-09T10:06:20.000Z", 2),
        ("a3", MAIN_THREAD, None, "error", "tool-result", "Edit failed",
         json.dumps({"toolName": "Edit"}), "2026-08-09T10:09:30.000Z", 3),
        ("a4", MAIN_THREAD, "t3", "approval", "tool-approval-request", "Allow bash?",
         json.dumps({"command": "bash -lc ls"}), "2026-08-09T10:08:30.000Z", 4),
        ("a5", MAIN_THREAD, "t1", "info", "reasoning", "thinking",
         "{}", "2026-08-09T10:06:05.000Z", 5),
        ("a6", MAIN_THREAD, "t1", "tool", "tool-call", "broken payload",
         "not json", "2026-08-09T10:06:12.000Z", 6),
    ]
    for row in activities:
        conn.execute(
            """INSERT INTO projection_thread_activities
               (activity_id, thread_id, turn_id, tone, kind, summary,
                payload_json, created_at, sequence)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            row,
        )

    conn.execute(
        """INSERT INTO projection_thread_sessions
           (thread_id, status, provider_name, provider_session_id,
            provider_thread_id, active_turn_id, last_error, updated_at,
            runtime_mode, provider_instance_id)
           VALUES (?, 'idle', 'cursor', 'ps1', 'pt1', NULL, NULL,
                   '2026-08-09T11:00:00.000Z', 'full-access', 'cursor')""",
        (MAIN_THREAD,),
    )
    conn.execute(
        """INSERT INTO projection_thread_proposed_plans
           (plan_id, thread_id, turn_id, plan_markdown, created_at, updated_at,
            implemented_at, implementation_thread_id)
           VALUES ('plan-1', ?, 'pt1', '# plan', '2026-08-09T10:10:00.000Z',
                   '2026-08-09T10:10:00.000Z', '2026-08-09T10:19:00.000Z', ?)""",
        (PLAN_THREAD, IMPL_THREAD),
    )

    events = [
        # (event_id, stream, version, type, occurred_at, actor, payload)
        ("e1", MAIN_THREAD, 1, "thread.created", "2026-08-09T10:05:00.000Z",
         "client", {"threadId": MAIN_THREAD, "projectId": PROJECT_ID,
                    "modelSelection": json.loads(_sel("cursor", "gpt-5.6-sol", "medium"))}),
        ("e2", MAIN_THREAD, 2, "thread.message-sent", "2026-08-09T10:06:00.000Z",
         "client", {"threadId": MAIN_THREAD, "messageId": "m1", "role": "user"}),
        ("e3", MAIN_THREAD, 3, "thread.turn-start-requested",
         "2026-08-09T10:06:01.000Z", "client",
         {"threadId": MAIN_THREAD, "messageId": "m1",
          "modelSelection": json.loads(_sel("cursor", "gpt-5.6-sol", "medium"))}),
        ("e4", MAIN_THREAD, 4, "thread.meta-updated", "2026-08-09T10:07:30.000Z",
         "client", {"threadId": MAIN_THREAD,
                    "modelSelection": json.loads(_sel("grok", "grok-4.5", "high"))}),
        ("e5", MAIN_THREAD, 5, "thread.message-sent", "2026-08-09T10:08:00.000Z",
         "server", {"threadId": MAIN_THREAD, "messageId": "m5", "role": "user"}),
        ("e6", MAIN_THREAD, 6, "thread.activity-appended",
         "2026-08-09T10:09:30.000Z", "provider", None),
    ]
    for eid, stream, version, etype, occurred, actor, payload in events:
        conn.execute(
            """INSERT INTO orchestration_events
               (event_id, aggregate_kind, stream_id, stream_version, event_type,
                occurred_at, command_id, causation_event_id, correlation_id,
                actor_kind, payload_json, metadata_json)
               VALUES (?, 'thread', ?, ?, ?, ?, NULL, NULL, NULL, ?, ?, '{}')""",
            (
                eid,
                stream,
                version,
                etype,
                occurred,
                actor,
                json.dumps(payload) if payload is not None else "not-json",
            ),
        )
    conn.commit()
    conn.close()


class T3CodeAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "state.sqlite"
        build_fixture(self.db)
        self.results = {
            r.session.external_id: r
            for r in T3CodeAdapter().parse_path(self.db, b"", start_offset=0)
        }

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_session_identity_is_the_thread_uuid(self) -> None:
        self.assertIn(MAIN_THREAD, self.results)
        session = self.results[MAIN_THREAD].session
        self.assertEqual(session.external_id, MAIN_THREAD)
        self.assertEqual(session.harness, Harness.T3CODE)

    def test_reingest_is_stable(self) -> None:
        again = {
            r.session.external_id: r
            for r in T3CodeAdapter().parse_path(self.db, b"", start_offset=0)
        }
        self.assertEqual(sorted(again), sorted(self.results))
        for key, result in again.items():
            self.assertEqual(
                [m.content_hash for m in result.messages],
                [m.content_hash for m in self.results[key].messages],
            )

    def test_tool_only_turns_are_plumbing(self) -> None:
        by_text = {m.text: m for m in self.results[MAIN_THREAD].messages}
        self.assertTrue(by_text["context refreshed"].is_tool_plumbing)
        empty = [m for m in self.results[MAIN_THREAD].messages if not m.text]
        self.assertTrue(empty and all(m.is_tool_plumbing for m in empty))
        self.assertFalse(by_text["add a retry"].is_tool_plumbing)

    def test_server_authored_user_turn_flagged(self) -> None:
        by_text = {m.text: m for m in self.results[MAIN_THREAD].messages}
        self.assertTrue(by_text["orchestrator brief"].authored_by_agent)
        self.assertFalse(by_text["add a retry"].authored_by_agent)

    def test_plan_implementation_brief_is_agent_authored(self) -> None:
        impl = self.results[IMPL_THREAD]
        self.assertEqual(impl.session.parent_session_id, PLAN_THREAD)
        first_user = next(m for m in impl.messages if m.role == "user")
        self.assertTrue(first_user.authored_by_agent)

    def test_tool_events_are_linked_to_a_message(self) -> None:
        tools = self.results[MAIN_THREAD].tool_events
        self.assertTrue(tools)
        self.assertTrue(all(t.message_seq is not None for t in tools))
        names = {t.tool_name for t in tools}
        self.assertIn("Read", names)
        self.assertIn("Edit", names)
        actions = {t.action for t in tools}
        self.assertIn("call", actions)
        self.assertIn("result", actions)
        self.assertIn("approval", actions)

    def test_turnless_activity_still_links(self) -> None:
        tools = self.results[MAIN_THREAD].tool_events
        edit = next(t for t in tools if t.tool_name == "Edit")
        self.assertIsNotNone(edit.message_seq)
        self.assertIs(edit.success, False)

    def test_info_tone_activity_is_not_a_tool_event(self) -> None:
        summaries = {t.tool_name for t in self.results[MAIN_THREAD].tool_events}
        self.assertNotIn("reasoning", summaries)

    def test_unknown_role_and_bad_payload_are_surfaced(self) -> None:
        warnings = " ".join(self.results[MAIN_THREAD].warnings)
        self.assertIn("oracle", warnings)
        self.assertIn("unparseable", warnings)
        roles = {m.role for m in self.results[MAIN_THREAD].messages}
        self.assertIn("oracle", roles)

    def test_model_identity_split_across_fields(self) -> None:
        session = self.results[MAIN_THREAD].session
        self.assertEqual(session.model, "gpt-5.6-sol")
        self.assertEqual(session.agent_profile, "cursor")
        self.assertEqual(session.provider, "cursor")
        self.assertEqual(session.effort, "medium")

        plan = self.results[PLAN_THREAD].session
        self.assertEqual(plan.model, "claude-opus-5")
        self.assertEqual(plan.agent_profile, "claudeAgent")
        self.assertEqual(plan.provider, "anthropic")
        self.assertEqual(plan.effort, "high")

    def test_live_reasoning_effort_option_list_is_normalized(self) -> None:
        selection = ModelSelection(
            json.dumps(
                {
                    "instanceId": "codex",
                    "model": "gpt-5.6-sol",
                    "options": [
                        {"id": "reasoningEffort", "value": "high"},
                        {"id": "serviceTier", "value": "default"},
                    ],
                }
            )
        )
        self.assertEqual(selection.agent_profile, "codex")
        self.assertEqual(selection.provider, "openai")
        self.assertEqual(selection.model, "gpt-5.6-sol")
        self.assertEqual(selection.effort, "high")

    def test_task_activity_exports_provider_backing_evidence(self) -> None:
        provider_id = "019febdf-eb13-7ee0-8110-26c0bb81a177"
        root_provider_id = "019febd9-95f2-7ea3-82c7-f13290099c71"
        conn = sqlite3.connect(self.db)
        conn.execute(
            "UPDATE projection_thread_sessions SET provider_name = 'codex', "
            "provider_instance_id = 'codex' WHERE thread_id = ?",
            (MAIN_THREAD,),
        )
        conn.execute(
            """INSERT INTO projection_thread_activities
               (activity_id, thread_id, turn_id, tone, kind, summary,
                payload_json, created_at, sequence)
               VALUES ('provider-root', ?, NULL, 'info', 'tool.completed',
                       'provider', ?, '2026-08-09T10:06:10.000Z', 6)""",
            (
                MAIN_THREAD,
                json.dumps({"data": {"threadId": root_provider_id}}),
            ),
        )
        conn.execute(
            """INSERT INTO projection_thread_activities
               (activity_id, thread_id, turn_id, tone, kind, summary,
                payload_json, created_at, sequence)
               VALUES ('provider-task', ?, NULL, 'info', 'task.started',
                       'worker', ?, '2026-08-09T10:06:11.000Z', 7)""",
            (MAIN_THREAD, json.dumps({"taskId": provider_id, "agentKind": "agent"})),
        )
        conn.commit()
        conn.close()
        result = {
            r.session.external_id: r
            for r in T3CodeAdapter().parse_path(self.db, b"", start_offset=0)
        }[MAIN_THREAD]
        self.assertEqual(
            result.extras["session_links"],
            [
                {
                    "link_type": "provider_backing",
                    "target_harness": "codex",
                    "target_external_id": root_provider_id,
                    "link_role": "root",
                    "evidence": {
                        "source": "t3code.projection_thread_activities",
                        "activity_id": "provider-root",
                        "field": "data.threadId",
                    },
                },
                {
                    "link_type": "provider_backing",
                    "target_harness": "codex",
                    "target_external_id": provider_id,
                    "link_role": "worker",
                    "evidence": {
                        "source": "t3code.projection_thread_activities",
                        "activity_id": "provider-task",
                        "field": "taskId",
                    },
                }
            ],
        )

    def test_provider_switch_uses_activity_history_and_ordering(self) -> None:
        codex_root = "019febd9-95f2-7ea3-82c7-f13290099c71"
        codex_worker = "019febdf-eb13-7ee0-8110-26c0bb81a177"
        grok_worker = "019febef-eb13-7ee0-8110-26c0bb81a177"
        conn = sqlite3.connect(self.db)
        conn.execute(
            "UPDATE projection_thread_sessions SET provider_name = 'grok', "
            "provider_instance_id = 'grok' WHERE thread_id = ?",
            (MAIN_THREAD,),
        )
        conn.execute(
            """INSERT INTO orchestration_events
               (event_id, aggregate_kind, stream_id, stream_version,
                event_type, occurred_at, actor_kind, payload_json, metadata_json)
               VALUES ('codex-selection', 'thread', ?, 7, 'thread.turn-started',
                       '2026-08-09T10:06:02.000Z', 'client', ?, '{}')""",
            (MAIN_THREAD, json.dumps({"modelSelection": {"instanceId": "codex"}})),
        )
        conn.executemany(
            """INSERT INTO projection_thread_activities
               (activity_id, thread_id, turn_id, tone, kind, summary,
                payload_json, created_at, sequence)
               VALUES (?, ?, NULL, 'info', 'task.started', 'worker', ?, ?, ?)""",
            [
                (
                    "codex-root-before-fallback",
                    MAIN_THREAD,
                    json.dumps({"data": {"threadId": codex_root}}),
                    "2026-08-09T10:06:10.000Z",
                    20,
                ),
                (
                    "grok-worker-after-fallback",
                    MAIN_THREAD,
                    json.dumps({"taskId": grok_worker}),
                    "2026-08-09T10:08:31.000Z",
                    1,
                ),
                (
                    "codex-worker-before-fallback",
                    MAIN_THREAD,
                    json.dumps({"taskId": codex_worker}),
                    "2026-08-09T10:06:11.000Z",
                    2,
                ),
            ],
        )
        conn.commit()
        conn.close()

        result = {
            r.session.external_id: r
            for r in T3CodeAdapter().parse_path(self.db, b"", start_offset=0)
        }[MAIN_THREAD]
        self.assertCountEqual(
            [link["target_external_id"] for link in result.extras["session_links"]],
            [codex_root, codex_worker],
        )

    def test_provider_switch_reparse_keeps_observed_links(self) -> None:
        provider_id = "019febdf-eb13-7ee0-8110-26c0bb81a177"
        conn = sqlite3.connect(self.db)
        conn.execute(
            "UPDATE projection_thread_sessions SET provider_name = 'codex', "
            "provider_instance_id = 'codex' WHERE thread_id = ?",
            (MAIN_THREAD,),
        )
        conn.execute(
            """INSERT INTO projection_thread_activities
               (activity_id, thread_id, turn_id, tone, kind, summary,
                payload_json, created_at, sequence)
               VALUES ('codex-history', ?, NULL, 'info', 'task.started',
                       'worker', ?, '2026-08-09T10:06:10.000Z', 7)""",
            (MAIN_THREAD, json.dumps({"taskId": provider_id})),
        )
        conn.commit()
        conn.close()

        ledger = connect(Path(self._tmp.name) / "ledger.db")
        init_db(ledger)
        repo = Repository(ledger)
        first = {
            r.session.external_id: r
            for r in T3CodeAdapter().parse_path(self.db, b"", start_offset=0)
        }[MAIN_THREAD]
        artifact = repo.upsert_artifact(
            harness="t3code", path="t3.sqlite", size=1, mtime_ns=1,
            content_hash="t3", parsed_offset=1, parser_version="test",
        )
        repo.save_parse_result(artifact_id=artifact, result=first, append=False)

        conn = sqlite3.connect(self.db)
        conn.execute(
            "UPDATE projection_thread_sessions SET provider_name = 'grok', "
            "provider_instance_id = 'grok' WHERE thread_id = ?",
            (MAIN_THREAD,),
        )
        conn.execute(
            """INSERT INTO projection_thread_activities
               (activity_id, thread_id, turn_id, tone, kind, summary,
                payload_json, created_at, sequence)
               VALUES ('grok-after-fallback', ?, NULL, 'info', 'task.started',
                       'worker', ?, '2026-08-09T10:08:31.000Z', 8)""",
            (MAIN_THREAD, json.dumps({"taskId": "019febef-eb13-7ee0-8110-26c0bb81a177"})),
        )
        conn.commit()
        conn.close()
        second = {
            r.session.external_id: r
            for r in T3CodeAdapter().parse_path(self.db, b"", start_offset=0)
        }[MAIN_THREAD]
        self.assertEqual(second.extras["session_links"], [])
        repo.save_parse_result(artifact_id=artifact, result=second, append=False)
        rows = ledger.execute(
            "SELECT target_external_id FROM session_links "
            "WHERE source_session_id = ? ORDER BY target_external_id",
            (f"t3code:{MAIN_THREAD}",),
        ).fetchall()
        self.assertEqual([row[0] for row in rows], [provider_id])

    def test_non_codex_t3_provider_does_not_emit_provider_backings(self) -> None:
        provider_id = "019febdf-eb13-7ee0-8110-26c0bb81a177"
        conn = sqlite3.connect(self.db)
        try:
            for provider in ("grok", "claude"):
                conn.execute(
                    "UPDATE projection_thread_sessions SET provider_name = ?, "
                    "provider_instance_id = ? WHERE thread_id = ?",
                    (provider, provider, MAIN_THREAD),
                )
                conn.execute(
                    """INSERT INTO projection_thread_activities
                       (activity_id, thread_id, turn_id, tone, kind, summary,
                        payload_json, created_at, sequence)
                       VALUES (?, ?, NULL, 'info', 'task.started',
                               'worker', ?, '2026-08-09T10:06:11.000Z', ?)""",
                    (
                        f"{provider}-task",
                        MAIN_THREAD,
                        json.dumps({"taskId": provider_id}),
                        7 if provider == "grok" else 8,
                    ),
                )
                conn.commit()
                result = {
                    r.session.external_id: r
                    for r in T3CodeAdapter().parse_path(
                        self.db, b"", start_offset=0
                    )
                }[MAIN_THREAD]
                self.assertEqual(result.extras["session_links"], [])
        finally:
            conn.close()

    def test_ambiguous_provider_session_does_not_emit_provider_backings(self) -> None:
        provider_id = "019febdf-eb13-7ee0-8110-26c0bb81a177"
        conn = sqlite3.connect(self.db)
        conn.execute(
            "UPDATE projection_thread_sessions SET provider_name = 'codex', "
            "provider_instance_id = 'grok' WHERE thread_id = ?",
            (MAIN_THREAD,),
        )
        conn.execute(
            "UPDATE projection_threads SET model_selection_json = ? "
            "WHERE thread_id = ?",
            (_sel("codex", "gpt-5.6-sol"), MAIN_THREAD),
        )
        conn.execute(
            """INSERT INTO projection_thread_activities
               (activity_id, thread_id, turn_id, tone, kind, summary,
                payload_json, created_at, sequence)
               VALUES ('ambiguous-provider', ?, NULL, 'info', 'task.started',
                       'worker', ?, '2026-08-09T10:06:11.000Z', 7)""",
            (MAIN_THREAD, json.dumps({"taskId": provider_id})),
        )
        conn.commit()
        conn.close()
        result = {
            r.session.external_id: r
            for r in T3CodeAdapter().parse_path(self.db, b"", start_offset=0)
        }[MAIN_THREAD]
        self.assertEqual(result.extras["session_links"], [])

    def test_current_runtime_overrides_stale_codex_provider_session(self) -> None:
        provider_id = "019febdf-eb13-7ee0-8110-26c0bb81a177"
        conn = sqlite3.connect(self.db)
        conn.execute(
            """CREATE TABLE provider_session_runtime (
                thread_id TEXT PRIMARY KEY,
                provider_name TEXT,
                provider_instance_id TEXT
            )"""
        )
        conn.execute(
            """INSERT INTO provider_session_runtime
               (thread_id, provider_name, provider_instance_id)
               VALUES (?, 'grok', 'grok')""",
            (MAIN_THREAD,),
        )
        conn.execute(
            "UPDATE projection_thread_sessions SET provider_name = 'codex', "
            "provider_instance_id = 'codex' WHERE thread_id = ?",
            (MAIN_THREAD,),
        )
        conn.execute(
            "UPDATE projection_threads SET model_selection_json = ? "
            "WHERE thread_id = ?",
            (_sel("codex", "gpt-5.6-sol"), MAIN_THREAD),
        )
        conn.execute(
            """INSERT INTO projection_thread_activities
               (activity_id, thread_id, turn_id, tone, kind, summary,
                payload_json, created_at, sequence)
               VALUES ('runtime-provider', ?, NULL, 'info', 'task.started',
                       'worker', ?, '2026-08-09T10:06:11.000Z', 7)""",
            (MAIN_THREAD, json.dumps({"taskId": provider_id})),
        )
        conn.commit()
        conn.close()
        result = {
            r.session.external_id: r
            for r in T3CodeAdapter().parse_path(self.db, b"", start_offset=0)
        }[MAIN_THREAD]
        self.assertEqual(result.extras["session_links"], [])

    def test_codex_model_selection_establishes_provider_without_runtime_row(self) -> None:
        provider_id = "019febdf-eb13-7ee0-8110-26c0bb81a177"
        conn = sqlite3.connect(self.db)
        conn.execute(
            "UPDATE projection_threads SET model_selection_json = ? "
            "WHERE thread_id = ?",
            (_sel("codex", "gpt-5.6-sol"), IMPL_THREAD),
        )
        conn.execute(
            """INSERT INTO projection_thread_activities
               (activity_id, thread_id, turn_id, tone, kind, summary,
                payload_json, created_at, sequence)
               VALUES ('selection-provider', ?, NULL, 'info', 'task.started',
                       'worker', ?, '2026-08-09T10:21:11.000Z', 7)""",
            (IMPL_THREAD, json.dumps({"taskId": provider_id})),
        )
        conn.commit()
        conn.close()
        result = {
            r.session.external_id: r
            for r in T3CodeAdapter().parse_path(self.db, b"", start_offset=0)
        }[IMPL_THREAD]
        self.assertEqual(len(result.extras["session_links"]), 1)
        self.assertEqual(
            result.extras["session_links"][0]["target_external_id"], provider_id
        )

    def test_t3_thread_id_is_not_a_codex_provider_backing(self) -> None:
        conn = sqlite3.connect(self.db)
        conn.execute(
            "UPDATE projection_thread_sessions SET provider_name = 'codex', "
            "provider_instance_id = 'codex' WHERE thread_id = ?",
            (MAIN_THREAD,),
        )
        conn.execute(
            """INSERT INTO projection_thread_activities
               (activity_id, thread_id, turn_id, tone, kind, summary,
                payload_json, created_at, sequence)
               VALUES ('t3-task', ?, NULL, 'info', 'task.started',
                       'worker', ?, '2026-08-09T10:06:11.000Z', 7)""",
                (
                    MAIN_THREAD,
                    json.dumps(
                        {
                            "data": {"threadId": PLAN_THREAD},
                            "receiverThreadIds": [PLAN_THREAD],
                        }
                    ),
                ),
        )
        conn.commit()
        conn.close()
        result = {
            r.session.external_id: r
            for r in T3CodeAdapter().parse_path(self.db, b"", start_offset=0)
        }[MAIN_THREAD]
        self.assertEqual(result.extras["session_links"], [])

    def test_worker_evidence_wins_when_root_and_worker_share_id(self) -> None:
        shared_id = "019febdf-eb13-7ee0-8110-26c0bb81a177"
        conn = sqlite3.connect(self.db)
        conn.execute(
            "UPDATE projection_thread_sessions SET provider_name = 'codex', "
            "provider_instance_id = 'codex' WHERE thread_id = ?",
            (MAIN_THREAD,),
        )
        conn.executemany(
            """INSERT INTO projection_thread_activities
               (activity_id, thread_id, turn_id, tone, kind, summary,
                payload_json, created_at, sequence)
               VALUES (?, ?, NULL, 'info', 'task.started', 'worker', ?, ?, ?)""",
            [
                (
                    "root-after-worker",
                    MAIN_THREAD,
                    json.dumps({"data": {"threadId": shared_id}}),
                    "2026-08-09T10:06:12.000Z",
                    8,
                ),
                (
                    "worker-before-root",
                    MAIN_THREAD,
                    json.dumps({"taskId": shared_id}),
                    "2026-08-09T10:06:11.000Z",
                    7,
                ),
            ],
        )
        conn.commit()
        conn.close()
        result = {
            r.session.external_id: r
            for r in T3CodeAdapter().parse_path(self.db, b"", start_offset=0)
        }[MAIN_THREAD]
        links = result.extras["session_links"]
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["target_external_id"], shared_id)
        self.assertEqual(links[0]["link_role"], "worker")
        self.assertEqual(links[0]["evidence"]["field"], "taskId")

    def test_current_non_codex_provider_overrides_stale_codex_selection(self) -> None:
        provider_id = "019febdf-eb13-7ee0-8110-26c0bb81a177"
        conn = sqlite3.connect(self.db)
        conn.execute(
            "UPDATE projection_thread_sessions SET provider_name = 'grok', "
            "provider_instance_id = 'grok' WHERE thread_id = ?",
            (MAIN_THREAD,),
        )
        conn.execute(
            "UPDATE projection_threads SET model_selection_json = ? "
            "WHERE thread_id = ?",
            (_sel("codex", "gpt-5.6-sol"), MAIN_THREAD),
        )
        conn.execute(
            """INSERT INTO projection_thread_activities
               (activity_id, thread_id, turn_id, tone, kind, summary,
                payload_json, created_at, sequence)
               VALUES ('stale-codex', ?, NULL, 'info', 'task.started',
                       'worker', ?, '2026-08-09T10:06:11.000Z', 7)""",
            (MAIN_THREAD, json.dumps({"taskId": provider_id})),
        )
        conn.commit()
        conn.close()
        result = {
            r.session.external_id: r
            for r in T3CodeAdapter().parse_path(self.db, b"", start_offset=0)
        }[MAIN_THREAD]
        self.assertEqual(result.extras["session_links"], [])

    def test_provider_backing_link_resolves_without_merging_rows(self) -> None:
        provider_id = "019febdf-eb13-7ee0-8110-26c0bb81a177"
        conn = connect(Path(self._tmp.name) / "agentlog.db")
        init_db(conn)
        repo = Repository(conn)
        t3_result = {
            r.session.external_id: r
            for r in T3CodeAdapter().parse_path(self.db, b"", start_offset=0)
        }[MAIN_THREAD]
        t3_result.extras["session_links"] = [
            {
                "link_type": "provider_backing",
                "target_harness": "codex",
                "target_external_id": provider_id,
                "evidence": {"source": "test"},
            }
        ]
        t3_artifact = repo.upsert_artifact(
            harness="t3code", path="t3.sqlite", size=1, mtime_ns=1,
            content_hash="t3", parsed_offset=1, parser_version="test",
        )
        repo.save_parse_result(
            artifact_id=t3_artifact, result=t3_result, append=False
        )
        self.assertIsNone(
            conn.execute(
                "SELECT target_session_id FROM session_links"
            ).fetchone()["target_session_id"]
        )
        provider_result = ParseResult(
            session=NormalizedSession(
                harness=Harness.CODEX,
                external_id=provider_id,
                model="gpt-5.6-terra",
            )
        )
        provider_artifact = repo.upsert_artifact(
            harness="codex", path="provider.jsonl", size=1, mtime_ns=1,
            content_hash="provider", parsed_offset=1, parser_version="test",
        )
        repo.save_parse_result(
            artifact_id=provider_artifact,
            result=provider_result,
            append=False,
        )
        row = conn.execute(
            "SELECT source_session_id, target_session_id, target_harness, "
            "target_external_id, link_type FROM session_links"
        ).fetchone()
        self.assertEqual(row["source_session_id"], f"t3code:{MAIN_THREAD}")
        self.assertEqual(row["target_session_id"], f"codex:{provider_id}")
        self.assertEqual(row["target_harness"], "codex")
        self.assertEqual(row["target_external_id"], provider_id)
        self.assertEqual(row["link_type"], "provider_backing")
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 2
        )
        conn.close()

    def test_wal_revision_changes_when_main_database_stat_does_not(self) -> None:
        wal = Path(f"{self.db}-wal")
        main_size, main_revision = file_stat(self.db)
        before_hash = hash_prefix(self.db, main_size)
        wal.write_bytes(b"uncheckpointed state")
        try:
            size, revision = file_stat(self.db)
            after_hash = hash_prefix(self.db, size)
        finally:
            wal.unlink()
        self.assertEqual(size, main_size)
        self.assertNotEqual(revision, main_revision)
        self.assertNotEqual(after_hash, before_hash)

    def test_logical_fast_skip_retries_when_commit_follows_fingerprint(self) -> None:
        source = Path(self._tmp.name) / "logical.sqlite"
        writer = sqlite3.connect(source)
        writer.execute("PRAGMA journal_mode = WAL")
        writer.execute("PRAGMA wal_autocheckpoint = 1000000")
        writer.execute("CREATE TABLE entries (id INTEGER PRIMARY KEY, value TEXT)")
        writer.execute("INSERT INTO entries(value) VALUES ('old')")
        writer.commit()
        ledger = connect(Path(self._tmp.name) / "ledger.db")
        init_db(ledger)
        repo = Repository(ledger)

        class Adapter(TranscriptAdapter):
            harness = Harness.T3CODE
            supports_byte_append = False

            def discover(self) -> list[Path]:
                return []

            def parse_chunk(self, path, data, *, start_offset):
                raise NotImplementedError

            def parse_path(self, path, data, *, start_offset):
                with sqlite3.connect(path) as conn:
                    values = conn.execute(
                        "SELECT value FROM entries ORDER BY id"
                    ).fetchall()
                return [
                    ParseResult(
                        session=NormalizedSession(
                            harness=self.harness, external_id="logical"
                        ),
                        messages=[
                            NormalizedMessage(
                                seq=i,
                                role="user",
                                text=row[0],
                                content_hash=f"h{i}",
                            )
                            for i, row in enumerate(values, 1)
                        ],
                        bytes_consumed=path.stat().st_size,
                    )
                ]

        try:
            first = IngestStats()
            _ingest_one(repo, Adapter(), source, first)
            ledger.commit()
            writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            real_fingerprint = sqlite_fingerprint
            calls = {"n": 0}

            def commit_after_fingerprint(path: Path) -> str:
                result = real_fingerprint(path)
                calls["n"] += 1
                if calls["n"] == 1:
                    writer.execute(
                        "INSERT INTO entries(value) VALUES ('fast-new')"
                    )
                    writer.commit()
                return result

            stats = IngestStats()
            with mock.patch(
                "agentlog.ingest.pipeline.sqlite_fingerprint",
                commit_after_fingerprint,
            ):
                _ingest_one(repo, Adapter(), source, stats)
            ledger.commit()
            values = [
                row[0]
                for row in ledger.execute(
                    "SELECT text FROM messages ORDER BY seq"
                )
            ]
            self.assertEqual(stats.parsed, 1)
            self.assertEqual(values, ["old", "fast-new"])
        finally:
            writer.close()
            ledger.close()

    def test_logical_parse_retries_when_commit_follows_fingerprint(self) -> None:
        source = Path(self._tmp.name) / "logical-parse.sqlite"
        writer = sqlite3.connect(source)
        writer.execute("PRAGMA journal_mode = WAL")
        writer.execute("PRAGMA wal_autocheckpoint = 1000000")
        writer.execute("CREATE TABLE entries (id INTEGER PRIMARY KEY, value TEXT)")
        writer.execute("INSERT INTO entries(value) VALUES ('old')")
        writer.commit()
        ledger = connect(Path(self._tmp.name) / "parse-ledger.db")
        init_db(ledger)
        repo = Repository(ledger)

        class Adapter(TranscriptAdapter):
            harness = Harness.T3CODE
            supports_byte_append = False

            def discover(self) -> list[Path]:
                return []

            def parse_chunk(self, path, data, *, start_offset):
                raise NotImplementedError

            def parse_path(self, path, data, *, start_offset):
                with sqlite3.connect(path) as conn:
                    values = conn.execute(
                        "SELECT value FROM entries ORDER BY id"
                    ).fetchall()
                return [
                    ParseResult(
                        session=NormalizedSession(
                            harness=self.harness, external_id="logical"
                        ),
                        messages=[
                            NormalizedMessage(
                                seq=i,
                                role="user",
                                text=row[0],
                                content_hash=f"h{i}",
                            )
                            for i, row in enumerate(values, 1)
                        ],
                        bytes_consumed=path.stat().st_size,
                    )
                ]

        try:
            first = IngestStats()
            _ingest_one(repo, Adapter(), source, first)
            ledger.commit()
            writer.execute("INSERT INTO entries(value) VALUES ('parse-new')")
            writer.commit()
            real_fingerprint = sqlite_fingerprint
            calls = {"n": 0}

            def commit_after_parse_fingerprint(path: Path) -> str:
                result = real_fingerprint(path)
                calls["n"] += 1
                if calls["n"] == 2:
                    writer.execute(
                        "INSERT INTO entries(value) VALUES ('parse-race')"
                    )
                    writer.commit()
                return result

            stats = IngestStats()
            with mock.patch(
                "agentlog.ingest.pipeline.sqlite_fingerprint",
                commit_after_parse_fingerprint,
            ):
                _ingest_one(repo, Adapter(), source, stats)
            ledger.commit()
            values = [
                row[0]
                for row in ledger.execute(
                    "SELECT text FROM messages ORDER BY seq"
                )
            ]
            self.assertEqual(stats.parsed, 1)
            self.assertEqual(values, ["old", "parse-new", "parse-race"])
        finally:
            writer.close()
            ledger.close()

    def test_per_message_model_follows_mid_session_switch(self) -> None:
        by_text = {m.text: m for m in self.results[MAIN_THREAD].messages}
        self.assertEqual(by_text["on it"].model, "gpt-5.6-sol")
        self.assertEqual(by_text["on it"].agent_profile, "cursor")
        # thread.meta-updated switched the selection before m6.
        self.assertEqual(by_text["done"].model, "grok-4.5")
        self.assertEqual(by_text["done"].agent_profile, "grok")
        self.assertEqual(by_text["done"].provider, "xai")
        self.assertEqual(by_text["done"].effort, "high")

    def test_user_rows_carry_no_model(self) -> None:
        for msg in self.results[MAIN_THREAD].messages:
            if msg.role == "user":
                self.assertIsNone(msg.model)

    def test_session_context_fields(self) -> None:
        session = self.results[MAIN_THREAD].session
        self.assertEqual(session.repo, "/Users/dev/demo")
        self.assertEqual(session.branch, "feature/demo")
        self.assertIsNotNone(session.started_at)
        self.assertIsNotNone(session.ended_at)


class T3CodeDiscoveryTests(unittest.TestCase):
    def test_missing_install_is_not_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            import agentlog.config as cfg

            original = cfg.T3CODE_HOME_CANDIDATES
            cfg.T3CODE_HOME_CANDIDATES = (Path(tmp) / "nope",)
            try:
                self.assertEqual(discover_t3code_dbs(), [])
            finally:
                cfg.T3CODE_HOME_CANDIDATES = original

    def test_globs_multiple_candidate_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            import agentlog.config as cfg

            root_a = Path(tmp) / ".t3" / "userdata"
            root_b = Path(tmp) / ".t3code" / "userdata"
            root_a.mkdir(parents=True)
            root_b.mkdir(parents=True)
            build_fixture(root_a / "state.sqlite")
            build_fixture(root_b / "state.sqlite")
            original = cfg.T3CODE_HOME_CANDIDATES
            cfg.T3CODE_HOME_CANDIDATES = (
                Path(tmp) / ".t3",
                Path(tmp) / ".t3code",
                Path(tmp) / "absent",
            )
            try:
                found = discover_t3code_dbs()
            finally:
                cfg.T3CODE_HOME_CANDIDATES = original
            self.assertEqual(len(found), 2)

    def test_watch_sources_cover_the_state_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            import agentlog.config as cfg
            from agentlog.watch.sources import t3code_sources

            userdata = Path(tmp) / ".t3" / "userdata"
            userdata.mkdir(parents=True)
            build_fixture(userdata / "state.sqlite")
            original = cfg.T3CODE_HOME_CANDIDATES
            cfg.T3CODE_HOME_CANDIDATES = (Path(tmp) / ".t3",)
            try:
                sources = t3code_sources()
            finally:
                cfg.T3CODE_HOME_CANDIDATES = original
            paths = {s.path for s in sources}
            self.assertIn(userdata / "state.sqlite", paths)
            self.assertIn(userdata, paths)
            self.assertTrue(all(s.harness == "t3code" for s in sources))
            self.assertTrue(all(s.poll for s in sources))

    def test_watch_sources_without_install_are_benign(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            import agentlog.config as cfg
            from agentlog.watch.sources import existing_watch_roots, t3code_sources

            original = cfg.T3CODE_HOME_CANDIDATES
            cfg.T3CODE_HOME_CANDIDATES = (Path(tmp) / "absent",)
            try:
                sources = t3code_sources()
                self.assertTrue(sources)
                self.assertEqual(existing_watch_roots(sources), [])
            finally:
                cfg.T3CODE_HOME_CANDIDATES = original

    def test_unrelated_sqlite_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite"
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE something (id INTEGER)")
            conn.commit()
            conn.close()
            self.assertEqual(
                T3CodeAdapter().parse_path(path, b"", start_offset=0), []
            )


if __name__ == "__main__":
    unittest.main()
