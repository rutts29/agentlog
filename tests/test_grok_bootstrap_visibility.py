from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from agentlog.analysis.attention import derive_attention
from agentlog.analysis.briefs import build_session_brief
from agentlog.analysis.claims.extract import _eligible_logical_root_ids
from agentlog.analysis.claims.packets import _eligible_root_session_ids
from agentlog.api.activity import activity_calendar, activity_rollup
from agentlog.api import queries as api_queries
from agentlog.api.descriptive import orchestration_overview
from agentlog.api.graph import graph_payload
from agentlog.api.queries import ingest_freshness
from agentlog.api.ranges import TimeRange
from agentlog.api import tokens
from agentlog.db.schema import connect, init_db
from agentlog.db.repository import Repository
from agentlog.mcp_server import tools as mcp_tools
from agentlog.source_reader import read_source_transcript


def _range() -> TimeRange:
    return TimeRange(
        key="all",
        start=None,
        end=datetime(2026, 8, 14, tzinfo=timezone.utc),
        prev_start=None,
        prev_end=None,
    )


class GrokBootstrapVisibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "agentlog.db"
        self.conn = connect(self.db)
        init_db(self.conn)
        self.conn.executescript(
            """
            INSERT INTO sessions
              (id, harness, external_id, parent_session_id, started_at, ended_at,
               repo, model, model_canonical, agent_profile, thread_source)
            VALUES
              ('grok:real', 'grok', 'real', NULL,
               '2026-08-13T10:00:00+00:00', '2026-08-13T10:10:00+00:00',
               '/repo', 'grok-4.6', 'grok-4.6', 'grok-build', NULL),
              ('grok:real-worker', 'grok', 'real-worker', 'real',
               '2026-08-13T10:01:00+00:00', '2026-08-13T10:05:00+00:00',
               '/repo', 'grok-4.6', 'grok-4.6', 'grok-build', 'workflow_subagent'),
              ('grok:bootstrap', 'grok', 'bootstrap', NULL,
               '2026-08-14T10:00:00+00:00', '2026-08-14T10:00:01+00:00',
               '/repo', 'grok-4.6', 'grok-4.6', 'grok-build-plan',
               'grok_bootstrap_only');

            INSERT INTO messages
              (id, session_id, seq, role, text, content_hash, is_tool_plumbing)
            VALUES
              ('bootstrap-system', 'grok:bootstrap', 1, 'system',
               'You are Grok 4.6 released by xAI.', 'bootstrap-system-hash', 1);

            INSERT INTO tool_events
              (id, session_id, seq, tool_name, action)
            VALUES ('bootstrap-tool', 'grok:bootstrap', 1, 'skills', 'result');
            """,
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_aggregates_hide_bootstrap_and_keep_real_grok_root_worker(self) -> None:
        activity = activity_rollup(self.conn, _range())
        grok = next(item for item in activity["by_harness"] if item["harness"] == "grok")
        self.assertEqual(grok["sessions"], 2)
        self.assertEqual(grok["messages"], 0)
        self.assertEqual(ingest_freshness(self.conn)["sessions"], 2)
        calendar = activity_calendar(self.conn, _range())
        self.assertEqual(sum(day["sessions"] for day in calendar["days"]), 2)

        graph = graph_payload(self.conn, _range())
        self.assertEqual(graph["counts"]["sessions"], 2)
        self.assertNotIn("grok:bootstrap", {node["id"] for node in graph["nodes"]})
        self.assertIn("grok:real", {node["id"] for node in graph["nodes"]})
        self.assertIn("grok:real-worker", {node["id"] for node in graph["nodes"]})

        stats = Repository(self.conn).stats()
        stats_grok = next(row for row in stats["by_harness"] if row["harness"] == "grok")
        self.assertEqual(stats_grok["sessions"], 2)
        self.assertEqual(stats["messages"], 0)
        self.assertEqual(stats["tool_events"], 0)
        repository = Repository(self.conn)
        self.assertNotIn(
            "grok:bootstrap",
            {row["id"] for row in repository.list_sessions(harness="grok")},
        )
        self.assertIsNone(repository.get_session("grok:bootstrap"))

        session_list = api_queries.list_sessions(self.conn, _range())
        self.assertNotIn(
            "grok:bootstrap", {item["id"] for item in session_list["items"]}
        )
        self.assertEqual(session_list["total"], 2)
        self.assertIsNone(api_queries.session_detail(self.conn, "grok:bootstrap"))

        self.assertNotIn("grok:bootstrap", _eligible_logical_root_ids(self.conn))
        self.assertNotIn("grok:bootstrap", _eligible_root_session_ids(self.conn))

    def test_direct_consumers_and_orchestration_hide_bootstrap(self) -> None:
        orchestration_ids = {
            item["id"] for item in orchestration_overview(self.conn, _range())["items"]
        }
        self.assertNotIn("grok:bootstrap", orchestration_ids)
        self.assertIn("grok:real", orchestration_ids)
        self.assertIsNone(build_session_brief(self.conn, "grok:bootstrap"))
        source = read_source_transcript(self.conn, "grok:bootstrap")
        self.assertEqual(source.status, "source_unavailable")
        self.assertEqual(source.messages, [])
        self.assertEqual(
            mcp_tools.get_session(self.conn, "grok:bootstrap")["error"],
            "not_found",
        )
        self.assertIsNone(tokens.session_tokens(self.conn, "grok:bootstrap"))

    def test_stale_presence_entry_does_not_reenter_attention(self) -> None:
        presence = Path(self._tmp.name) / "presence.json"
        presence.write_text(
            json.dumps(
                {
                    "sessions": [
                        {
                            "session_id": "grok:bootstrap",
                            "harness": "grok",
                            "state": "waiting",
                            "last_activity_at": "2026-08-14T10:00:00+00:00",
                            "age_seconds": 0,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        items = derive_attention(
            self.conn,
            now=datetime(2026, 8, 14, 10, 0, 1, tzinfo=timezone.utc),
            presence_path=presence,
        )
        self.assertNotIn("grok:bootstrap", {item.session_id for item in items})


if __name__ == "__main__":
    unittest.main()
