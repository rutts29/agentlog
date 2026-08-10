from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from agentlog.api import tokens
from agentlog.api.activity import activity_calendar
from agentlog.api.clusters import resolve_session_roots
from agentlog.analysis.skills import index_skills, list_skill_profiles, skill_detail
from agentlog.api.descriptive import (
    auto_review_surface,
    duration_and_volume,
    ledger_counts,
    model_monthly_mix,
    orchestration_overview,
    request_kind_distribution,
    search_messages,
    session_facets,
    sessions_daily_by,
    tool_usage,
)
from agentlog.api.harnesses import harness_matrix
from agentlog.api.queries import model_mix, recent_sessions
from agentlog.api.ranges import TimeRange
from agentlog.api.identity_aggregates import visible_logical_sessions
from agentlog.db.schema import connect, init_db


def _tr() -> TimeRange:
    return TimeRange(
        key="all",
        start=None,
        end=datetime(2026, 8, 11, tzinfo=timezone.utc),
        prev_start=None,
        prev_end=None,
    )


class IdentitySafeAggregateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self._tmp.name) / "aggregate.db")
        init_db(self.conn)
        self.conn.executescript(
            """
            INSERT INTO sessions
              (id, harness, external_id, parent_session_id, started_at, ended_at,
               repo, model, model_canonical)
            VALUES
              ('t3-root', 't3code', 'root', NULL,
               '2026-08-10T10:00:00+00:00', '2026-08-10T10:10:00+00:00',
               '/repo', 'orchestrator-model', 'orchestrator-model'),
              ('codex-backing', 'codex', 'backing', NULL,
               '2026-08-10T10:00:01+00:00', '2026-08-10T10:09:00+00:00',
               '/repo', 'backing-model', 'backing-model'),
              ('codex-worker', 'codex', 'worker', 'backing',
               '2026-08-10T10:02:00+00:00', '2026-08-10T10:08:00+00:00',
               '/repo', 'worker-model', 'worker-model'),
              ('codex-grandchild', 'codex', 'grandchild', 'worker',
               '2026-08-10T10:03:00+00:00', '2026-08-10T10:07:00+00:00',
               '/repo', 'grandchild-model', 'grandchild-model'),
              ('t3-unlinked', 't3code', 'unlinked', NULL,
               '2026-08-10T10:04:00+00:00', '2026-08-10T10:05:00+00:00',
               '/repo', 't3-model', 't3-model'),
              ('codex-independent', 'codex', 'independent', NULL,
               '2026-08-10T10:05:00+00:00', '2026-08-10T10:06:00+00:00',
               '/repo', 'independent-model', 'independent-model');

            INSERT INTO messages
              (id, session_id, seq, role, model, model_canonical, text, content_hash)
            VALUES
              ('m-root', 't3-root', 1, 'assistant', 'orchestrator-model', 'orchestrator-model', 'root', 'h1'),
              ('m-backing', 'codex-backing', 1, 'assistant', 'backing-model', 'backing-model', 'backing', 'h2'),
              ('m-worker', 'codex-worker', 1, 'assistant', 'worker-model', 'worker-model', 'worker', 'h3'),
              ('m-grandchild', 'codex-grandchild', 1, 'assistant', 'grandchild-model', 'grandchild-model', 'grandchild', 'h4'),
              ('m-t3', 't3-unlinked', 1, 'assistant', 't3-model', 't3-model', 't3', 'h5'),
              ('m-codex', 'codex-independent', 1, 'assistant', 'independent-model', 'independent-model', 'codex', 'h6');

            INSERT INTO tool_events (id, session_id, seq, tool_name, action)
            VALUES
              ('tool-root', 't3-root', 1, 'root', 'call'),
              ('tool-backing', 'codex-backing', 1, 'backing', 'call'),
              ('tool-worker', 'codex-worker', 1, 'worker', 'call'),
              ('tool-grandchild', 'codex-grandchild', 1, 'grandchild', 'call'),
              ('tool-t3', 't3-unlinked', 1, 't3', 'call'),
              ('tool-codex', 'codex-independent', 1, 'codex', 'call');

            INSERT INTO token_usage
              (id, session_id, seq, granularity, usage_source, model,
               input_tokens, output_tokens, total_tokens)
            VALUES
              ('tok-backing', 'codex-backing', 1, 'session_cumulative', 'fixture', 'backing-model', 90, 10, 100),
              ('tok-worker', 'codex-worker', 1, 'session_cumulative', 'fixture', 'worker-model', 45, 5, 50),
              ('tok-grandchild', 'codex-grandchild', 1, 'session_cumulative', 'fixture', 'grandchild-model', 18, 2, 20),
              ('tok-codex', 'codex-independent', 1, 'session_cumulative', 'fixture', 'independent-model', 9, 1, 10);

            INSERT INTO session_links
              (source_session_id, target_session_id, link_type, target_harness,
               target_external_id, link_role, confidence, evidence_json)
            VALUES ('t3-root', 'codex-backing', 'provider_backing', 'codex',
                    'backing', 'root', 'observed', '{}');
            """
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_aggregates_collapse_root_backing_and_keep_workers(self) -> None:
        ledger = ledger_counts(self.conn, _tr())
        self.assertEqual(ledger["sessions"], 5)
        self.assertEqual(ledger["messages"], 5)
        self.assertEqual(ledger["tool_events"], 5)

        daily = sessions_daily_by(self.conn, _tr(), by="harness")
        self.assertEqual(daily[0]["total"], 5)
        self.assertEqual(daily[0]["t3code"], 4)
        self.assertEqual(daily[0]["codex"], 1)

        facets = session_facets(self.conn, _tr())
        harnesses = {item["value"]: item["count"] for item in facets["harness"]}
        self.assertEqual(harnesses, {"t3code": 4, "codex": 1})

        monthly = model_monthly_mix(self.conn, _tr())["series"]
        self.assertEqual(monthly[0]["total"], 5)
        models = {item["model"] for item in monthly[0]["items"]}
        self.assertIn("backing-model", models)
        self.assertNotIn("orchestrator-model", models)

        mix = {item["model"]: item for item in model_mix(self.conn, _tr())}
        self.assertIn("backing-model", mix)
        self.assertNotIn("orchestrator-model", mix)
        self.assertEqual(mix["backing-model"]["harnesses"][0]["harness"], "t3code")

        recent = {item["id"]: item for item in recent_sessions(self.conn, _tr())}
        self.assertNotIn("codex-backing", recent)
        self.assertEqual(recent["t3-root"]["message_count"], 1)

        calendar = activity_calendar(self.conn, _tr())
        cell = next(item for item in calendar["days"] if item["date"] == "2026-08-10")
        self.assertEqual(cell["sessions"], 5)
        self.assertEqual(cell["messages"], 5)
        self.assertEqual(cell["active_harnesses"], ["codex", "t3code"])

        distribution = duration_and_volume(self.conn, _tr())
        self.assertEqual(distribution["identity_grain"], "logical_sessions")
        self.assertEqual(distribution["sessions"], 5)
        self.assertEqual(
            next(item for item in distribution["message_buckets"] if item["bucket"] == "1–2")["count"],
            5,
        )

        registry = harness_matrix(self.conn)
        self.assertEqual(registry["identity_grain"], "logical_sessions")
        by_harness = {item["id"]: item for item in registry["items"]}
        self.assertEqual(by_harness["t3code"]["sessions"], 4)
        self.assertEqual(by_harness["t3code"]["messages"], 4)
        self.assertEqual(by_harness["codex"]["sessions"], 1)

    def test_tokens_and_semantic_roots_use_logical_identity(self) -> None:
        coverage = tokens.coverage(self.conn, _tr())
        self.assertEqual(coverage["sessions_total"], 5)
        by_harness = {item["harness"]: item for item in coverage["by_harness"]}
        self.assertEqual(by_harness["t3code"]["sessions"], 4)
        self.assertEqual(by_harness["t3code"]["sessions_with_usage"], 3)

        token_rows = {item["harness"]: item for item in tokens.by_harness(self.conn, _tr())["items"]}
        self.assertEqual(token_rows["t3code"]["totals"]["total_tokens"], 170)
        detail = tokens.session_tokens(self.conn, "t3-root")
        assert detail is not None
        self.assertEqual(detail["transcript_session_id"], "codex-backing")
        self.assertEqual(detail["totals"]["total_tokens"], 100)

        roots = resolve_session_roots(self.conn)
        for session_id in ("t3-root", "codex-backing", "codex-worker", "codex-grandchild"):
            self.assertEqual(roots[session_id], "t3-root")

    def test_tools_request_kinds_search_and_skills_use_canonical_transcript(self) -> None:
        self.conn.execute(
            """
            INSERT INTO derivation_runs
              (id, kind, extractor_name, extractor_version, started_at, status)
            VALUES ('det-run', 'det', 'det', '1', '2026-08-10T10:00:00+00:00', 'ok')
            """
        )
        for session_id, kind in (
            ("t3-root", "root_only"),
            ("codex-backing", "substantive"),
            ("codex-worker", "worker_brief"),
        ):
            request_id = f"{session_id}:request"
            self.conn.execute(
                """
                INSERT INTO messages (id, session_id, seq, role, text, content_hash)
                VALUES (?, ?, 2, 'user', ?, ?)
                """,
                (request_id, session_id, f"request {kind}", request_id),
            )
            self.conn.execute(
                """
                INSERT INTO exchange_windows
                  (id, session_id, request_message_id, response_message_id, input_hash, content_hash)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    f"w:{session_id}",
                    session_id,
                    request_id,
                    f"m-{session_id.split('-', 1)[-1]}",
                    request_id,
                    request_id,
                ),
            )
            self.conn.execute(
                """
                INSERT INTO window_det_classifications
                  (id, window_id, run_id, turn_kinds_json, request_kind, route,
                   extractor_name, extractor_version, created_at)
                VALUES (?, ?, 'det-run', '[]', ?, 'ux', 'det', '1', '2026-08-10T10:00:00+00:00')
                """,
                (f"d:{session_id}", f"w:{session_id}", kind),
            )
        self.conn.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")

        tools = tool_usage(self.conn, _tr())
        self.assertEqual(tools["total"], 5)
        by_tool = {item["tool"]: item for item in tools["items"]}
        self.assertNotIn("root", by_tool)
        self.assertEqual(by_tool["backing"]["by_harness"], {"t3code": 1})

        kinds = request_kind_distribution(self.conn, _tr())
        self.assertEqual(kinds["total"], 2)
        self.assertEqual(
            {item["request_kind"] for item in kinds["items"]},
            {"substantive", "worker_brief"},
        )

        backing_hit = search_messages(self.conn, _tr(), q="backing")
        self.assertEqual(backing_hit["total"], 1)
        self.assertEqual(backing_hit["items"][0]["session_id"], "t3-root")
        self.assertEqual(backing_hit["items"][0]["harness"], "t3code")
        self.assertEqual(backing_hit["items"][0]["physical_session_id"], "codex-backing")
        self.assertEqual(search_messages(self.conn, _tr(), q="root")["total"], 0)
        self.assertEqual(
            search_messages(self.conn, _tr(), q="backing", harness=["codex"])["total"],
            0,
        )

        skills_root = Path(self._tmp.name) / "skills"
        skill_path = skills_root / "writing-plans" / "SKILL.md"
        skill_path.parent.mkdir(parents=True)
        skill_path.write_text(
            "---\nname: writing-plans\ndescription: plans\n---\n# Writing plans\n",
            encoding="utf-8",
        )
        index_skills(self.conn, [("codex", skills_root)], now="2026-08-10T10:00:00+00:00")
        for exposure_id, session_id in (
            ("skill-root", "t3-root"),
            ("skill-backing", "codex-backing"),
            ("skill-worker", "codex-worker"),
        ):
            self.conn.execute(
                """
                INSERT INTO skill_exposures
                  (id, session_id, message_id, skill_name, exposure_type)
                VALUES (?, ?, NULL, 'writing-plans', 'invoked')
                """,
                (exposure_id, session_id),
            )
        self.conn.commit()
        profile = next(
            item
            for item in list_skill_profiles(self.conn, min_sessions=1)["items"]
            if item["name"] == "writing-plans"
        )
        self.assertEqual(profile["fires"], 2)
        self.assertEqual(profile["sessions"], 2)
        detail = skill_detail(self.conn, str(profile["id"]), min_sessions=1)
        assert detail is not None
        exposure_sessions = {item["session_id"]: item for item in detail["exposure_sessions"]}
        self.assertEqual(set(exposure_sessions), {"t3-root", "codex-worker"})
        self.assertEqual(exposure_sessions["t3-root"]["transcript_session_id"], "codex-backing")

    def test_auto_review_uses_backing_metrics_without_hiding_workers(self) -> None:
        self.conn.execute(
            """
            INSERT INTO derivation_runs
              (id, kind, extractor_name, extractor_version, started_at, status)
            VALUES ('auto-run', 'det', 'auto', '1', '2026-08-10T10:00:00+00:00', 'ok')
            """
        )
        for session_id in ("t3-root", "codex-backing", "codex-worker"):
            window_id = f"auto-window:{session_id}"
            message_id = {
                "t3-root": "m-root",
                "codex-backing": "m-backing",
                "codex-worker": "m-worker",
            }[session_id]
            self.conn.execute(
                """
                INSERT INTO exchange_windows
                  (id, session_id, request_message_id, response_message_id, input_hash, content_hash)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (window_id, session_id, message_id, message_id, window_id, window_id),
            )
            self.conn.execute(
                """
                INSERT INTO auto_review_observations
                  (id, window_id, run_id, payload_json, extractor_name,
                   extractor_version, created_at)
                VALUES (?, ?, 'auto-run', '{"status":"ok"}', 'auto', '1',
                        '2026-08-10T10:10:00+00:00')
                """,
                (f"auto:{session_id}", window_id),
            )
        self.conn.commit()

        surface = auto_review_surface(self.conn, _tr())
        self.assertEqual(surface["total"], 2)
        self.assertEqual(surface["by_day"], [{"day": "2026-08-10", "count": 2}])
        models = {item["model"]: item for item in surface["by_model"]}
        self.assertNotIn("orchestrator-model", models)
        self.assertEqual(models["backing-model"]["harnesses"], [{"harness": "t3code", "count": 1}])
        self.assertEqual(models["worker-model"]["harnesses"], [{"harness": "t3code", "count": 1}])

        items = {item["physical_session_id"]: item for item in surface["items"]}
        self.assertEqual(set(items), {"codex-backing", "codex-worker"})
        self.assertEqual(items["codex-backing"]["session_id"], "t3-root")
        self.assertEqual(items["codex-backing"]["transcript_session_id"], "codex-backing")
        self.assertEqual(items["codex-backing"]["harness"], "t3code")
        self.assertEqual(items["codex-backing"]["model"], "backing-model")
        self.assertEqual(items["codex-worker"]["session_id"], "codex-worker")
        self.assertEqual(items["codex-worker"]["harness"], "t3code")
        self.assertEqual(items["codex-worker"]["runtime_harness"], "codex")

    def test_fallback_auto_review_and_overview_signals_use_t3_once(self) -> None:
        self.conn.executescript(
            """
            UPDATE sessions
            SET model = 'grok-4.5', model_canonical = 'grok-4.5',
                provider = 'xai', agent_profile = 'grok'
            WHERE id = 't3-root';
            UPDATE sessions
            SET provider = 'openai', agent_profile = 'codex'
            WHERE id = 'codex-backing';
            INSERT INTO derivation_runs
              (id, kind, extractor_name, extractor_version, started_at, status)
            VALUES ('fallback-run', 'det', 'fixture', '1', '2026-08-10T10:00:00+00:00', 'ok');
            INSERT INTO exchange_windows
              (id, session_id, request_message_id, response_message_id, input_hash, content_hash)
            VALUES
              ('fallback-root-window', 't3-root', 'm-root', 'm-root', 'root', 'root'),
              ('fallback-backing-window', 'codex-backing', 'm-backing', 'm-backing', 'backing', 'backing'),
              ('fallback-worker-window', 'codex-worker', 'm-worker', 'm-worker', 'worker', 'worker');
            INSERT INTO auto_review_observations
              (id, window_id, run_id, payload_json, extractor_name, extractor_version, created_at)
            VALUES
              ('fallback-root-review', 'fallback-root-window', 'fallback-run', '{"status":"ok"}', 'fixture', '1', '2026-08-10T10:10:00+00:00'),
              ('fallback-backing-review', 'fallback-backing-window', 'fallback-run', '{"status":"ok"}', 'fixture', '1', '2026-08-10T10:10:00+00:00');
            INSERT INTO window_det_classifications
              (id, window_id, run_id, turn_kinds_json, request_kind, route,
               extractor_name, extractor_version, created_at)
            VALUES
              ('fallback-root-kind', 'fallback-root-window', 'fallback-run', '[]', 'auto_review', 'fixture', 'fixture', '1', '2026-08-10T10:10:00+00:00'),
              ('fallback-backing-kind', 'fallback-backing-window', 'fallback-run', '[]', 'auto_review', 'fixture', 'fixture', '1', '2026-08-10T10:10:00+00:00'),
              ('fallback-worker-kind', 'fallback-worker-window', 'fallback-run', '[]', 'worker_brief', 'fixture', 'fixture', '1', '2026-08-10T10:10:00+00:00');
            """
        )
        self.conn.commit()

        surface = auto_review_surface(self.conn, _tr())
        self.assertEqual(surface["total"], 1)
        self.assertEqual(surface["items"][0]["physical_session_id"], "t3-root")
        self.assertEqual(surface["items"][0]["transcript_session_id"], "t3-root")
        self.assertEqual(surface["items"][0]["model"], "grok-4.5")

        overview = orchestration_overview(self.conn, _tr())
        self.assertEqual(overview["signals"]["auto_review"], 1)
        self.assertEqual(overview["signals"]["worker_brief"], 1)

    def test_multi_owner_backing_is_not_collapsed(self) -> None:
        self.conn.execute(
            """
            INSERT INTO sessions
              (id, harness, external_id, started_at, repo, model, model_canonical)
            VALUES ('t3-other', 't3code', 'other', '2026-08-10T10:06:00+00:00',
                    '/repo', 'other-model', 'other-model')
            """
        )
        self.conn.execute(
            """
            INSERT INTO session_links
              (source_session_id, target_session_id, link_type, target_harness,
               target_external_id, link_role, confidence, evidence_json)
            VALUES ('t3-other', 'codex-backing', 'provider_backing', 'codex',
                    'backing', 'root', 'observed', '{}')
            """
        )
        self.conn.commit()
        rows = self.conn.execute("SELECT id, harness FROM sessions").fetchall()
        visible = visible_logical_sessions(self.conn, rows)
        ids = {item.session_id for item in visible}
        self.assertTrue({"t3-root", "t3-other", "codex-backing"} <= ids)


if __name__ == "__main__":
    unittest.main()
