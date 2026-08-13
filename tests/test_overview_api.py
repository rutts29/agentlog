from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from agentlog.api.app import create_app
from agentlog.api import descriptive
from agentlog.api.deps import get_conn
from agentlog.db.schema import connect, init_db
from agentlog.source_reader import CachedSourceTranscriptReader


class OverviewApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "overview.db"
        conn = connect(self.path)
        init_db(conn)
        conn.executescript(
            """
            INSERT INTO sessions
              (id, harness, external_id, started_at, ended_at, model, cwd)
            VALUES
              ('codex:one', 'codex', 'one',
               '2026-08-10T10:00:00+00:00', '2026-08-10T10:05:00+00:00',
               'gpt-5.5', '/tmp/project'),
              ('claude:two', 'claude', 'two',
               '2026-08-11T10:00:00+00:00', '2026-08-11T10:10:00+00:00',
               'claude-opus-5', '/tmp/project');
            INSERT INTO messages (id, session_id, seq, role, model, text)
            VALUES
              ('one:1', 'codex:one', 1, 'user', NULL, 'hello'),
              ('one:2', 'codex:one', 2, 'assistant', 'gpt-5.5', 'done'),
              ('two:1', 'claude:two', 1, 'user', NULL, 'review'),
              ('two:2', 'claude:two', 2, 'assistant', 'claude-opus-5', 'done');
            """
        )
        conn.commit()
        conn.close()
        self.app = create_app(self.path)
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.client.close()
        self.app.state.overview_cache.close()
        self.tmp.cleanup()

    @staticmethod
    def _stable_payload(value):
        if isinstance(value, dict):
            return {
                key: OverviewApiTests._stable_payload(item)
                for key, item in value.items()
                if key not in {"start", "end", "generated_at"}
            }
        if isinstance(value, list):
            return [OverviewApiTests._stable_payload(item) for item in value]
        return value

    def test_overview_sections_match_standalone_endpoints(self) -> None:
        endpoints = {
            "summary": "/api/summary",
            "timeseries": "/api/timeseries/sessions",
            "models": "/api/models",
            "heatmap": "/api/heatmap",
            "projects": "/api/projects",
            "recent": "/api/sessions/recent",
            "tools": "/api/tools",
            "kinds": "/api/request-kinds",
            "distributions": "/api/distributions",
        }
        for range_key in ("24h", "7d", "30d", "all"):
            params = {"range": range_key}
            overview = self.client.get("/api/overview", params=params)
            self.assertEqual(overview.status_code, 200)
            aggregate = overview.json()
            for key, endpoint in endpoints.items():
                with self.subTest(range=range_key, key=key):
                    response = self.client.get(endpoint, params=params)
                    self.assertEqual(response.status_code, 200)
                    expected = {key: response.json()}
                    self.assertEqual(
                        self._stable_payload({key: aggregate[key]}),
                        self._stable_payload(expected),
                    )

    def test_overview_uses_one_connection_dependency(self) -> None:
        conn = sqlite3.connect(str(self.path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        opened = 0

        def one_connection():
            nonlocal opened
            opened += 1
            yield conn

        self.app.dependency_overrides[get_conn] = one_connection
        try:
            response = self.client.get("/api/overview", params={"range": "all"})
        finally:
            self.app.dependency_overrides.pop(get_conn, None)
            conn.close()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(opened, 1)

    def test_overview_preserves_an_existing_transaction(self) -> None:
        conn = sqlite3.connect(str(self.path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")

        def existing_transaction():
            yield conn

        self.app.dependency_overrides[get_conn] = existing_transaction
        try:
            response = self.client.get("/api/overview", params={"range": "all"})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(conn.in_transaction)
        finally:
            self.app.dependency_overrides.pop(get_conn, None)
            conn.rollback()
            conn.close()

    def test_overview_holds_one_wal_snapshot_across_sections(self) -> None:
        original = descriptive.sessions_daily_by

        def commit_then_read(conn, tr, *, by):
            writer = connect(self.path)
            try:
                writer.execute(
                    """
                    INSERT INTO sessions (id, harness, external_id, started_at)
                    VALUES (
                      'cursor:late', 'cursor', 'late',
                      '2026-08-12T10:00:00+00:00'
                    )
                    """
                )
                writer.commit()
            finally:
                writer.close()
            return original(conn, tr, by=by)

        with mock.patch.object(
            descriptive, "sessions_daily_by", side_effect=commit_then_read
        ):
            response = self.client.get("/api/overview", params={"range": "all"})

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["summary"]["kpis"]["sessions"]["value"], 2)
        self.assertEqual(
            sum(day["total"] for day in payload["timeseries"]["series"]), 2
        )
        check = connect(self.path)
        try:
            count = check.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        finally:
            check.close()
        self.assertEqual(count, 3)

    def test_overview_never_hydrates_transcript_sources(self) -> None:
        with mock.patch.object(
            CachedSourceTranscriptReader,
            "__call__",
            side_effect=AssertionError("overview hydrated transcript content"),
        ):
            response = self.client.get("/api/overview", params={"range": "all"})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("graph", response.json())
        self.assertNotIn("attention", response.json())


if __name__ == "__main__":
    unittest.main()
