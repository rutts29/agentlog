from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from agentlog.analysis.derive import derived_freshness, run_derive
from agentlog.analysis.extractors.deterministic import iter_window_input_rows
from agentlog.analysis.extractors.models import WindowContext
from agentlog.analysis.extractors.triage import triage_window
from agentlog.analysis.extractors.taxonomy import Route
from agentlog.api.app import create_app
from agentlog.db.repository import Repository
from agentlog.db.schema import connect, init_db
from agentlog.normalize.models import (
    Harness,
    NormalizedMessage,
    NormalizedSession,
    ParseResult,
)
from agentlog.service.health import build_health
from agentlog.watch.presence import atomic_write_json


def _seed(
    conn,
    *,
    authored_by_agent: bool = False,
    external_id: str = "derive-sess",
) -> str:
    repo = Repository(conn)
    art_id = repo.upsert_artifact(
        harness="cursor",
        path="/tmp/fixture-derive.jsonl",
        size=10,
        mtime_ns=1,
        content_hash="abc",
        parsed_offset=10,
        parser_version="test",
    )
    result = ParseResult(
        session=NormalizedSession(
            harness=Harness.CURSOR,
            external_id=external_id,
            model="grok-4.5",
        ),
        messages=[
            NormalizedMessage(
                seq=1,
                role="user",
                text="<user_query>\nPlease refactor the auth module.\n</user_query>",
                content_hash="h1",
                authored_by_agent=authored_by_agent,
            ),
            NormalizedMessage(
                seq=2,
                role="assistant",
                text="Refactoring auth.",
                content_hash="h2",
            ),
            NormalizedMessage(
                seq=3,
                role="user",
                text="<user_query>\nlooks good, ship it\n</user_query>",
                content_hash="h3",
            ),
            NormalizedMessage(
                seq=4,
                role="assistant",
                text="Shipped.",
                content_hash="h4",
            ),
        ],
    )
    sid = repo.save_parse_result(artifact_id=art_id, result=result, append=False)
    msgs = repo.list_messages(sid)
    users = [m for m in msgs if m["role"] == "user"]
    assistants = [m for m in msgs if m["role"] == "assistant"]
    windows = []
    for u in users:
        nxt = next((a for a in assistants if a["seq"] > u["seq"]), None)
        if nxt:
            windows.append((u["id"], nxt["id"], u["content_hash"]))
    repo.replace_exchange_windows(sid, windows)
    conn.commit()
    return sid


class DeriveIdempotencyTests(unittest.TestCase):
    def test_second_derive_skips_work(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.db"
            conn = connect(db)
            init_db(conn)
            _seed(conn)
            first = run_derive(conn, index_skill_inventory=False)
            self.assertFalse(first.skipped)
            self.assertGreater(first.windows_updated, 0)
            self.assertEqual(first.windows_classified, first.windows_total)
            second = run_derive(conn, index_skill_inventory=False)
            self.assertTrue(second.skipped)
            self.assertEqual(second.windows_updated, 0)
            self.assertEqual(second.input_fingerprint, first.input_fingerprint)
            n = conn.execute(
                "SELECT COUNT(*) AS c FROM window_det_classifications"
            ).fetchone()["c"]
            self.assertEqual(int(n), first.windows_total)

    def test_targeted_derive_leaves_unrelated_stale_windows_cold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.db"
            conn = connect(db)
            init_db(conn)
            _seed(conn)
            window_id = str(
                conn.execute(
                    "SELECT id FROM exchange_windows ORDER BY id LIMIT 1"
                ).fetchone()["id"]
            )

            result = run_derive(
                conn,
                index_skill_inventory=False,
                window_ids={window_id},
            )

            self.assertFalse(result.skipped)
            self.assertEqual(result.windows_updated, 1)
            self.assertTrue(derived_freshness(conn)["stale"])


class AuthoredByAgentDeriveTests(unittest.TestCase):
    def test_authored_request_classified_worker_brief(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.db"
            conn = connect(db)
            init_db(conn)
            _seed(conn, authored_by_agent=True)
            result = run_derive(conn, index_skill_inventory=False)
            self.assertFalse(result.skipped)
            rows = list(
                conn.execute(
                    """
                    SELECT d.request_kind, d.route, m.authored_by_agent
                    FROM window_det_classifications d
                    JOIN exchange_windows w ON w.id = d.window_id
                    JOIN messages m ON m.id = w.request_message_id
                    ORDER BY m.seq
                    """
                )
            )
            self.assertGreaterEqual(len(rows), 1)
            self.assertTrue(rows[0]["authored_by_agent"])
            self.assertEqual(rows[0]["request_kind"], "worker_brief")
            self.assertEqual(rows[0]["route"], Route.WORKER_TASK.value)

    def test_triage_unit_still_overrides(self) -> None:
        ctx = WindowContext(
            window_id="w1",
            session_id="s1",
            harness="cursor",
            request_text="<user_query>\nYou are a builder.\n</user_query>",
            authored_by_agent=True,
        )
        result = triage_window(ctx)
        self.assertEqual(result.request_kind, "worker_brief")


class DerivedFreshnessHealthTests(unittest.TestCase):
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

    def test_freshness_reports_stale_then_fresh(self) -> None:
        _seed(self.conn)
        before = derived_freshness(self.conn)
        self.assertTrue(before["stale"])
        self.assertEqual(before["windows_classified"], 0)
        self.assertGreater(before["windows_total"], 0)

        run_derive(self.conn, index_skill_inventory=False)
        after = derived_freshness(self.conn)
        self.assertFalse(after["stale"])
        self.assertEqual(after["windows_classified"], after["windows_total"])
        self.assertIsNotNone(after["last_derive_at"])

        now = datetime.now(timezone.utc)
        atomic_write_json(
            self.presence,
            {"ts": now.isoformat(), "generation": 1, "sessions": []},
        )
        from agentlog.watch.events import record_ingest_event

        record_ingest_event(
            self.conn,
            harness="cursor",
            sessions_added=1,
            sessions_updated=0,
            messages_added=2,
        )
        payload = build_health(self.db, conn=self.conn, now=now)
        self.assertIn("derived", payload)
        self.assertFalse(payload["derived"]["stale"])
        self.assertEqual(
            payload["derived"]["windows_classified"],
            payload["derived"]["windows_total"],
        )

        client = TestClient(create_app(self.db))
        res = client.get("/api/health")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertIn("derived", body)
        self.assertIn("windows_total", body["derived"])

    def test_fingerprint_ignores_session_added_after_window_scan(self) -> None:
        _seed(self.conn)
        writer = connect(self.db)
        injected = False

        def inject_after_window_scan(statement: str) -> None:
            nonlocal injected
            if injected or "FROM messages m" not in statement:
                return
            injected = True
            _seed(writer, external_id="added-during-fingerprint")

        self.conn.set_trace_callback(inject_after_window_scan)
        try:
            rows = iter_window_input_rows(self.conn)
        finally:
            self.conn.set_trace_callback(None)
            writer.close()

        self.assertTrue(injected)
        self.assertEqual(len(rows), 2)


if __name__ == "__main__":
    unittest.main()
