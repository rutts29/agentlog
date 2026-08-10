from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from agentlog.api.app import create_app
from agentlog.db.repository import Repository
from agentlog.db.schema import connect, init_db
from agentlog.ingest.pipeline import IngestStats, ingest_harness
from agentlog.watch.daemon import WatchDaemon
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
        self.assertEqual(daemon._take_changed("codex"), [])
        self.assertNotIn("codex", daemon._retry_counts)

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

    def test_file_drop_triggers_debounced_ingest(self) -> None:
        path = self.sessions / "rollout-watch-sess-3.jsonl"
        fired = threading.Event()
        original_run = WatchDaemon._run_ingest

        def wrapped(self: WatchDaemon, harness: str) -> None:
            original_run(self, harness)
            fired.set()

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

        conn = connect(self.db)
        try:
            events = list_ingest_events(conn)
            self.assertGreaterEqual(len(events), 1)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
