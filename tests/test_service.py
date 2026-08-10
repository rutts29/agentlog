from __future__ import annotations

import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from agentlog.api.app import create_app
from agentlog.config import LOG_BACKUP_COUNT, LOG_MAX_BYTES
from agentlog.db.schema import connect, init_db
from agentlog.service.health import build_health
from agentlog.service.launchd import (
    API_LABEL,
    WATCH_LABEL,
    default_paths,
    render_api_plist,
    render_watch_plist,
)
from agentlog.service.logging_setup import configure_daemon_logging
from agentlog.service.plists import render_daemon_plist
from agentlog.watch.daemon import WatchDaemon
from agentlog.watch.events import list_ingest_events
from agentlog.watch.presence import atomic_write_json
from agentlog.watch.sources import WatchSource


def _codex_jsonl(session_id: str) -> str:
    lines = [
        {
            "timestamp": "2026-08-09T12:00:00Z",
            "type": "session_meta",
            "payload": {"id": session_id, "cwd": "/tmp/proj", "model": "gpt-5"},
        },
        {
            "timestamp": "2026-08-09T12:00:01Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "catchup hello"},
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


class PlistTests(unittest.TestCase):
    def test_render_contains_keepalive_and_absolute_python(self) -> None:
        body = render_daemon_plist(
            label="com.agentlog.watch",
            python=Path("/tmp/proj/.venv/bin/python"),
            module_args=["-m", "agentlog.watch"],
            working_directory=Path("/tmp/proj"),
            stdout_path=Path("/tmp/logs/out.log"),
            stderr_path=Path("/tmp/logs/err.log"),
            env={"AGENTLOG_LOG_FILE": "/tmp/logs/watch.log"},
        )
        self.assertIn("<string>com.agentlog.watch</string>", body)
        self.assertIn("<key>KeepAlive</key>", body)
        self.assertIn("<true/>", body)
        self.assertIn("<key>RunAtLoad</key>", body)
        self.assertIn("<string>Background</string>", body)
        self.assertIn("<key>Nice</key>", body)
        self.assertIn("/tmp/proj/.venv/bin/python", body)
        self.assertIn("AGENTLOG_LOG_FILE", body)

    def test_default_plists_use_fixed_api_port(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".venv" / "bin").mkdir(parents=True)
            py = root / ".venv" / "bin" / "python"
            py.write_text("", encoding="utf-8")
            paths = default_paths(project_root=root, db_path=root / "db.sqlite")
            watch = render_watch_plist(paths)
            api = render_api_plist(paths)
            self.assertIn(WATCH_LABEL, watch)
            self.assertIn("<string>-m</string>", watch)
            self.assertIn("agentlog.watch", watch)
            self.assertIn(API_LABEL, api)
            self.assertIn("<string>8787</string>", api)
            self.assertIn(str(paths.python), watch)
            self.assertIn(str(paths.python), api)


class HealthTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.db = self.root / "agentlog.db"
        self.conn = connect(self.db)
        init_db(self.conn)
        self.presence = self.root / "presence.json"

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_healthy_when_presence_fresh_and_events_exist(self) -> None:
        from agentlog.watch.events import record_ingest_event

        record_ingest_event(
            self.conn,
            harness="codex",
            sessions_added=1,
            sessions_updated=0,
            messages_added=2,
        )
        now = datetime.now(timezone.utc)
        atomic_write_json(
            self.presence,
            {"ts": now.isoformat(), "generation": 1, "sessions": []},
        )
        payload = build_health(self.db, conn=self.conn, now=now)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["degraded"])
        self.assertIsNone(payload["reason"])
        self.assertTrue(payload["watcher"]["alive"])
        self.assertTrue(payload["watcher"]["presence_fresh"])
        self.assertIn("codex", payload["last_ingest_by_harness"])

    def test_degraded_when_presence_stale(self) -> None:
        from agentlog.watch.events import record_ingest_event

        record_ingest_event(
            self.conn,
            harness="claude",
            sessions_added=0,
            sessions_updated=1,
            messages_added=1,
        )
        old = datetime.now(timezone.utc) - timedelta(seconds=300)
        atomic_write_json(
            self.presence,
            {"ts": old.isoformat(), "generation": 1, "sessions": []},
        )
        # Backdate mtime so health does not treat rewrite time as fresh.
        import os

        os.utime(self.presence, (old.timestamp(), old.timestamp()))
        payload = build_health(
            self.db,
            conn=self.conn,
            now=datetime.now(timezone.utc),
            presence_stale_seconds=45,
        )
        self.assertTrue(payload["degraded"])
        self.assertFalse(payload["watcher"]["alive"])
        self.assertIn("presence stale", payload["reason"] or "")

    def test_api_health_route(self) -> None:
        from agentlog.watch.events import record_ingest_event

        record_ingest_event(
            self.conn,
            harness="cursor",
            sessions_added=1,
            sessions_updated=0,
            messages_added=1,
        )
        atomic_write_json(
            self.presence,
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "generation": 1,
                "sessions": [],
            },
        )
        client = TestClient(create_app(self.db))
        res = client.get("/api/health")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertTrue(body["ok"])
        self.assertIn("watcher", body)
        self.assertIn("last_ingest_by_harness", body)


class CatchupTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.sessions = self.root / "sessions"
        self.sessions.mkdir()
        self.db = self.root / "agentlog.db"
        conn = connect(self.db)
        init_db(conn)
        conn.close()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_start_runs_catchup_without_change_event(self) -> None:
        path = self.sessions / "rollout-catchup-1.jsonl"
        path.write_text(_codex_jsonl("catchup-1"), encoding="utf-8")
        with mock.patch("agentlog.ingest.codex.CODEX_SESSIONS_DIR", self.sessions):
            daemon = WatchDaemon(
                db_path=self.db,
                sources=[WatchSource("codex", self.sessions, poll=False)],
                debounce_seconds=30.0,
                use_watchdog=False,
            )
            # Avoid long-lived threads in the unit test: call catch-up directly
            # after the same setup start() uses before watching forever.
            daemon._catchup_ingest()

        conn = connect(self.db)
        try:
            events = list_ingest_events(conn)
            self.assertGreaterEqual(len(events), 1)
            self.assertEqual(events[0].harness, "codex")
            count = conn.execute(
                "SELECT COUNT(*) AS c FROM sessions WHERE harness = 'codex'"
            ).fetchone()
            self.assertGreaterEqual(int(count["c"]), 1)
        finally:
            conn.close()


class LoggingSetupTests(unittest.TestCase):
    def test_rotating_handler_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "daemon.log"
            configure_daemon_logging(log_path)
            import logging

            root = logging.getLogger()
            handlers = [
                h for h in root.handlers if h.__class__.__name__ == "RotatingFileHandler"
            ]
            self.assertEqual(len(handlers), 1)
            self.assertEqual(handlers[0].maxBytes, LOG_MAX_BYTES)
            self.assertEqual(handlers[0].backupCount, LOG_BACKUP_COUNT)


if __name__ == "__main__":
    unittest.main()
