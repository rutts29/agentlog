from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from agentlog.api.app import create_app
from agentlog.api.events import _bootstrap_ingest_events, iter_event_sse
from agentlog.api.live import _source_snapshot_status, live_payload
from agentlog.db.schema import connect, init_db
from agentlog.watch.events import list_ingest_events, record_ingest_event
from agentlog.watch.presence import (
    PresenceMap,
    atomic_write_json,
    enrich_presence_sessions,
    external_id_for_path,
    peek_transcript_state,
    read_presence_file,
)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(r) + "\n" for r in rows),
        encoding="utf-8",
    )


class TailPeekTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_cursor_tool_running(self) -> None:
        path = self.root / "sess.jsonl"
        _write_jsonl(
            path,
            [
                {"role": "user", "message": {"content": [{"type": "text", "text": "hi"}]}},
                {
                    "role": "assistant",
                    "message": {
                        "content": [
                            {"type": "text", "text": "ok"},
                            {"type": "tool_use", "name": "Shell"},
                        ]
                    },
                },
            ],
        )
        self.assertEqual(peek_transcript_state(path), "tool_running")

    def test_cursor_waiting_after_user(self) -> None:
        path = self.root / "wait.jsonl"
        _write_jsonl(
            path,
            [
                {"role": "assistant", "message": {"content": [{"type": "text", "text": "a"}]}},
                {"type": "turn_ended"},
                {"role": "user", "message": {"content": [{"type": "text", "text": "next"}]}},
            ],
        )
        self.assertEqual(peek_transcript_state(path), "waiting")

    def test_codex_streaming(self) -> None:
        path = self.root / "rollout-x.jsonl"
        _write_jsonl(
            path,
            [
                {
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": "go"},
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "hi"}],
                    },
                },
            ],
        )
        self.assertEqual(peek_transcript_state(path), "streaming")

    def test_malformed_tail_never_raises(self) -> None:
        path = self.root / "bad.jsonl"
        path.write_text(
            '{"role":"user","message":{"content":"ok"}}\n'
            "{not json\n"
            '{"role":"assistant","message":{"content":[{"type":"text","text":"x"}]}}\n',
            encoding="utf-8",
        )
        self.assertEqual(peek_transcript_state(path), "streaming")

    def test_empty_and_missing(self) -> None:
        empty = self.root / "empty.jsonl"
        empty.write_text("", encoding="utf-8")
        self.assertEqual(peek_transcript_state(empty), "unknown")
        self.assertEqual(peek_transcript_state(self.root / "nope.jsonl"), "unknown")


class PresenceMapTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.clock = {"t": 1_000.0}
        self.state = self.root / "presence.json"
        self.pmap = PresenceMap(
            active_seconds=90.0,
            state_path=self.state,
            clock=lambda: self.clock["t"],
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_active_to_idle_expiry(self) -> None:
        path = self.root / "agent.jsonl"
        _write_jsonl(
            path,
            [{"role": "assistant", "message": {"content": [{"type": "text", "text": "x"}]}}],
        )
        with mock.patch(
            "agentlog.watch.presence.cursor_adapter.external_id_from_path",
            return_value="proj/abc",
        ):
            entry = self.pmap.note_activity("cursor", path)
        self.assertIsNotNone(entry)
        self.assertEqual(len(self.pmap.active(now=self.clock["t"])), 1)

        self.clock["t"] = 1_000.0 + 89.0
        self.assertEqual(len(self.pmap.active()), 1)

        self.clock["t"] = 1_000.0 + 91.0
        removed = self.pmap.expire()
        self.assertEqual(removed, ["cursor:proj/abc"])
        self.assertEqual(self.pmap.active(), [])

    def test_each_daemon_instance_has_a_distinct_epoch(self) -> None:
        first = self.pmap.snapshot()["epoch"]
        second = PresenceMap(
            active_seconds=90.0,
            state_path=self.state,
            clock=lambda: self.clock["t"],
        ).snapshot()["epoch"]
        self.assertIsInstance(first, str)
        self.assertNotEqual(first, second)

    def test_pending_ingest_then_linked(self) -> None:
        db = self.root / "agentlog.db"
        conn = connect(db)
        init_db(conn)
        conn.close()

        path = self.root / "rollout-2026-08-09T12-00-00-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl"
        _write_jsonl(
            path,
            [
                {
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": "hello world"},
                }
            ],
        )
        pmap = PresenceMap(
            active_seconds=90.0,
            state_path=self.state,
            db_path=db,
            clock=lambda: self.clock["t"],
        )
        entry = pmap.note_activity("codex", path)
        assert entry is not None
        self.assertTrue(entry.pending_ingest)
        self.assertIsNone(entry.session_id)
        self.assertEqual(entry.title, "hello world")

        # Simulate a prior ingest landing in the DB.
        ext = entry.external_id
        sid = f"codex:{ext}"
        conn = connect(db)
        init_db(conn)
        conn.execute(
            """
            INSERT INTO sessions (
                id, harness, external_id, repo, cwd, model
            ) VALUES (?, 'codex', ?, '/tmp/proj', '/tmp/proj', 'gpt-5')
            """,
            (sid, ext),
        )
        conn.execute(
            """
            INSERT INTO messages (
                id, session_id, seq, role, text, content_hash, is_tool_plumbing
            ) VALUES (?, ?, 1, 'user', 'saved title', 'h1', 0)
            """,
            (f"{sid}:m:1", sid),
        )
        conn.commit()
        conn.close()

        entry2 = pmap.note_activity("codex", path)
        assert entry2 is not None
        self.assertFalse(entry2.pending_ingest)
        self.assertEqual(entry2.session_id, sid)
        self.assertEqual(entry2.title, "saved title")
        self.assertEqual(entry2.repo, "/tmp/proj")

    def test_state_file_atomic_replace(self) -> None:
        path = self.root / "agent.jsonl"
        _write_jsonl(
            path,
            [{"role": "user", "message": {"content": "x"}}],
        )
        with mock.patch(
            "agentlog.watch.presence.cursor_adapter.external_id_from_path",
            return_value="p/1",
        ):
            self.pmap.note_activity("cursor", path)
        self.assertTrue(self.state.is_file())
        data = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(len(data["sessions"]), 1)
        self.assertFalse(any(self.root.glob(".presence.json.*.tmp")))

        # Concurrent readers never see partial JSON.
        errors: list[BaseException] = []
        stop = threading.Event()

        def reader() -> None:
            while not stop.is_set():
                try:
                    raw = read_presence_file(self.state)
                    self.assertIsInstance(raw.get("sessions"), list)
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)
                    return

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        try:
            for i in range(40):
                self.clock["t"] = 1_000.0 + i
                with mock.patch(
                    "agentlog.watch.presence.cursor_adapter.external_id_from_path",
                    return_value=f"p/{i}",
                ):
                    self.pmap.note_activity("cursor", path)
                atomic_write_json(self.state, self.pmap.snapshot())
        finally:
            stop.set()
            t.join(timeout=2)
        self.assertEqual(errors, [])

    def test_enrich_pending_flag(self) -> None:
        db = self.root / "e.db"
        conn = connect(db)
        init_db(conn)
        conn.close()
        sessions = [
            {
                "harness": "cursor",
                "external_id": "proj/new",
                "session_id": None,
                "pending_ingest": True,
                "title": None,
                "repo": None,
            }
        ]
        out = enrich_presence_sessions(db, sessions)
        self.assertTrue(out[0]["pending_ingest"])
        self.assertIsNone(out[0]["session_id"])


class PresenceApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.db = self.root / "agentlog.db"
        conn = connect(self.db)
        init_db(conn)
        conn.close()
        self.presence = self.root / "presence.json"
        # Fresh clock so age_seconds stays inside the active window.
        self.now = time.time()
        self.ts = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(self.now))
        atomic_write_json(
            self.presence,
            {
                "ts": self.ts,
                "epoch": "daemon-a",
                "generation": 1,
                "active_seconds": 90.0,
                "sessions": [
                    {
                        "harness": "cursor",
                        "external_id": "proj/live-1",
                        "session_id": None,
                        "source_path": str(self.root / "live-1.jsonl"),
                        "state": "streaming",
                        "last_activity_at": self.ts,
                        "age_seconds": 1.0,
                        "pending_ingest": True,
                        "title": "pending title",
                        "repo": None,
                    }
                ],
            },
        )
        (self.root / "live-1.jsonl").write_text(
            json.dumps(
                {
                    "role": "assistant",
                    "message": {
                        "content": [{"type": "text", "text": "streaming"}]
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        # /api/live also scans the real machine; isolate tests to the fixture.
        self._scan_patch = mock.patch(
            "agentlog.api.live.SCAN_CACHE.rows", return_value=[]
        )
        self._scan_patch.start()
        self.client = TestClient(create_app(self.db))

    def tearDown(self) -> None:
        self._scan_patch.stop()
        self._tmp.cleanup()

    def test_api_live(self) -> None:
        res = self.client.get("/api/live")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(len(body["sessions"]), 1)
        self.assertTrue(body["sessions"][0]["pending_ingest"])
        self.assertEqual(body["sessions"][0]["state"], "streaming")
        self.assertEqual(body["epoch"], "daemon-a")
        self.assertIn("presence.json", body["path"])

    def test_foreign_parent_reference_does_not_promote_or_count_worker(self) -> None:
        conn = connect(self.db)
        conn.executemany(
            """
            INSERT INTO sessions (id, harness, external_id, parent_session_id, repo)
            VALUES (?, ?, ?, ?, '/tmp/project')
            """,
            [
                ("codex:root", "codex", "root", None),
                ("cursor:proj/live-1", "cursor", "proj/live-1", "codex:root"),
            ],
        )
        conn.commit()
        conn.close()

        body = live_payload(
            self.db,
            presence_path=self.presence,
            now=self.now,
            scan=False,
        )
        self.assertEqual(body["counts"]["workers"], 0)
        self.assertEqual([row["session_id"] for row in body["sessions"]], ["cursor:proj/live-1"])
        self.assertIsNone(body["sessions"][0]["parent_session_id"])

    def test_typed_provider_worker_link_promotes_cross_harness_owner(self) -> None:
        conn = connect(self.db)
        conn.executescript(
            """
            INSERT INTO sessions (id, harness, external_id, repo)
            VALUES ('t3code:owner', 't3code', 'owner', '/tmp/project'),
                   ('cursor:proj/live-1', 'cursor', 'proj/live-1', '/tmp/project');
            INSERT INTO session_links
              (source_session_id, target_session_id, link_type, target_harness,
               target_external_id, link_role)
            VALUES ('t3code:owner', 'cursor:proj/live-1', 'provider_backing',
                    'cursor', 'proj/live-1', 'worker');
            """
        )
        conn.commit()
        conn.close()

        body = live_payload(
            self.db,
            presence_path=self.presence,
            now=self.now,
            scan=False,
        )
        self.assertEqual(body["counts"]["workers"], 1)
        self.assertEqual(body["sessions"][0]["parent_session_id"], "t3code:owner")
        self.assertEqual(body["sessions"][0]["logical_session_id"], "t3code:owner")
        self.assertEqual(body["sessions"][0]["logical_harness"], "t3code")

    def test_sse_emits_presence_on_change(self) -> None:
        data = json.loads(self.presence.read_text(encoding="utf-8"))
        gen = iter_event_sse(
            self.db,
            poll_seconds=0.01,
            max_cycles=4,
            presence_path=self.presence,
        )
        out: list[str] = []
        for i, frame in enumerate(gen):
            out.append(frame)
            if i == 0:
                data["generation"] = 99
                data["sessions"] = [
                    {
                        "harness": "claude",
                        "external_id": "uuid-1",
                        "session_id": None,
                        "source_path": str(self.root / "c.jsonl"),
                        "state": "waiting",
                        "last_activity_at": self.ts,
                        "age_seconds": 0.1,
                        "pending_ingest": True,
                        "title": None,
                        "repo": None,
                    }
                ]
                atomic_write_json(self.presence, data)
                now = time.time() + 10
                os.utime(self.presence, (now, now))
        joined = "".join(out)
        self.assertIn(": connected", joined)
        self.assertIn("event: presence", joined)
        self.assertIn('"epoch":"daemon-a"', joined)
        self.assertIn('"action":"active"', joined)
        self.assertIn("uuid-1", joined)

    def test_sse_future_since_starts_at_current_event_tail(self) -> None:
        conn = connect(self.db)
        try:
            record_ingest_event(
                conn,
                harness="codex",
                sessions_added=1,
                sessions_updated=0,
                messages_added=1,
                ts="2026-08-12T10:00:00+00:00",
            )
        finally:
            conn.close()

        data = json.loads(self.presence.read_text(encoding="utf-8"))
        gen = iter_event_sse(
            self.db,
            since="2099-01-01T00:00:00+00:00",
            poll_seconds=0,
            max_cycles=2,
            presence_path=self.presence,
        )
        self.assertEqual(next(gen), ": connected\n\n")
        data["generation"] = 2
        atomic_write_json(self.presence, data)
        self.assertIn("event: presence", next(gen))

        conn = connect(self.db)
        try:
            record_ingest_event(
                conn,
                harness="cursor",
                sessions_added=2,
                sessions_updated=0,
                messages_added=3,
                ts="2026-08-12T11:00:00+00:00",
            )
        finally:
            conn.close()
        joined = "".join(gen)
        self.assertIn('"harness":"cursor"', joined)
        self.assertIn("id:2", joined)
        self.assertNotIn('"harness":"codex"', joined)

    def test_sse_bootstrap_cannot_skip_an_interleaved_commit(self) -> None:
        reader = connect(self.db)
        inserted: list[int] = []

        def list_then_commit(*args: object, **kwargs: object):
            events = list_ingest_events(*args, **kwargs)
            writer = connect(self.db)
            try:
                event = record_ingest_event(
                    writer,
                    harness="codex",
                    sessions_added=1,
                    sessions_updated=0,
                    messages_added=1,
                    ts="2026-08-12T12:00:00+00:00",
                )
                inserted.append(event.id)
            finally:
                writer.close()
            return events

        try:
            with mock.patch(
                "agentlog.api.events.list_ingest_events",
                side_effect=list_then_commit,
            ):
                events, tail_id = _bootstrap_ingest_events(
                    reader,
                    since="2099-01-01T00:00:00+00:00",
                    limit=200,
                )
        finally:
            reader.close()

        self.assertGreater(inserted[0], 0)
        selected = {event.id for event in events}
        self.assertTrue(inserted[0] in selected or tail_id < inserted[0])

    def test_sse_reconnect_resumes_after_last_event_id(self) -> None:
        conn = connect(self.db)
        try:
            first = record_ingest_event(
                conn,
                harness="codex",
                sessions_added=1,
                sessions_updated=0,
                messages_added=1,
                ts="2026-08-12T10:00:00+00:00",
            )
            second = record_ingest_event(
                conn,
                harness="cursor",
                sessions_added=1,
                sessions_updated=0,
                messages_added=2,
                ts="2026-08-12T10:01:00+00:00",
            )
        finally:
            conn.close()

        initial = "".join(
            iter_event_sse(
                self.db,
                since="2026-08-12T09:00:00+00:00",
                poll_seconds=0,
                max_cycles=1,
                presence_path=self.presence,
            )
        )
        self.assertIn(f"id:{first.id}\n", initial)
        self.assertIn(f"id:{second.id}\n", initial)

        conn = connect(self.db)
        try:
            third = record_ingest_event(
                conn,
                harness="claude",
                sessions_added=1,
                sessions_updated=0,
                messages_added=3,
                ts="2026-08-12T10:02:00+00:00",
            )
        finally:
            conn.close()
        resumed = "".join(
            iter_event_sse(
                self.db,
                since="2026-08-12T09:00:00+00:00",
                after_id=second.id,
                poll_seconds=0,
                max_cycles=1,
                presence_path=self.presence,
            )
        )
        self.assertIn(f"id:{third.id}\n", resumed)
        self.assertNotIn(f"id:{first.id}\n", resumed)
        self.assertNotIn(f"id:{second.id}\n", resumed)


class SourceSnapshotStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.source = self.root / "source.jsonl"
        self.source.write_text('{"type":"start"}\n', encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _row(self, **overrides: object) -> dict:
        row = {
            "logical_session_id": "t3code:root",
            "readiness_storage": "source_backed",
            "readiness_artifact_path": str(self.source),
            "readiness_sync_status": "current",
            "readiness_artifact_size": 1,
            "readiness_artifact_mtime_ns": 1,
        }
        row.update(overrides)
        return row

    def test_normal_append_remains_a_stable_readable_snapshot(self) -> None:
        with self.source.open("a", encoding="utf-8") as handle:
            handle.write('{"type":"append"}\n')

        self.assertEqual(_source_snapshot_status(self._row()), "stable")

    def test_missing_identity_path_or_frozen_state_is_pending(self) -> None:
        self.assertEqual(
            _source_snapshot_status(self._row(logical_session_id=None)), "pending"
        )
        self.assertEqual(
            _source_snapshot_status(
                self._row(readiness_artifact_path=str(self.root / "missing.jsonl"))
            ),
            "pending",
        )
        self.assertEqual(
            _source_snapshot_status(
                self._row(readiness_sync_status="frozen_diverged")
            ),
            "pending",
        )


class ExternalIdHelperTests(unittest.TestCase):
    def test_skips_non_jsonl(self) -> None:
        self.assertIsNone(external_id_for_path("cursor", Path("/tmp/state.vscdb")))
        self.assertIsNone(
            external_id_for_path("claude", Path("/tmp/skill-injections.jsonl"))
        )


if __name__ == "__main__":
    unittest.main()
