from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient
from starlette.requests import Request
from watchdog.events import FileMovedEvent

from agentlog.analysis.windows import build_exchange_windows
from agentlog.api.app import create_app
from agentlog.db.repository import SOURCE_BACKED, Repository
from agentlog.db.schema import connect, init_db
from agentlog.ingest.base import TranscriptAdapter
from agentlog.ingest.pipeline import (
    FrozenLegacyTranscriptError,
    IngestStats,
    _ingest_one,
    ingest_harness,
)
from agentlog.normalize.models import (
    Harness,
    NormalizedMessage,
    NormalizedSession,
    ParseResult,
)
from agentlog.watch.daemon import (
    WatchDaemon,
    _HarnessHandler,
    _WriteSerializedConnection,
)
from agentlog.watch.debounce import Debouncer
from agentlog.watch.events import list_ingest_events, record_ingest_event
from agentlog.watch.sources import WatchSource


def _codex_jsonl(session_id: str = "watch-sess-1") -> str:
    lines = [
        {
            "timestamp": "2026-08-09T12:00:00Z",
            "type": "session_meta",
            "payload": {
                "id": session_id,
                "cwd": "/tmp/proj",
                "model": "gpt-5",
            },
        },
        {
            "timestamp": "2026-08-09T12:00:01Z",
            "type": "event_msg",
            "payload": {
                "type": "user_message",
                "message": "hello from watch test",
            },
        },
        {
            "timestamp": "2026-08-09T12:00:02Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "hi"}],
            },
        },
    ]
    return "".join(json.dumps(line) + "\n" for line in lines)


class DebouncerTests(unittest.TestCase):
    def test_rejects_negative_max_wait(self) -> None:
        with self.assertRaisesRegex(ValueError, "max_wait must be non-negative"):
            Debouncer(1.0, lambda _key: None, max_wait=-1.0)

    def test_fires_after_quiet_period(self) -> None:
        fired: list[str] = []
        clock = {"t": 0.0}

        def now() -> float:
            return clock["t"]

        deb = Debouncer(0.2, fired.append, clock=now, sleeper=lambda _: None)
        deb.ping("codex")
        clock["t"] = 0.1
        self.assertEqual(deb.drain_ready(), [])
        clock["t"] = 0.25
        self.assertEqual(deb.drain_ready(), ["codex"])
        self.assertEqual(deb.drain_ready(), [])

    def test_ping_resets_deadline(self) -> None:
        clock = {"t": 0.0}
        deb = Debouncer(
            1.0, lambda _k: None, clock=lambda: clock["t"], sleeper=lambda _: None
        )
        deb.ping("claude")
        clock["t"] = 0.9
        deb.ping("claude")
        clock["t"] = 1.5
        self.assertEqual(deb.drain_ready(), [])
        clock["t"] = 2.0
        self.assertEqual(deb.drain_ready(), ["claude"])

    def test_continuous_pings_fire_at_max_wait(self) -> None:
        clock = {"t": 0.0}
        deb = Debouncer(
            30.0,
            lambda _key: None,
            max_wait=120.0,
            clock=lambda: clock["t"],
            sleeper=lambda _: None,
        )

        for observed_at in (0.0, 20.0, 40.0, 60.0, 80.0, 100.0, 119.0):
            clock["t"] = observed_at
            deb.ping("t3code")
            self.assertEqual(deb.drain_ready(), [])

        clock["t"] = 120.0
        self.assertEqual(deb.drain_ready(), ["t3code"])

    def test_max_wait_window_restarts_after_fire(self) -> None:
        clock = {"t": 0.0}
        deb = Debouncer(
            10.0,
            lambda _key: None,
            max_wait=20.0,
            clock=lambda: clock["t"],
            sleeper=lambda _: None,
        )
        deb.ping("t3code")
        clock["t"] = 20.0
        self.assertEqual(deb.drain_ready(), ["t3code"])

        deb.ping("t3code")
        clock["t"] = 29.9
        self.assertEqual(deb.drain_ready(), [])
        clock["t"] = 30.0
        self.assertEqual(deb.drain_ready(), ["t3code"])

    def test_continuous_changes_trigger_callback_at_max_wait(self) -> None:
        fired = threading.Event()
        stop_pings = threading.Event()
        deb = Debouncer(0.1, lambda _key: fired.set(), max_wait=0.2)

        def keep_changing() -> None:
            while not stop_pings.wait(0.01):
                deb.ping("t3code")

        deb.start()
        deb.ping("t3code")
        pinger = threading.Thread(target=keep_changing)
        pinger.start()
        try:
            self.assertTrue(fired.wait(2.0))
            self.assertTrue(pinger.is_alive())
        finally:
            stop_pings.set()
            pinger.join(timeout=1.0)
            deb.stop()

    def test_background_loop_fires(self) -> None:
        fired: list[str] = []
        event = threading.Event()

        def on_fire(key: str) -> None:
            fired.append(key)
            event.set()

        deb = Debouncer(0.05, on_fire)
        deb.start()
        try:
            deb.ping("cursor")
            self.assertTrue(event.wait(2.0))
            self.assertEqual(fired, ["cursor"])
        finally:
            deb.stop()


class IngestEventTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "e.db"
        self.conn = connect(self.db)
        init_db(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_record_and_list_since(self) -> None:
        a = record_ingest_event(
            self.conn,
            harness="codex",
            sessions_added=1,
            sessions_updated=0,
            messages_added=2,
            ts="2026-08-09T10:00:00+00:00",
        )
        record_ingest_event(
            self.conn,
            harness="claude",
            sessions_added=0,
            sessions_updated=1,
            messages_added=3,
            ts="2026-08-09T11:00:00+00:00",
        )
        self.assertEqual(a.id, 1)
        recent = list_ingest_events(self.conn, since="2026-08-09T10:30:00+00:00")
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0].harness, "claude")
        self.assertEqual(recent[0].messages_added, 3)

    def test_migration_creates_table(self) -> None:
        row = self.conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version = 6"
        ).fetchone()
        self.assertIsNotNone(row)
        cols = {
            r[1]
            for r in self.conn.execute("PRAGMA table_info(ingest_events)").fetchall()
        }
        self.assertTrue(
            {
                "id",
                "ts",
                "harness",
                "sessions_added",
                "sessions_updated",
                "messages_added",
            }.issubset(cols)
        )


class EventsApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "api.db"
        self.conn = connect(self.db)
        init_db(self.conn)
        record_ingest_event(
            self.conn,
            harness="codex",
            sessions_added=2,
            sessions_updated=1,
            messages_added=9,
            ts="2026-08-09T15:00:00+00:00",
        )
        self.client = TestClient(create_app(self.db))

    def tearDown(self) -> None:
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass
        self._tmp.cleanup()

    def test_list_events(self) -> None:
        res = self.client.get("/api/events")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(len(body["items"]), 1)
        self.assertEqual(body["items"][0]["harness"], "codex")
        self.assertEqual(body["items"][0]["sessions_added"], 2)

    def test_list_events_since(self) -> None:
        res = self.client.get(
            "/api/events", params={"since": "2026-08-09T16:00:00+00:00"}
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["items"], [])

    def test_list_events_bad_since(self) -> None:
        res = self.client.get("/api/events", params={"since": "not-a-date"})
        self.assertEqual(res.status_code, 400)

    def test_events_stream_pushes(self) -> None:
        self.conn.close()
        from agentlog.api.events import iter_event_sse

        frames = list(iter_event_sse(self.db, poll_seconds=0, max_cycles=1))
        joined = "".join(frames)
        self.assertIn(": connected", joined)
        self.assertIn("event: ingest", joined)
        self.assertIn('"harness":"codex"', joined)

        # Confirm the SSE route is wired (OpenAPI paths).
        paths = create_app(self.db).openapi()["paths"]
        self.assertIn("/api/events/stream", paths)

    def test_events_stream_honors_last_event_id(self) -> None:
        from agentlog.api.events import events_stream

        app = create_app(self.db)
        request = Request(
            {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": "/api/events/stream",
                "raw_path": b"/api/events/stream",
                "query_string": b"",
                "headers": [(b"last-event-id", b"41")],
                "client": ("127.0.0.1", 123),
                "server": ("127.0.0.1", 8787),
                "app": app,
            }
        )
        with mock.patch(
            "agentlog.api.events.iter_event_sse",
            return_value=iter(()),
        ) as stream:
            events_stream(request, since="2026-08-12T10:00:00+00:00")
        stream.assert_called_once_with(
            self.db,
            since="2026-08-12T10:00:00+00:00",
            after_id=41,
        )


class WatchDaemonIngestTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.sessions = self.root / "sessions"
        self.sessions.mkdir()
        self.db = self.root / "agentlog.db"
        self.conn = connect(self.db)
        init_db(self.conn)
        self.conn.close()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_ingest_cycle_records_event(self) -> None:
        path = self.sessions / "rollout-watch-sess-1.jsonl"
        path.write_text(_codex_jsonl(), encoding="utf-8")
        with mock.patch(
            "agentlog.ingest.codex.CODEX_SESSIONS_DIR", self.sessions
        ):
            daemon = WatchDaemon(
                db_path=self.db,
                sources=[WatchSource("codex", self.sessions, poll=False)],
                debounce_seconds=0.05,
                use_watchdog=False,
            )
            daemon._run_ingest("codex")

        conn = connect(self.db)
        try:
            events = list_ingest_events(conn)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].harness, "codex")
            self.assertGreaterEqual(events[0].sessions_added, 1)
            self.assertGreaterEqual(events[0].messages_added, 1)
            sessions = conn.execute(
                "SELECT COUNT(*) AS c FROM sessions WHERE harness = 'codex'"
            ).fetchone()
            self.assertGreaterEqual(int(sessions["c"]), 1)
        finally:
            conn.close()

    def test_second_ingest_is_noop_via_hash(self) -> None:
        path = self.sessions / "rollout-watch-sess-2.jsonl"
        path.write_text(_codex_jsonl("watch-sess-2"), encoding="utf-8")
        with mock.patch(
            "agentlog.ingest.codex.CODEX_SESSIONS_DIR", self.sessions
        ):
            conn = connect(self.db)
            init_db(conn)
            repo = Repository(conn)
            first = ingest_harness(repo, "codex")
            second = ingest_harness(repo, "codex")
            conn.close()
        self.assertGreaterEqual(first.sessions_added, 1)
        self.assertEqual(second.parsed, 0)
        self.assertEqual(second.appended, 0)
        self.assertGreaterEqual(second.skipped, 1)

    def test_startup_skip_never_derives_or_hydrates_cold_source(self) -> None:
        path = self.sessions / "rollout-cold-startup.jsonl"
        path.write_text(_codex_jsonl("cold-startup"), encoding="utf-8")
        daemon = WatchDaemon(
            db_path=self.db,
            sources=[WatchSource("codex", self.sessions, poll=False)],
            debounce_seconds=0,
            use_watchdog=False,
        )
        with mock.patch(
            "agentlog.ingest.codex.CODEX_SESSIONS_DIR", self.sessions
        ):
            first = daemon._run_ingest("codex")
            self.assertTrue(first)
            conn = connect(self.db)
            try:
                session = conn.execute(
                    "SELECT id FROM sessions WHERE id = 'codex:cold-startup'"
                ).fetchone()
                messages = conn.execute(
                    "SELECT id FROM messages WHERE session_id = 'codex:cold-startup' "
                    "ORDER BY seq"
                ).fetchall()
                self.assertIsNotNone(session)
                self.assertEqual(len(messages), 1)
                response_id = "codex:cold-startup:m:2"
                conn.execute(
                    "INSERT INTO messages (id, session_id, seq, role, text, "
                    "content_hash) VALUES (?, ?, 2, 'assistant', '', 'cold-response')",
                    (response_id, str(session["id"])),
                )
                Repository(conn).replace_exchange_windows(
                    str(session["id"]),
                    [(str(messages[0]["id"]), response_id, "cold")],
                )
                conn.commit()
            finally:
                conn.close()
            with mock.patch.object(daemon, "_run_derive") as derive, mock.patch(
                "agentlog.analysis.extractors.deterministic.CachedSourceTranscriptReader"
            ) as reader:
                worker = daemon._schedule_ingest("codex")
                assert worker is not None
                worker.join(timeout=2.0)
                self.assertFalse(worker.is_alive())

        derive.assert_not_called()
        reader.assert_not_called()

    def test_changed_path_ingest_does_not_discover_other_artifacts(self) -> None:
        changed = self.sessions / "rollout-changed.jsonl"
        untouched = self.sessions / "rollout-untouched.jsonl"
        changed.write_text(_codex_jsonl("changed"), encoding="utf-8")
        untouched.write_text(_codex_jsonl("untouched"), encoding="utf-8")
        daemon = WatchDaemon(
            db_path=self.db,
            sources=[WatchSource("codex", self.sessions, poll=False)],
            debounce_seconds=0,
            use_watchdog=False,
        )
        daemon._note_change("codex", str(changed))
        with mock.patch(
            "agentlog.ingest.codex.CodexAdapter.discover",
            side_effect=AssertionError("event ingest must not rediscover"),
        ):
            self.assertTrue(daemon._run_ingest("codex"))

        conn = connect(self.db)
        try:
            ids = {
                str(row["id"])
                for row in conn.execute(
                    "SELECT id FROM sessions WHERE harness = 'codex'"
                )
            }
        finally:
            conn.close()
        self.assertIn("codex:changed", ids)
        self.assertNotIn("codex:untouched", ids)

    def test_out_of_root_change_is_ignored_without_full_discovery(self) -> None:
        outside = self.root / "outside.jsonl"
        outside.write_text(_codex_jsonl("outside"), encoding="utf-8")
        daemon = WatchDaemon(
            db_path=self.db,
            sources=[WatchSource("codex", self.sessions, poll=False)],
            debounce_seconds=0,
            use_watchdog=False,
        )
        daemon._note_change("codex", str(outside))
        with mock.patch(
            "agentlog.watch.daemon.ingest_harness"
        ) as ingest:
            self.assertFalse(daemon._run_ingest("codex"))
        ingest.assert_not_called()

    def test_noise_inside_root_is_ignored_without_full_discovery(self) -> None:
        noise = self.sessions / ".DS_Store"
        noise.write_bytes(b"noise")
        daemon = WatchDaemon(
            db_path=self.db,
            sources=[WatchSource("codex", self.sessions, poll=False)],
            debounce_seconds=0,
            use_watchdog=False,
        )
        daemon._note_change("codex", str(noise))
        with mock.patch(
            "agentlog.watch.daemon.ingest_harness"
        ) as ingest:
            self.assertFalse(daemon._run_ingest("codex"))
        ingest.assert_not_called()

    def test_deleted_jsonl_event_is_ignored_without_retry(self) -> None:
        deleted = self.sessions / "rollout-deleted.jsonl"
        daemon = WatchDaemon(
            db_path=self.db,
            sources=[WatchSource("codex", self.sessions, poll=False)],
            debounce_seconds=0,
            use_watchdog=False,
        )
        daemon._note_change("codex", str(deleted))
        with mock.patch("agentlog.watch.daemon.ingest_harness") as ingest:
            self.assertFalse(daemon._run_ingest("codex"))
        ingest.assert_not_called()
        self.assertNotIn("codex", daemon._retry_counts)
        self.assertEqual(daemon._take_changed("codex"), [])

    def test_deleted_sqlite_sidecar_is_ignored_when_base_is_missing(self) -> None:
        deleted = self.root / "deleted.sqlite"
        daemon = WatchDaemon(
            db_path=self.db,
            sources=[WatchSource("t3code", self.root, poll=False)],
            debounce_seconds=0,
            use_watchdog=False,
        )
        self.assertEqual(
            daemon._canonical_changed_paths("t3code", [str(deleted) + "-wal"]),
            [],
        )

    def test_sqlite_sidecar_maps_to_configured_database(self) -> None:
        source = self.root / "state.sqlite"
        source.write_bytes(b"sqlite placeholder")
        daemon = WatchDaemon(
            db_path=self.db,
            sources=[WatchSource("t3code", source, poll=False)],
            debounce_seconds=0,
            use_watchdog=False,
        )
        self.assertEqual(
            daemon._canonical_changed_paths(
                "t3code", [str(source) + "-wal"]
            ),
            [str(source.resolve())],
        )

    def test_unknown_sqlite_sidecar_does_not_escape_directory_root(self) -> None:
        source = self.sessions / "state.sqlite"
        sidecar = self.sessions / "missing.sqlite-wal"
        source.write_bytes(b"sqlite placeholder")
        daemon = WatchDaemon(
            db_path=self.db,
            sources=[WatchSource("t3code", self.sessions, poll=False)],
            debounce_seconds=0,
            use_watchdog=False,
        )
        self.assertEqual(
            daemon._canonical_changed_paths("t3code", [str(sidecar)]), []
        )

    def test_pipeline_sqlite_sidecar_stays_scoped_to_main_database(self) -> None:
        source = self.root / "state.sqlite"
        source.write_bytes(b"sqlite placeholder")
        conn = connect(self.db)
        try:
            repo = Repository(conn)
            with mock.patch(
                "agentlog.ingest.pipeline._ingest_one"
            ) as ingest_one:
                stats = ingest_harness(
                    repo,
                    "t3code",
                    changed_paths=[str(source) + "-shm"],
                )
        finally:
            conn.close()
        self.assertEqual(stats.failed, 0)
        ingest_one.assert_called_once()
        self.assertEqual(ingest_one.call_args.args[2], source)

    def test_pipeline_missing_changed_artifacts_do_not_ingest(self) -> None:
        missing_jsonl = self.sessions / "missing.jsonl"
        missing_sqlite = self.root / "missing.sqlite"
        conn = connect(self.db)
        try:
            repo = Repository(conn)
            with mock.patch("agentlog.ingest.pipeline._ingest_one") as ingest_one:
                jsonl_stats = ingest_harness(
                    repo, "codex", changed_paths=[str(missing_jsonl)]
                )
                sqlite_stats = ingest_harness(
                    repo,
                    "t3code",
                    changed_paths=[str(missing_sqlite) + "-wal"],
                )
        finally:
            conn.close()
        self.assertEqual(jsonl_stats.failed, 0)
        self.assertEqual(sqlite_stats.failed, 0)
        ingest_one.assert_not_called()

    def test_cursor_state_change_reconciles_metadata_without_reparsing(self) -> None:
        state = self.root / "state.vscdb"
        composer_id = "cursor-metadata-control"
        old_metadata = {
            "modelConfig": {
                "modelName": "old-model",
                "selectedModels": [
                    {"parameters": [{"id": "effort", "value": "low"}]}
                ],
            },
            "trackedGitRepos": [
                {
                    "branches": [
                        {"branchName": "old-branch", "lastInteractionAt": 1}
                    ]
                }
            ],
        }
        new_metadata = {
            "modelConfig": {
                "modelName": "new-model",
                "selectedModels": [
                    {"parameters": [{"id": "effort", "value": "high"}]}
                ],
            },
            "trackedGitRepos": [
                {
                    "branches": [
                        {"branchName": "new-branch", "lastInteractionAt": 2}
                    ]
                }
            ],
        }
        state_conn = sqlite3.connect(state)
        state_conn.execute(
            "CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value BLOB)"
        )
        state_conn.execute(
            "INSERT INTO cursorDiskKV (key, value) VALUES (?, ?)",
            (f"composerData:{composer_id}", json.dumps(old_metadata)),
        )
        state_conn.commit()
        state_conn.execute(
            "UPDATE cursorDiskKV SET value = ? WHERE key = ?",
            (json.dumps(new_metadata), f"composerData:{composer_id}"),
        )
        state_conn.commit()
        state_conn.close()
        conn = connect(self.db)
        try:
            repo = Repository(conn)
            artifact_id = repo.upsert_artifact(
                harness="cursor",
                path=str(self.sessions / "original.jsonl"),
                size=42,
                mtime_ns=1,
                content_hash="source-checkpoint",
                parsed_offset=42,
                parser_version="15",
                transcript_storage=SOURCE_BACKED,
            )
            session_id = repo.save_parse_result(
                artifact_id=artifact_id,
                result=ParseResult(
                    session=NormalizedSession(
                        harness=Harness.CURSOR,
                        external_id=composer_id,
                        model="old-model",
                        effort="low",
                        effort_source="low",
                        branch="old-branch",
                    ),
                    messages=[
                        NormalizedMessage(
                            seq=1,
                            role="user",
                            text="stored request",
                            content_hash="request-checkpoint",
                        ),
                        NormalizedMessage(
                            seq=2,
                            role="assistant",
                            model="message-model",
                            text="stored transcript text",
                            content_hash="message-checkpoint",
                        )
                    ],
                ),
                append=False,
                transcript_storage=SOURCE_BACKED,
            )
            repo.replace_exchange_windows(
                session_id, build_exchange_windows(repo.list_messages(session_id))
            )
            conn.commit()
        finally:
            conn.close()

        daemon = WatchDaemon(
            db_path=self.db,
            sources=[
                WatchSource("cursor", self.sessions, poll=False),
                WatchSource("cursor", state, poll=True),
            ],
            debounce_seconds=0,
            use_watchdog=False,
        )
        daemon._note_change("cursor", str(state))
        with mock.patch(
            "agentlog.ingest.cursor.CursorAdapter.discover",
            side_effect=AssertionError("state control must not discover transcripts"),
        ), mock.patch(
            "agentlog.ingest.pipeline._ingest_one",
            side_effect=AssertionError("state control must not parse transcripts"),
        ):
            self.assertTrue(daemon._run_ingest("cursor"))

        conn = connect(self.db)
        try:
            session = conn.execute(
                """
                SELECT model, model_canonical, provider, agent_profile,
                       effort, effort_source, branch, artifact_id,
                       transcript_storage
                FROM sessions WHERE id = ?
                """,
                (session_id,),
            ).fetchone()
            message = conn.execute(
                """
                SELECT model, text, content_hash FROM messages
                WHERE session_id = ? AND seq = 2
                """,
                (session_id,),
            ).fetchone()
            artifact = conn.execute(
                "SELECT content_hash, parsed_offset FROM artifacts WHERE id = ?",
                (artifact_id,),
            ).fetchone()
            window_count = conn.execute(
                "SELECT COUNT(*) AS c FROM exchange_windows WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            events = list_ingest_events(conn)
        finally:
            conn.close()
        assert session is not None
        assert message is not None
        assert artifact is not None
        assert window_count is not None
        self.assertEqual(
            (session["model"], session["effort"], session["effort_source"], session["branch"]),
            ("new-model", "high", "high", "new-branch"),
        )
        self.assertEqual(
            (session["model_canonical"], session["provider"], session["agent_profile"]),
            ("new-model", None, None),
        )
        self.assertEqual(session["artifact_id"], artifact_id)
        self.assertEqual(session["transcript_storage"], SOURCE_BACKED)
        self.assertEqual(
            (message["model"], message["text"], message["content_hash"]),
            ("message-model", "", "message-checkpoint"),
        )
        self.assertEqual((artifact["content_hash"], artifact["parsed_offset"]), ("source-checkpoint", 42))
        self.assertEqual(int(window_count["c"]), 1)
        self.assertEqual(events[-1].sessions_updated, 1)

    def test_cursor_configured_state_event_passes_metadata_control_path(self) -> None:
        state = self.root / "state.vscdb"
        state.write_bytes(b"state")
        outside = self.root / "outside" / "state.vscdb"
        outside.parent.mkdir()
        outside.write_bytes(b"outside")
        wal = Path(f"{state}-wal")
        wal.write_bytes(b"wal")
        deleted = self.root / "deleted.vscdb"
        daemon = WatchDaemon(
            db_path=self.db,
            sources=[
                WatchSource("cursor", self.sessions, poll=False),
                WatchSource("cursor", state, poll=True),
            ],
            debounce_seconds=0,
            use_watchdog=False,
        )

        daemon._note_change("cursor", str(state))
        with mock.patch(
            "agentlog.watch.daemon.ingest_harness",
            return_value=IngestStats(skipped=1),
        ) as ingest:
            self.assertTrue(daemon._run_ingest("cursor"))
        self.assertEqual(
            ingest.call_args.kwargs["changed_paths"], []
        )
        self.assertEqual(
            ingest.call_args.kwargs["cursor_metadata_state_db"], state.resolve()
        )

        state.unlink()
        daemon._note_change("cursor", str(outside))
        daemon._note_change("cursor", str(wal))
        daemon._note_change("cursor", str(deleted))
        daemon._note_change("cursor", str(state))
        with mock.patch("agentlog.watch.daemon.ingest_harness") as ingest:
            self.assertFalse(daemon._run_ingest("cursor"))
        ingest.assert_not_called()

    def test_cursor_watch_paths_require_transcript_grammar(self) -> None:
        projects = self.root / "cursor-projects"
        main = projects / "project" / "agent-transcripts" / "root.jsonl"
        subagent = (
            projects
            / "project"
            / "agent-transcripts"
            / "parent"
            / "subagents"
            / "child.jsonl"
        )
        unrelated = projects / "project" / "notes" / "unrelated.jsonl"
        root_noise = projects / "unrelated.jsonl"
        outside = self.root / "outside.jsonl"
        for path in (main, subagent, unrelated, root_noise, outside):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}\n", encoding="utf-8")
        escaped = projects / "project" / "agent-transcripts" / "escaped.jsonl"
        escaped.symlink_to(outside)
        daemon = WatchDaemon(
            db_path=self.db,
            sources=[WatchSource("cursor", projects, poll=False)],
            debounce_seconds=0,
            use_watchdog=False,
        )

        accepted = daemon._canonical_changed_paths(
            "cursor",
            [str(main), str(subagent), str(unrelated), str(root_noise), str(escaped)],
        )

        self.assertEqual(accepted, sorted([str(main.resolve()), str(subagent.resolve())]))

    def test_t3_watch_paths_require_state_database_grammar(self) -> None:
        root = self.root / "t3"
        direct = root / "state.sqlite"
        nested = root / "userdata" / "state.sqlite"
        unrelated = root / "other" / "state.sqlite"
        for path in (direct, nested, unrelated):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"sqlite")
        daemon = WatchDaemon(
            db_path=self.db,
            sources=[WatchSource("t3code", root, poll=False)],
            debounce_seconds=0,
            use_watchdog=False,
        )

        accepted = daemon._canonical_changed_paths(
            "t3code", [str(direct), str(nested), str(unrelated)]
        )

        self.assertEqual(accepted, sorted([str(direct.resolve()), str(nested.resolve())]))

    def test_hermes_watch_paths_require_board_kanban_grammar(self) -> None:
        root = self.root / "hermes"
        state = root / "state.db"
        kanban = root / "kanban.db"
        board = root / "kanban" / "boards" / "board-a" / "kanban.db"
        unrelated = root / "kanban" / "boards" / "board-a" / "other.db"
        nested = root / "kanban" / "boards" / "board-a" / "nested" / "kanban.db"
        for path in (state, kanban, board, unrelated, nested):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"sqlite")
        daemon = WatchDaemon(
            db_path=self.db,
            sources=[WatchSource("hermes", root, poll=False)],
            debounce_seconds=0,
            use_watchdog=False,
        )

        accepted = daemon._canonical_changed_paths(
            "hermes", [str(state), str(kanban), str(board), str(unrelated), str(nested)]
        )

        self.assertEqual(
            accepted,
            sorted([str(state.resolve()), str(kanban.resolve()), str(board.resolve())]),
        )

    def test_moved_watch_event_notes_source_and_destination(self) -> None:
        changes: list[tuple[str, str]] = []
        handler = _HarnessHandler("codex", lambda harness, path: changes.append((harness, path)))
        handler.dispatch(FileMovedEvent("/tmp/old.jsonl", "/tmp/new.jsonl"))

        self.assertEqual(
            changes,
            [("codex", "/tmp/old.jsonl"), ("codex", "/tmp/new.jsonl")],
        )

    def test_poll_loop_does_not_clear_pending_watchdog_change(self) -> None:
        path = self.sessions / "rollout-race.jsonl"
        path.write_text(_codex_jsonl("race"), encoding="utf-8")
        daemon = WatchDaemon(
            db_path=self.db,
            sources=[WatchSource("codex", self.sessions, poll=True)],
            debounce_seconds=0,
            use_watchdog=False,
        )
        daemon._note_change("codex", str(path))
        daemon.request_stop()

        daemon._poll_loop()

        self.assertEqual(daemon._take_changed("codex"), [str(path)])

    def test_directory_poll_detects_hermes_board_wal_only_change(self) -> None:
        boards = self.root / "hermes" / "kanban" / "boards"
        board_db = boards / "board-a" / "kanban.db"
        board_db.parent.mkdir(parents=True)
        board_db.write_bytes(b"sqlite")
        daemon = WatchDaemon(
            db_path=self.db,
            sources=[WatchSource("hermes", boards, poll=True)],
            debounce_seconds=0,
            use_watchdog=False,
        )
        daemon._poll_once(emit_changes=False)
        wal = Path(f"{board_db}-wal")
        wal.write_bytes(b"wal")

        daemon._poll_once()

        self.assertEqual(daemon._take_changed("hermes"), [str(board_db)])

    def test_cursor_jsonl_change_stays_exactly_scoped(self) -> None:
        transcript = self.sessions / "composer.jsonl"
        transcript.write_text("{}\n", encoding="utf-8")
        conn = connect(self.db)
        try:
            repo = Repository(conn)
            with mock.patch(
                "agentlog.ingest.cursor.CursorAdapter.discover",
                side_effect=AssertionError("JSONL event must stay scoped"),
            ), mock.patch(
                "agentlog.ingest.pipeline._ingest_one"
            ) as ingest_one:
                stats = ingest_harness(
                    repo, "cursor", changed_paths=[str(transcript)]
                )
        finally:
            conn.close()
        self.assertEqual(stats.failed, 0)
        ingest_one.assert_called_once()

    def test_startup_ingest_without_changed_paths_discovers_all(self) -> None:
        path = self.sessions / "rollout-startup.jsonl"
        path.write_text(_codex_jsonl("startup"), encoding="utf-8")
        with mock.patch(
            "agentlog.ingest.codex.CODEX_SESSIONS_DIR", self.sessions
        ):
            conn = connect(self.db)
            try:
                repo = Repository(conn)
                with mock.patch.object(
                    repo, "conn", conn
                ), mock.patch(
                    "agentlog.ingest.codex.CodexAdapter.discover",
                    return_value=[path],
                ) as discover:
                    stats = ingest_harness(repo, "codex")
            finally:
                conn.close()
        self.assertEqual(stats.failed, 0)
        discover.assert_called_once_with()

    def test_frozen_legacy_error_is_not_a_retry_failure(self) -> None:
        path = self.sessions / "rollout-frozen.jsonl"
        path.write_text(_codex_jsonl("frozen"), encoding="utf-8")
        conn = connect(self.db)
        try:
            repo = Repository(conn)
            with mock.patch(
                "agentlog.ingest.codex.CodexAdapter.discover",
                return_value=[path],
            ), mock.patch(
                "agentlog.ingest.pipeline._ingest_one",
                side_effect=FrozenLegacyTranscriptError(
                    "legacy session codex:frozen diverged in its canonical source"
                ),
            ):
                stats = ingest_harness(repo, "codex")
        finally:
            conn.close()
        self.assertEqual(stats.failed, 0)
        self.assertEqual(stats.skipped, 1)
        self.assertIn("identity frozen", stats.warnings[0])

    def test_transient_changed_artifact_failure_remains_retryable(self) -> None:
        path = self.sessions / "rollout-transient.jsonl"
        path.write_text(_codex_jsonl("transient"), encoding="utf-8")
        conn = connect(self.db)
        try:
            repo = Repository(conn)
            with mock.patch(
                "agentlog.ingest.pipeline._ingest_one",
                side_effect=OSError("source still being written"),
            ):
                stats = ingest_harness(
                    repo, "codex", changed_paths=[str(path)]
                )
        finally:
            conn.close()
        self.assertEqual(stats.failed, 1)
        self.assertIn("source still being written", stats.warnings[0])

    def test_write_transactions_are_serialized(self) -> None:
        write_lock = threading.Lock()
        first = _WriteSerializedConnection(connect(self.db), write_lock)
        second_attempting = threading.Event()
        second_finished = threading.Event()
        errors: list[BaseException] = []

        first.execute(
            "INSERT INTO ingest_events "
            "(ts, harness, sessions_added, sessions_updated, messages_added) "
            "VALUES ('2026-08-12T00:00:00Z', 'codex', 0, 0, 0)"
        )

        def write_second() -> None:
            second = _WriteSerializedConnection(connect(self.db), write_lock)
            second_attempting.set()
            try:
                second.execute(
                    "INSERT INTO ingest_events "
                    "(ts, harness, sessions_added, sessions_updated, messages_added) "
                    "VALUES ('2026-08-12T00:00:01Z', 't3code', 0, 0, 0)"
                )
                second.commit()
            except BaseException as exc:
                errors.append(exc)
            finally:
                second.close()
                second_finished.set()

        writer = threading.Thread(target=write_second)
        writer.start()
        try:
            self.assertTrue(second_attempting.wait(1.0))
            self.assertFalse(second_finished.wait(0.05))
            first.commit()
            self.assertTrue(second_finished.wait(1.0))
            self.assertEqual(errors, [])
        finally:
            first.close()
            writer.join(timeout=1.0)

    def test_autocommit_writes_release_serialization_lock(self) -> None:
        write_lock = threading.Lock()
        first_raw = connect(self.db)
        first_raw.isolation_level = None
        first = _WriteSerializedConnection(first_raw, write_lock)
        second = _WriteSerializedConnection(connect(self.db), write_lock)
        try:
            first.execute("CREATE TABLE autocommit_probe (id INTEGER)")
            self.assertTrue(write_lock.acquire(blocking=False))
            write_lock.release()

            first.executemany(
                "INSERT INTO autocommit_probe (id) VALUES (?)",
                [(1,), (2,)],
            )
            self.assertTrue(write_lock.acquire(blocking=False))
            write_lock.release()

            second.execute(
                "INSERT INTO ingest_events "
                "(ts, harness, sessions_added, sessions_updated, messages_added) "
                "VALUES ('2026-08-12T00:00:02Z', 't3code', 0, 0, 0)"
            )
            second.commit()
        finally:
            first.close()
            second.close()

    def test_top_level_savepoint_release_releases_serialization_lock(self) -> None:
        write_lock = threading.Lock()
        conn = _WriteSerializedConnection(connect(self.db), write_lock)
        try:
            conn.execute("SAVEPOINT outer_write")
            conn.execute(
                "INSERT INTO ingest_events "
                "(ts, harness, sessions_added, sessions_updated, messages_added) "
                "VALUES ('2026-08-12T00:00:03Z', 'codex', 0, 0, 0)"
            )
            conn.execute("RELEASE outer_write")
            self.assertTrue(write_lock.acquire(blocking=False))
            write_lock.release()
        finally:
            conn.close()

    def test_failed_cycle_schedules_bounded_retry(self) -> None:
        path = self.sessions / "rollout-watch-retry.jsonl"
        path.write_text(_codex_jsonl("watch-retry"), encoding="utf-8")
        failed = IngestStats(failed=1)
        succeeded = IngestStats(skipped=1)
        daemon = WatchDaemon(
            db_path=self.db,
            sources=[WatchSource("codex", self.sessions, poll=False)],
            debounce_seconds=0,
            use_watchdog=False,
        )
        daemon._note_change("codex", str(path))
        with mock.patch(
            "agentlog.watch.daemon.ingest_harness",
            side_effect=[failed, succeeded],
        ) as ingest:
            daemon._run_ingest("codex")
            self.assertEqual(daemon._debouncer.drain_ready(), ["codex"])
            daemon._run_ingest("codex")

        self.assertEqual(ingest.call_count, 2)
        self.assertEqual(
            ingest.call_args_list[0].kwargs["changed_paths"], [str(path.resolve())]
        )
        self.assertEqual(
            ingest.call_args_list[1].kwargs["changed_paths"], [str(path.resolve())]
        )
        self.assertEqual(daemon._take_changed("codex"), [])
        self.assertNotIn("codex", daemon._retry_counts)

    def test_failed_ingest_does_not_schedule_derive(self) -> None:
        daemon = WatchDaemon(
            db_path=self.db,
            sources=[WatchSource("codex", self.sessions, poll=False)],
            debounce_seconds=0,
            use_watchdog=False,
        )
        recovered = threading.Event()

        def fail_then_succeed(_repo: Repository, _harness: str) -> IngestStats:
            if recovered.is_set():
                return IngestStats(changed_window_ids={"changed-window"})
            recovered.set()
            return IngestStats(failed=1)

        with mock.patch(
            "agentlog.watch.daemon.ingest_harness",
            side_effect=fail_then_succeed,
        ), mock.patch.object(daemon, "_schedule_derive") as derive:
            daemon._debouncer.start()
            try:
                daemon._schedule_ingest("codex")
                deadline = time.monotonic() + 2.0
                while derive.call_count < 1 and time.monotonic() < deadline:
                    time.sleep(0.01)
            finally:
                daemon._debouncer.stop()
                daemon._join_background_jobs()

        self.assertEqual(derive.call_count, 1)

    def test_persistent_failure_stops_after_bounded_retries(self) -> None:
        daemon = WatchDaemon(
            db_path=self.db,
            sources=[WatchSource("codex", self.sessions, poll=False)],
            debounce_seconds=0,
            use_watchdog=False,
        )
        exhausted = threading.Event()

        def persistent_failure(_repo: Repository, _harness: str) -> IngestStats:
            if daemon._retry_counts.get("codex", 0) >= 3:
                exhausted.set()
            return IngestStats(failed=1)

        with mock.patch(
            "agentlog.watch.daemon.ingest_harness",
            side_effect=persistent_failure,
        ) as ingest, mock.patch.object(daemon, "_schedule_derive"):
            daemon._debouncer.start()
            try:
                daemon._schedule_ingest("codex")
                self.assertTrue(exhausted.wait(2.0))
                deadline = time.monotonic() + 2.0
                while ingest.call_count < 4 and time.monotonic() < deadline:
                    time.sleep(0.01)
            finally:
                daemon._debouncer.stop()
                daemon._join_background_jobs()

        self.assertEqual(ingest.call_count, 4)
        self.assertEqual(daemon._retry_counts["codex"], 4)

    def test_failed_cycle_without_change_schedules_retry_and_no_failure_event(self) -> None:
        failed = IngestStats(failed=1)
        succeeded = IngestStats(skipped=1)
        daemon = WatchDaemon(
            db_path=self.db,
            sources=[WatchSource("codex", self.sessions, poll=False)],
            debounce_seconds=0,
            use_watchdog=False,
        )
        with mock.patch(
            "agentlog.watch.daemon.ingest_harness",
            side_effect=[failed, succeeded],
        ) as ingest:
            daemon._run_ingest("codex")
            self.assertEqual(daemon._debouncer.drain_ready(), ["codex"])
            daemon._run_ingest("codex")

        self.assertEqual(ingest.call_count, 2)
        self.assertNotIn("codex", daemon._retry_counts)
        conn = connect(self.db)
        try:
            self.assertEqual(len(list_ingest_events(conn)), 1)
        finally:
            conn.close()

    def test_connect_failure_requeues_ingest(self) -> None:
        path = self.sessions / "rollout-connect-retry.jsonl"
        path.write_text(_codex_jsonl("connect-retry"), encoding="utf-8")
        daemon = WatchDaemon(
            db_path=self.db,
            sources=[WatchSource("codex", self.sessions, poll=False)],
            debounce_seconds=0,
            use_watchdog=False,
        )
        daemon._note_change("codex", str(path))
        real_connect = connect
        calls = 0

        def fail_once(db_path: Path):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise sqlite3.OperationalError("transient open failure")
            return real_connect(db_path)

        with mock.patch("agentlog.watch.daemon.connect", side_effect=fail_once), mock.patch(
            "agentlog.watch.daemon.ingest_harness", return_value=IngestStats(skipped=1)
        ):
            self.assertFalse(daemon._run_ingest("codex"))
            self.assertEqual(daemon._debouncer.drain_ready(), ["codex"])
            self.assertTrue(daemon._run_ingest("codex"))

        self.assertNotIn("codex", daemon._retry_counts)

    def test_connect_failure_worker_recovers(self) -> None:
        daemon = WatchDaemon(
            db_path=self.db,
            sources=[WatchSource("codex", self.sessions, poll=False)],
            debounce_seconds=0,
            use_watchdog=False,
        )
        real_connect = connect
        calls = 0
        recovered = threading.Event()

        def fail_once(db_path: Path):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise sqlite3.OperationalError("transient open failure")
            recovered.set()
            return real_connect(db_path)

        with mock.patch("agentlog.watch.daemon.connect", side_effect=fail_once), mock.patch(
            "agentlog.watch.daemon.ingest_harness", return_value=IngestStats(skipped=1)
        ), mock.patch.object(daemon, "_schedule_derive"):
            daemon._debouncer.start()
            try:
                daemon._schedule_ingest("codex")
                self.assertTrue(recovered.wait(2.0))
            finally:
                daemon._debouncer.stop()
                daemon._join_background_jobs()

        self.assertGreaterEqual(calls, 2)
        self.assertNotIn("codex", daemon._retry_counts)

    def test_new_changes_do_not_reset_bounded_retry_count(self) -> None:
        daemon = WatchDaemon(
            db_path=self.db,
            sources=[WatchSource("codex", self.sessions, poll=False)],
            debounce_seconds=0,
            use_watchdog=False,
        )
        daemon._retry_counts["codex"] = 2
        daemon._note_change("codex", "/tmp/another-change.jsonl")
        self.assertEqual(daemon._retry_counts["codex"], 2)

        daemon._retry_counts["codex"] = 4
        daemon._note_change("codex", "/tmp/fresh-cycle.jsonl")
        self.assertNotIn("codex", daemon._retry_counts)

    def test_failed_worker_requeues_and_recovers(self) -> None:
        failed = IngestStats(failed=1)
        succeeded = IngestStats(skipped=1)
        first_failed = threading.Event()
        retry_finished = threading.Event()
        daemon = WatchDaemon(
            db_path=self.db,
            sources=[WatchSource("codex", self.sessions, poll=False)],
            debounce_seconds=0,
            use_watchdog=False,
        )

        def ingest_side_effect(_repo: Repository, _harness: str) -> IngestStats:
            if not first_failed.is_set():
                first_failed.set()
                return failed
            retry_finished.set()
            return succeeded

        with mock.patch(
            "agentlog.watch.daemon.ingest_harness",
            side_effect=ingest_side_effect,
        ) as ingest, mock.patch.object(daemon, "_schedule_derive"):
            daemon._debouncer.start()
            try:
                daemon._schedule_ingest("codex")
                self.assertTrue(retry_finished.wait(1.0))
                self.assertEqual(ingest.call_count, 2)
            finally:
                daemon._debouncer.stop()
                daemon._join_background_jobs()

        self.assertNotIn("codex", daemon._retry_counts)
        conn = connect(self.db)
        try:
            self.assertEqual(len(list_ingest_events(conn)), 1)
        finally:
            conn.close()

    def test_slow_codex_does_not_block_t3_ingest(self) -> None:
        codex_started = threading.Event()
        release_codex = threading.Event()
        daemon = WatchDaemon(
            db_path=self.db,
            sources=[
                WatchSource("codex", self.sessions, poll=False),
                WatchSource("t3code", self.sessions, poll=False),
            ],
            debounce_seconds=0,
            use_watchdog=False,
        )

        def fake_ingest(_repo: Repository, harness: str) -> IngestStats:
            if harness == "codex":
                codex_started.set()
                release_codex.wait(2.0)
            return IngestStats(skipped=1)

        with mock.patch(
            "agentlog.watch.daemon.ingest_harness",
            side_effect=fake_ingest,
        ), mock.patch.object(daemon, "_schedule_derive"):
            codex_worker = daemon._schedule_ingest("codex")
            self.assertIsNotNone(codex_worker)
            self.assertTrue(codex_started.wait(1.0))
            t3_worker = daemon._schedule_ingest("t3code")
            self.assertIsNotNone(t3_worker)
            assert t3_worker is not None
            t3_worker.join(timeout=1.0)
            try:
                self.assertFalse(t3_worker.is_alive())
                assert codex_worker is not None
                self.assertTrue(codex_worker.is_alive())
            finally:
                release_codex.set()
                assert codex_worker is not None
                codex_worker.join(timeout=2.0)

    def test_slow_source_window_preparation_does_not_block_t3_write(self) -> None:
        source = self.root / "slow.jsonl"
        source.write_text("{}\n", encoding="utf-8")
        preparation_started = threading.Event()
        release_preparation = threading.Event()
        t3_finished = threading.Event()
        errors: list[BaseException] = []

        class SlowAdapter(TranscriptAdapter):
            harness = Harness.CODEX

            def __init__(self) -> None:
                self.calls = 0

            def discover(self) -> list[Path]:
                return [source]

            def parse_chunk(
                self, path: Path, data: bytes, *, start_offset: int
            ) -> ParseResult:
                self.calls += 1
                if self.calls == 2:
                    preparation_started.set()
                    release_preparation.wait(2.0)
                return ParseResult(
                    session=NormalizedSession(
                        harness=Harness.CODEX, external_id="slow-window"
                    ),
                    messages=[
                        NormalizedMessage(
                            seq=1,
                            role="user",
                            text="request",
                            content_hash="request-hash",
                        ),
                        NormalizedMessage(
                            seq=2,
                            role="assistant",
                            text="response",
                            content_hash="response-hash",
                        ),
                    ],
                    bytes_consumed=len(data),
                )

        daemon = WatchDaemon(
            db_path=self.db,
            sources=[WatchSource("codex", self.sessions, poll=False)],
            debounce_seconds=0,
            use_watchdog=False,
        )
        adapter = SlowAdapter()

        def prepare_codex() -> None:
            conn: _WriteSerializedConnection | None = None
            try:
                raw = connect(self.db)
                conn = _WriteSerializedConnection(raw, daemon._write_lock)
                _ingest_one(Repository(conn), adapter, source, IngestStats())
                conn.commit()
            except BaseException as exc:
                errors.append(exc)
            finally:
                if conn is not None:
                    conn.close()

        def write_t3() -> None:
            conn: _WriteSerializedConnection | None = None
            try:
                raw = connect(self.db)
                conn = _WriteSerializedConnection(raw, daemon._write_lock)
                conn.execute(
                    "INSERT INTO ingest_events "
                    "(ts, harness, sessions_added, sessions_updated, messages_added) "
                    "VALUES ('2026-08-12T00:00:04Z', 't3code', 0, 0, 0)"
                )
                conn.commit()
                t3_finished.set()
            except BaseException as exc:
                errors.append(exc)
            finally:
                if conn is not None:
                    conn.close()

        codex = threading.Thread(target=prepare_codex)
        t3 = threading.Thread(target=write_t3)
        codex.start()
        self.assertTrue(preparation_started.wait(1.0))
        t3.start()
        try:
            self.assertTrue(t3_finished.wait(1.0))
        finally:
            release_preparation.set()
            codex.join(timeout=2.0)
            t3.join(timeout=2.0)
        self.assertFalse(codex.is_alive())
        self.assertFalse(t3.is_alive())
        self.assertEqual(errors, [])

    def test_source_change_during_window_preparation_prevents_checkpoint(self) -> None:
        from agentlog.ingest import pipeline as ingest_pipeline

        source = self.root / "mutating.jsonl"
        source.write_text("{}\n", encoding="utf-8")

        class MutatingAdapter(TranscriptAdapter):
            harness = Harness.CODEX

            def discover(self) -> list[Path]:
                return [source]

            def parse_chunk(
                self, path: Path, data: bytes, *, start_offset: int
            ) -> ParseResult:
                return ParseResult(
                    session=NormalizedSession(
                        harness=Harness.CODEX, external_id="mutating-window"
                    ),
                    messages=[
                        NormalizedMessage(
                            seq=1,
                            role="user",
                            text="request",
                            content_hash="request-hash",
                        ),
                        NormalizedMessage(
                            seq=2,
                            role="assistant",
                            text="response",
                            content_hash="response-hash",
                        ),
                    ],
                    bytes_consumed=len(data),
                )

        real_builder = ingest_pipeline._windows_from_source_result

        def mutate_after_build(session_id: str, result: ParseResult):
            windows = real_builder(session_id, result)
            source.write_text('{"changed":true}\n', encoding="utf-8")
            return windows

        conn = connect(self.db)
        try:
            with mock.patch(
                "agentlog.ingest.pipeline._windows_from_source_result",
                side_effect=mutate_after_build,
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "source changed before checkpoint"
                ):
                    _ingest_one(
                        Repository(conn), MutatingAdapter(), source, IngestStats()
                    )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) AS c FROM artifacts").fetchone()["c"],
                0,
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) AS c FROM sessions").fetchone()["c"],
                0,
            )
        finally:
            conn.close()

    def test_slow_derive_does_not_block_t3_ingest(self) -> None:
        derive_started = threading.Event()
        release_derive = threading.Event()
        derive_calls = 0
        daemon = WatchDaemon(
            db_path=self.db,
            sources=[
                WatchSource("codex", self.sessions, poll=False),
                WatchSource("t3code", self.sessions, poll=False),
            ],
            debounce_seconds=0,
            use_watchdog=False,
        )

        def slow_first_derive(
            _harnesses: tuple[str, ...], *, window_ids: set[str] | None = None
        ) -> None:
            nonlocal derive_calls
            derive_calls += 1
            if derive_calls == 1:
                derive_started.set()
                release_derive.wait(2.0)

        with mock.patch(
            "agentlog.watch.daemon.ingest_harness",
            side_effect=[
                IngestStats(changed_window_ids={"codex-window"}),
                IngestStats(changed_window_ids={"t3-window"}),
            ],
        ), mock.patch.object(
            daemon, "_run_derive", side_effect=slow_first_derive
        ):
            codex_worker = daemon._schedule_ingest("codex")
            assert codex_worker is not None
            codex_worker.join(timeout=1.0)
            self.assertTrue(derive_started.wait(1.0))

            t3_worker = daemon._schedule_ingest("t3code")
            assert t3_worker is not None
            t3_worker.join(timeout=1.0)
            try:
                self.assertFalse(t3_worker.is_alive())
            finally:
                release_derive.set()
                daemon._join_background_jobs()
        self.assertEqual(derive_calls, 2)

    def test_derive_connect_failure_is_retried(self) -> None:
        daemon = WatchDaemon(
            db_path=self.db,
            sources=[],
            debounce_seconds=0,
            use_watchdog=False,
        )
        real_connect = connect
        calls = 0

        def fail_once(db_path: Path):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise sqlite3.OperationalError("transient open failure")
            return real_connect(db_path)

        with mock.patch("agentlog.watch.daemon.connect", side_effect=fail_once), mock.patch(
            "agentlog.analysis.derive.run_derive"
        ) as derive:
            derive.return_value = mock.Mock(
                skipped=True,
                windows_total=0,
                windows_classified=0,
                windows_updated=0,
                run_id=None,
            )
            worker = daemon._schedule_derive("codex")
            assert worker is not None
            worker.join(timeout=2.0)

        self.assertFalse(worker.is_alive())
        self.assertEqual(calls, 2)
        self.assertEqual(derive.call_count, 1)

    def test_persistent_derive_failure_stops_after_bounded_retries(self) -> None:
        daemon = WatchDaemon(
            db_path=self.db,
            sources=[],
            debounce_seconds=0,
            use_watchdog=False,
        )
        with mock.patch.object(daemon, "_run_derive", return_value=False) as derive:
            worker = daemon._schedule_derive("codex")
            assert worker is not None
            worker.join(timeout=2.0)

        self.assertFalse(worker.is_alive())
        self.assertEqual(derive.call_count, 4)
        self.assertEqual(daemon._derive_pending, set())
        self.assertEqual(daemon._derive_retry_count, 0)

    def test_slow_skill_scan_does_not_hold_write_lock(self) -> None:
        from agentlog.analysis.derive import run_derive
        scan_started = threading.Event()
        release_scan = threading.Event()
        t3_finished = threading.Event()
        errors: list[BaseException] = []
        daemon = WatchDaemon(
            db_path=self.db,
            sources=[],
            debounce_seconds=0,
            use_watchdog=False,
        )

        def slow_discovery(_roots):
            scan_started.set()
            release_scan.wait(2.0)
            return []

        def derive_work() -> None:
            conn: _WriteSerializedConnection | None = None
            try:
                raw = connect(self.db)
                conn = _WriteSerializedConnection(raw, daemon._write_lock)
                run_derive(conn)  # type: ignore[arg-type]
            except BaseException as exc:
                errors.append(exc)
            finally:
                if conn is not None:
                    conn.close()

        def write_t3() -> None:
            conn: _WriteSerializedConnection | None = None
            try:
                raw = connect(self.db)
                conn = _WriteSerializedConnection(raw, daemon._write_lock)
                conn.execute(
                    "INSERT INTO ingest_events "
                    "(ts, harness, sessions_added, sessions_updated, messages_added) "
                    "VALUES ('2026-08-12T00:00:05Z', 't3code', 0, 0, 0)"
                )
                conn.commit()
                t3_finished.set()
            except BaseException as exc:
                errors.append(exc)
            finally:
                if conn is not None:
                    conn.close()

        with mock.patch(
            "agentlog.analysis.skills.discover_skill_files",
            side_effect=slow_discovery,
        ), mock.patch(
            "agentlog.analysis.derive.index_t3_visibility",
            return_value=mock.Mock(to_dict=lambda: {}),
        ):
            derive = threading.Thread(target=derive_work)
            t3 = threading.Thread(target=write_t3)
            derive.start()
            self.assertTrue(scan_started.wait(1.0))
            t3.start()
            try:
                self.assertTrue(t3_finished.wait(1.0))
            finally:
                release_scan.set()
                derive.join(timeout=2.0)
                t3.join(timeout=2.0)
        self.assertFalse(derive.is_alive())
        self.assertFalse(t3.is_alive())
        self.assertEqual(errors, [])

    def test_slow_deterministic_compute_does_not_hold_write_lock(self) -> None:
        from agentlog.analysis.derive import run_derive
        from agentlog.analysis.extractors.deterministic import (
            DeterministicInputChanged,
        )
        from agentlog.analysis.extractors.triage import triage_windows

        seed = connect(self.db)
        repo = Repository(seed)
        artifact_id = repo.upsert_artifact(
            harness="codex",
            path="/tmp/derive-contention.jsonl",
            size=1,
            mtime_ns=1,
            content_hash="derive-contention",
            parsed_offset=1,
            parser_version="test",
        )
        session_id = repo.save_parse_result(
            artifact_id=artifact_id,
            result=ParseResult(
                session=NormalizedSession(
                    harness=Harness.CODEX, external_id="derive-contention"
                ),
                messages=[
                    NormalizedMessage(
                        seq=1, role="user", text="request", content_hash="req"
                    ),
                    NormalizedMessage(
                        seq=2,
                        role="assistant",
                        text="response",
                        content_hash="resp",
                    ),
                ],
            ),
            append=False,
        )
        repo.replace_exchange_windows(
            session_id,
            [
                (
                    f"{session_id}:m:1",
                    f"{session_id}:m:2",
                    "req",
                )
            ],
        )
        seed.commit()
        seed.close()

        compute_started = threading.Event()
        release_compute = threading.Event()
        t3_finished = threading.Event()
        errors: list[BaseException] = []
        daemon = WatchDaemon(
            db_path=self.db,
            sources=[],
            debounce_seconds=0,
            use_watchdog=False,
        )

        def slow_triage(contexts):
            compute_started.set()
            release_compute.wait(2.0)
            return triage_windows(contexts)

        def derive_work() -> None:
            conn: _WriteSerializedConnection | None = None
            try:
                conn = _WriteSerializedConnection(
                    connect(self.db), daemon._write_lock
                )
                run_derive(conn, index_skill_inventory=False)  # type: ignore[arg-type]
            except BaseException as exc:
                errors.append(exc)
            finally:
                if conn is not None:
                    conn.close()

        def write_t3() -> None:
            conn: _WriteSerializedConnection | None = None
            try:
                conn = _WriteSerializedConnection(
                    connect(self.db), daemon._write_lock
                )
                conn.execute(
                    "INSERT INTO ingest_events "
                    "(ts, harness, sessions_added, sessions_updated, messages_added) "
                    "VALUES ('2026-08-12T00:00:06Z', 't3code', 0, 0, 0)"
                )
                conn.commit()
                t3_finished.set()
            except BaseException as exc:
                errors.append(exc)
            finally:
                if conn is not None:
                    conn.close()

        with mock.patch(
            "agentlog.analysis.extractors.deterministic.triage_windows",
            side_effect=slow_triage,
        ):
            derive = threading.Thread(target=derive_work)
            t3 = threading.Thread(target=write_t3)
            derive.start()
            self.assertTrue(compute_started.wait(1.0))
            t3.start()
            try:
                self.assertTrue(t3_finished.wait(1.0))
            finally:
                release_compute.set()
                derive.join(timeout=2.0)
                t3.join(timeout=2.0)
        self.assertFalse(derive.is_alive())
        self.assertFalse(t3.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], DeterministicInputChanged)

    def test_derive_retries_when_full_context_changes_during_compute(self) -> None:
        from agentlog.analysis.derive import derived_freshness, run_derive
        from agentlog.analysis.extractors.deterministic import (
            DeterministicInputChanged,
            iter_window_input_rows,
        )
        from agentlog.analysis.extractors.triage import triage_windows

        seed = connect(self.db)
        repo = Repository(seed)
        artifact_id = repo.upsert_artifact(
            harness="codex",
            path="/tmp/derive-snapshot-race.jsonl",
            size=1,
            mtime_ns=1,
            content_hash="derive-snapshot-race",
            parsed_offset=1,
            parser_version="test",
        )
        session_id = repo.save_parse_result(
            artifact_id=artifact_id,
            result=ParseResult(
                session=NormalizedSession(
                    harness=Harness.CODEX, external_id="derive-snapshot-race"
                ),
                messages=[
                    NormalizedMessage(
                        seq=1, role="user", text="request", content_hash="req"
                    ),
                    NormalizedMessage(
                        seq=2,
                        role="assistant",
                        text="response",
                        content_hash="resp",
                    ),
                ],
            ),
            append=False,
        )
        repo.replace_exchange_windows(
            session_id,
            [(f"{session_id}:m:1", f"{session_id}:m:2", "req")],
        )
        seed.commit()
        seed.close()

        compute_started = threading.Event()
        release_compute = threading.Event()
        first = True

        def pause_first_compute(contexts):
            nonlocal first
            if first:
                first = False
                compute_started.set()
                release_compute.wait(2.0)
            return triage_windows(contexts)

        daemon = WatchDaemon(
            db_path=self.db,
            sources=[],
            debounce_seconds=0,
            use_watchdog=False,
        )

        with mock.patch(
            "agentlog.analysis.extractors.deterministic.triage_windows",
            side_effect=pause_first_compute,
        ):
            worker = daemon._schedule_derive("codex")
            assert worker is not None
            self.assertTrue(compute_started.wait(1.0))
            writer = connect(self.db)
            try:
                writer.execute(
                    "UPDATE messages SET authored_by_agent = 1 WHERE id = ?",
                    (f"{session_id}:m:1",),
                )
                writer.commit()
            finally:
                writer.close()
            release_compute.set()
            worker.join(timeout=2.0)

        self.assertFalse(worker.is_alive())
        self.assertEqual(daemon._derive_retry_count, 0)
        check = connect(self.db)
        try:
            row = check.execute(
                "SELECT window_id, request_kind, route "
                "FROM window_det_classifications"
            ).fetchone()
            run_count = check.execute(
                "SELECT COUNT(*) AS c FROM derivation_runs"
            ).fetchone()["c"]
        finally:
            check.close()
        self.assertEqual(run_count, 1)
        self.assertEqual(row["request_kind"], "worker_brief")
        self.assertEqual(row["route"], "worker_task")

        compute_started.clear()
        release_compute.clear()
        first = True
        errors: list[BaseException] = []

        def derive_with_tool_race() -> None:
            conn = connect(self.db)
            try:
                run_derive(conn, force=True, index_skill_inventory=False)
            except BaseException as exc:
                errors.append(exc)
            finally:
                conn.close()

        with mock.patch(
            "agentlog.analysis.extractors.deterministic.triage_windows",
            side_effect=pause_first_compute,
        ):
            worker = threading.Thread(target=derive_with_tool_race)
            worker.start()
            self.assertTrue(compute_started.wait(1.0))
            writer = connect(self.db)
            try:
                writer.execute(
                    "INSERT INTO tool_events "
                    "(id, session_id, message_id, seq, tool_name, action) "
                    "VALUES (?, ?, ?, 1, 'shell', 'call')",
                    ("tool-race", session_id, f"{session_id}:m:2"),
                )
                writer.commit()
            finally:
                writer.close()
            release_compute.set()
            worker.join(timeout=2.0)

        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], DeterministicInputChanged)

        retry = connect(self.db)
        try:
            run_derive(retry, force=True, index_skill_inventory=False)
            tool_count = retry.execute(
                "SELECT json_extract(features_json, '$.tool_count') AS c "
                "FROM window_det_classifications"
            ).fetchone()["c"]
        finally:
            retry.close()
        self.assertEqual(tool_count, 1)

        sequential = connect(self.db)
        try:
            sequential.execute(
                "INSERT INTO tool_events "
                "(id, session_id, message_id, seq, tool_name, action) "
                "VALUES (?, ?, ?, 2, 'browser', 'call')",
                ("tool-sequential", session_id, f"{session_id}:m:2"),
            )
            sequential.commit()
            self.assertTrue(derived_freshness(sequential)["stale"])
            tool_result = run_derive(
                sequential, index_skill_inventory=False
            )
            self.assertFalse(tool_result.skipped)
            features = json.loads(
                sequential.execute(
                    "SELECT features_json FROM window_det_classifications"
                ).fetchone()["features_json"]
            )
            self.assertEqual(features["tool_count"], 2)

            sequential.execute(
                "INSERT INTO skill_exposures "
                "(id, session_id, message_id, skill_name, exposure_type) "
                "VALUES ('skill-sequential', ?, NULL, 'review', 'matched')",
                (session_id,),
            )
            sequential.commit()
            self.assertTrue(derived_freshness(sequential)["stale"])
            skill_result = run_derive(
                sequential, index_skill_inventory=False
            )
            self.assertFalse(skill_result.skipped)
            features = json.loads(
                sequential.execute(
                    "SELECT features_json FROM window_det_classifications"
                ).fetchone()["features_json"]
            )
            expected_fp = dict(iter_window_input_rows(sequential))[
                row["window_id"]
            ]
            self.assertEqual(features["skill_names"], ["review"])
            self.assertEqual(features["input_fp"], expected_fp)
            self.assertFalse(derived_freshness(sequential)["stale"])

            statements: list[str] = []
            sequential.set_trace_callback(statements.append)
            iter_window_input_rows(sequential)
            sequential.set_trace_callback(None)
            selects = [
                statement
                for statement in statements
                if statement.lstrip().upper().startswith("SELECT")
            ]
            self.assertEqual(len(selects), 4)
        finally:
            sequential.set_trace_callback(None)
            sequential.close()

    def test_same_harness_coalesces_to_one_worker(self) -> None:
        first_started = threading.Event()
        release_first = threading.Event()
        calls = 0
        active = 0
        max_active = 0
        state_lock = threading.Lock()
        daemon = WatchDaemon(
            db_path=self.db,
            sources=[WatchSource("t3code", self.sessions, poll=False)],
            debounce_seconds=0,
            use_watchdog=False,
        )

        def fake_ingest(_repo: Repository, _harness: str) -> IngestStats:
            nonlocal calls, active, max_active
            with state_lock:
                calls += 1
                active += 1
                max_active = max(max_active, active)
                call_number = calls
            try:
                if call_number == 1:
                    first_started.set()
                    release_first.wait(2.0)
                return IngestStats(skipped=1)
            finally:
                with state_lock:
                    active -= 1

        with mock.patch(
            "agentlog.watch.daemon.ingest_harness",
            side_effect=fake_ingest,
        ), mock.patch.object(daemon, "_schedule_derive"):
            first_worker = daemon._schedule_ingest("t3code")
            assert first_worker is not None
            self.assertTrue(first_started.wait(1.0))
            second_worker = daemon._schedule_ingest("t3code")
            self.assertIs(second_worker, first_worker)
            release_first.set()
            first_worker.join(timeout=2.0)

        self.assertFalse(first_worker.is_alive())
        self.assertEqual(calls, 2)
        self.assertEqual(max_active, 1)

    def test_stale_worker_cleanup_cannot_erase_new_worker_reschedule(self) -> None:
        daemon = WatchDaemon(
            db_path=self.db,
            sources=[WatchSource("codex", self.sessions, poll=False)],
            debounce_seconds=0,
            use_watchdog=False,
        )
        old_worker = threading.Thread()
        new_worker = threading.Thread()
        daemon._ingest_threads["codex"] = new_worker
        daemon._ingest_reschedule.add("codex")

        daemon._finish_ingest_worker("codex", old_worker)

        self.assertIs(daemon._ingest_threads["codex"], new_worker)
        self.assertIn("codex", daemon._ingest_reschedule)

    def test_stop_reports_live_background_writer(self) -> None:
        daemon = WatchDaemon(
            db_path=self.db,
            sources=[],
            debounce_seconds=0,
            use_watchdog=False,
        )
        release = threading.Event()
        worker = threading.Thread(
            target=lambda: release.wait(2.0), name="agentlog-test-writer"
        )
        worker.start()
        daemon._ingest_threads["codex"] = worker
        try:
            with self.assertRaisesRegex(RuntimeError, "shutdown incomplete"):
                daemon.stop(worker_timeout=0.01)
        finally:
            release.set()
            worker.join(timeout=1.0)

    def test_file_drop_triggers_debounced_ingest(self) -> None:
        path = self.sessions / "rollout-watch-sess-3.jsonl"
        fired = threading.Event()
        original_run = WatchDaemon._run_ingest

        def wrapped(self: WatchDaemon, harness: str) -> bool:
            completed = original_run(self, harness)
            fired.set()
            return completed

        with mock.patch(
            "agentlog.ingest.codex.CODEX_SESSIONS_DIR", self.sessions
        ), mock.patch.object(WatchDaemon, "_run_ingest", wrapped):
            daemon = WatchDaemon(
                db_path=self.db,
                sources=[WatchSource("codex", self.sessions, poll=False)],
                debounce_seconds=0.1,
                use_watchdog=False,
            )
            daemon._debouncer.start()
            try:
                path.write_text(_codex_jsonl("watch-sess-3"), encoding="utf-8")
                daemon._note_change("codex", str(path))
                self.assertTrue(fired.wait(3.0))
            finally:
                daemon._debouncer.stop()
                daemon._join_background_jobs()

        conn = connect(self.db)
        try:
            events = list_ingest_events(conn)
            self.assertGreaterEqual(len(events), 1)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
