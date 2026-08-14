from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from agentlog.api.descriptive import (
    list_sessions_v2,
    orchestration_overview,
    orchestration_tree,
    search_messages,
    session_detail_v2,
)
from agentlog.api import tokens
from agentlog.api.descriptive import ledger_counts
from agentlog.api.identity_aggregates import visible_logical_sessions
from agentlog.api.ranges import TimeRange
from agentlog.db.migrations.v025_session_link_roles import apply as apply_v025
from agentlog.session_identity import (
    lineage_parent_ids,
    logical_projection,
    provider_backing_exclusion_sql,
    provider_backing_shadow_ids,
    provider_root_shadow_ids,
)
from agentlog.db.schema import connect, init_db


def _tr() -> TimeRange:
    return TimeRange(
        key="all",
        start=None,
        end=datetime(2026, 8, 11, tzinfo=timezone.utc),
        prev_start=None,
        prev_end=None,
    )


class SessionIdentityProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self._tmp.name) / "identity.db")
        init_db(self.conn)
        self.conn.executescript(
            """
            INSERT INTO sessions
              (id, harness, external_id, parent_session_id, started_at, ended_at,
               repo, model)
            VALUES
              ('t3code:root', 't3code', 'root', NULL,
               '2026-08-10T10:00:00+00:00', '2026-08-10T10:10:00+00:00',
               '/repo', 'gpt-5.6-sol'),
              ('t3code:child', 't3code', 'child', 'root',
               '2026-08-10T10:01:00+00:00', '2026-08-10T10:05:00+00:00',
               '/repo', 'gpt-5.6-sol'),
              ('codex:backing', 'codex', 'backing', NULL,
               '2026-08-10T10:00:01+00:00', '2026-08-10T10:09:00+00:00',
               '/repo', 'gpt-5.6-sol'),
              ('codex:worker', 'codex', 'worker', 'backing',
               '2026-08-10T10:02:00+00:00', '2026-08-10T10:08:00+00:00',
               '/repo', 'gpt-5.6-sol'),
              ('codex:grandchild', 'codex', 'grandchild', 'worker',
               '2026-08-10T10:03:00+00:00', '2026-08-10T10:07:00+00:00',
               '/repo', 'gpt-5.6-sol'),
              ('codex:unlinked', 'codex', 'unlinked', NULL,
               '2026-08-10T09:00:00+00:00', '2026-08-10T09:05:00+00:00',
               '/repo', 'gpt-5.5');

            INSERT INTO messages (id, session_id, seq, role, text, content_hash)
            VALUES
              ('t3-u', 't3code:root', 1, 'user', 'orchestrator request', 't3-u'),
              ('t3-a', 't3code:root', 2, 'assistant', 'orchestrator reply', 't3-a'),
              ('cx-u', 'codex:backing', 1, 'user', 'provider request', 'cx-u'),
              ('cx-a', 'codex:backing', 2, 'assistant', 'rich provider reply', 'cx-a'),
              ('cw-u', 'codex:worker', 1, 'user', 'worker request', 'cw-u'),
              ('cg-u', 'codex:grandchild', 1, 'user', 'grandchild request', 'cg-u'),
              ('cu-u', 'codex:unlinked', 1, 'user', 'unlinked request', 'cu-u');

            INSERT INTO tool_events
              (id, session_id, message_id, seq, tool_name, action)
            VALUES ('tool', 'codex:backing', 'cx-a', 1, 'exec_command', 'call');

            INSERT INTO exchange_windows
              (id, session_id, request_message_id, response_message_id,
               input_hash, content_hash)
            VALUES ('window', 'codex:backing', 'cx-u', 'cx-a', 'cx-u', 'window');

            INSERT INTO session_links
              (source_session_id, target_session_id, link_type,
               target_harness, target_external_id, link_role,
               confidence, evidence_json)
            VALUES ('t3code:root', 'codex:backing', 'provider_backing',
                    'codex', 'backing', 'root', 'observed', '{}');
            """
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_default_list_returns_presentation_roots(self) -> None:
        result = list_sessions_v2(self.conn, _tr())
        rows = {item["id"]: item for item in result["items"]}
        self.assertEqual(set(rows), {"t3code:root", "codex:unlinked"})
        self.assertEqual(result["view"], "roots")
        self.assertEqual(result["total"], 2)
        root = rows["t3code:root"]
        self.assertEqual(root["logical_harness"], "t3code")
        self.assertEqual(root["runtime_harness"], "codex")
        self.assertEqual(root["orchestrator_session_id"], "t3code:root")
        self.assertEqual(root["transcript_session_id"], "codex:backing")
        self.assertEqual(root["message_count"], 4)
        self.assertEqual(root["tool_count"], 1)
        self.assertEqual(root["navigation_id"], "t3code:root")
        self.assertEqual(root["child_count"], 2)
        self.assertEqual(root["descendant_count"], 3)
        self.assertFalse(root["is_orphan"])

    def test_harness_sort_uses_logical_harness_with_stable_id_ties(self) -> None:
        result = list_sessions_v2(
            self.conn,
            _tr(),
            sort="harness",
            order="asc",
        )
        rows = result["items"]
        self.assertEqual(
            [row["logical_harness"] for row in rows],
            ["codex", "t3code"],
        )
        self.assertEqual(
            [row["id"] for row in rows],
            ["codex:unlinked", "t3code:root"],
        )

    def test_t3_detail_uses_provider_transcript_and_codex_stays_direct(self) -> None:
        detail = session_detail_v2(self.conn, "t3code:root")
        assert detail is not None
        self.assertEqual(detail["session"]["id"], "t3code:root")
        self.assertEqual(detail["session"]["logical_harness"], "t3code")
        self.assertEqual(detail["transcript"]["id"], "codex:backing")
        self.assertEqual(detail["messages"][1]["text"], "rich provider reply")
        detail_children = {child["id"]: child for child in detail["children"]}
        self.assertEqual(set(detail_children), {"t3code:child", "codex:worker"})
        logical_child = detail_children["codex:worker"]
        self.assertEqual(logical_child["logical_harness"], "t3code")
        self.assertEqual(logical_child["runtime_harness"], "codex")
        self.assertEqual(
            logical_child["orchestrator_session_id"], "t3code:root"
        )
        self.assertEqual(logical_child["parent_navigation_id"], "t3code:root")
        self.assertEqual(detail["session"]["child_count"], 2)
        direct = session_detail_v2(self.conn, "codex:backing")
        assert direct is not None
        self.assertEqual(direct["session"]["id"], "codex:backing")
        self.assertEqual(direct["session"]["harness"], "codex")
        self.assertEqual(direct["session"]["logical_harness"], "t3code")
        self.assertEqual(direct["session"]["runtime_harness"], "codex")
        self.assertEqual(direct["session"]["navigation_id"], "t3code:root")
        direct_children = {child["id"]: child for child in direct["children"]}
        child = direct_children["codex:worker"]
        self.assertEqual(child["harness"], "codex")
        self.assertEqual(child["logical_harness"], "t3code")
        self.assertEqual(child["runtime_harness"], "codex")
        self.assertEqual(child["orchestrator_session_id"], "t3code:root")

    def test_source_backed_t3_keeps_runtime_backing_provenance(self) -> None:
        self.conn.executescript(
            """
            INSERT INTO artifacts
              (harness,path,size,mtime_ns,content_hash,parsed_offset,
               parser_version,transcript_storage)
            VALUES ('t3code','/tmp/t3.jsonl',1,1,'t3',1,'test','source_backed'),
                   ('codex','/tmp/backing.jsonl',1,1,'backing',1,'test','source_backed');
            INSERT INTO sessions
              (id,harness,external_id,artifact_id,transcript_storage)
            VALUES ('t3code:source-root','t3code','source-root',1,'source_backed'),
                   ('codex:source-backing','codex','source-backing',2,'source_backed');
            INSERT INTO session_links
              (source_session_id,target_session_id,link_type,target_harness,
               target_external_id,link_role)
            VALUES ('t3code:source-root','codex:source-backing','provider_backing',
                    'codex','source-backing','root');
            """
        )
        self.conn.commit()

        projection = logical_projection(
            self.conn, "t3code:source-root", "t3code"
        )
        self.assertIsNone(projection["transcript_session_id"])
        self.assertEqual(projection["runtime_harness"], "codex")
        backing = projection["provider_backings"][0]
        self.assertEqual(backing["target_session_id"], "codex:source-backing")
        self.assertEqual(backing["artifact_path"], "/tmp/backing.jsonl")
        self.assertEqual(
            projection["runtime_backing_provenance"],
            {
                "status": "validated",
                "harness": "codex",
                "session_id": "codex:source-backing",
                "external_id": "source-backing",
                "artifact_id": 2,
                "artifact_path": "/tmp/backing.jsonl",
            },
        )
        detail = session_detail_v2(self.conn, "t3code:source-root")
        assert detail is not None
        self.assertEqual(
            detail["session"]["runtime_backing_provenance"],
            projection["runtime_backing_provenance"],
        )
        listed = {
            item["id"] for item in list_sessions_v2(self.conn, _tr())["items"]
        }
        self.assertIn("t3code:source-root", listed)
        self.assertNotIn("codex:source-backing", listed)

    def test_typed_provider_worker_parent_wins_over_native_runtime_parent(self) -> None:
        self.conn.execute(
            """
            INSERT INTO session_links
              (source_session_id, target_session_id, link_type, target_harness,
               target_external_id, link_role)
            VALUES ('t3code:root', 'codex:worker', 'provider_backing', 'codex',
                    'worker', 'worker')
            """
        )
        self.conn.commit()

        self.assertEqual(
            lineage_parent_ids(self.conn)["codex:worker"], "t3code:root"
        )

    def test_agent_launch_worker_is_explicit_without_identity_projection(self) -> None:
        self.conn.executescript(
            """
            INSERT INTO sessions
              (id, harness, external_id, started_at, thread_source)
            VALUES ('grok:launched', 'grok', 'launched',
                    '2026-08-10T10:06:00+00:00', 'autonomous_agent_unlinked');
            INSERT INTO session_links
              (source_session_id, target_session_id, link_type,
               target_harness, target_external_id, link_role)
            VALUES ('t3code:root', 'grok:launched', 'agent_launch',
                    'grok', 'launched', 'worker');
            """
        )
        self.conn.commit()

        self.assertEqual(lineage_parent_ids(self.conn)["grok:launched"], "t3code:root")
        tree = orchestration_tree(self.conn, "grok:launched")
        assert tree is not None
        worker = next(
            child for child in tree["tree"]["children"] if child["id"] == "grok:launched"
        )
        self.assertEqual(worker["relationship"], "agent_worker")
        self.assertEqual(worker["logical_harness"], "grok")
        self.assertEqual(worker["runtime_harness"], "grok")
        self.assertEqual(worker["transcript_session_id"], "grok:launched")
        self.assertNotIn("grok:launched", provider_backing_shadow_ids(self.conn))
        narrow = TimeRange(
            key="custom",
            start=datetime(2026, 8, 10, 10, 5, tzinfo=timezone.utc),
            end=datetime(2026, 8, 10, 10, 7, tzinfo=timezone.utc),
            prev_start=None,
            prev_end=None,
        )
        listed = list_sessions_v2(self.conn, narrow)
        self.assertEqual([item["id"] for item in listed["items"]], ["t3code:root"])
        overview = orchestration_overview(self.conn, narrow)
        row = next(item for item in overview["items"] if item["id"] == "t3code:root")
        self.assertGreaterEqual(row["child_count"], 1)

    def test_ambiguous_agent_launch_links_remain_roots(self) -> None:
        self.conn.executescript(
            """
            INSERT INTO sessions (id, harness, external_id, started_at)
            VALUES ('grok:ambiguous', 'grok', 'ambiguous', '2026-08-10T10:06:00+00:00');
            INSERT INTO session_links
              (source_session_id, target_session_id, link_type,
               target_harness, target_external_id, link_role)
            VALUES ('t3code:root', 'grok:ambiguous', 'agent_launch', 'grok', 'ambiguous', 'worker'),
                   ('t3code:child', 'grok:ambiguous', 'agent_launch', 'grok', 'ambiguous', 'worker');
            """
        )
        self.conn.commit()

        self.assertNotIn("grok:ambiguous", lineage_parent_ids(self.conn))
        tree = orchestration_tree(self.conn, "grok:ambiguous")
        assert tree is not None
        self.assertEqual(tree["root_id"], "grok:ambiguous")

    def test_same_harness_parent_wins_over_agent_launch(self) -> None:
        self.conn.executescript(
            """
            INSERT INTO sessions (id, harness, external_id, parent_session_id, started_at)
            VALUES ('grok:physical-parent', 'grok', 'physical-parent', NULL, '2026-08-10T10:06:00+00:00'),
                   ('grok:physical-child', 'grok', 'physical-child', 'physical-parent', '2026-08-10T10:07:00+00:00');
            INSERT INTO session_links
              (source_session_id, target_session_id, link_type,
               target_harness, target_external_id, link_role)
            VALUES ('t3code:root', 'grok:physical-child', 'agent_launch',
                    'grok', 'physical-child', 'worker');
            """
        )
        self.conn.commit()

        self.assertEqual(
            lineage_parent_ids(self.conn)["grok:physical-child"],
            "grok:physical-parent",
        )

    def test_tree_keeps_backing_as_provenance_and_flattens_its_children(self) -> None:
        self.conn.executescript(
            """
            UPDATE sessions
            SET model = 'backing-model', model_canonical = 'backing-model', effort = 'high'
            WHERE id = 'codex:backing';
            INSERT INTO session_links
              (source_session_id, target_session_id, link_type,
               target_harness, target_external_id, link_role,
               confidence, evidence_json)
            VALUES ('t3code:root', 'codex:worker', 'provider_backing',
                    'codex', 'worker', 'worker', 'observed', '{}')
            ;
            """
        )
        self.conn.commit()
        result = orchestration_tree(self.conn, "t3code:root")
        assert result is not None
        children = {child["id"]: child for child in result["tree"]["children"]}
        self.assertNotIn("codex:backing", children)
        self.assertEqual(
            result["tree"]["provider_backings"][0]["target_session_id"],
            "codex:backing",
        )
        self.assertEqual(result["tree"]["model"], "backing-model")
        self.assertEqual(result["tree"]["effort"], "high")
        worker = children["codex:worker"]
        self.assertEqual(worker["logical_harness"], "t3code")
        self.assertEqual(worker["children"][0]["id"], "codex:grandchild")
        self.assertEqual(worker["children"][0]["logical_harness"], "t3code")
        nodes = [result["tree"]]
        ids: list[str] = []
        while nodes:
            node = nodes.pop()
            ids.append(node["id"])
            nodes.extend(node["children"])
        self.assertEqual(len(ids), len(set(ids)))
        direct = orchestration_tree(self.conn, "codex:grandchild")
        assert direct is not None
        self.assertEqual(direct["root_id"], "t3code:root")
        self.assertEqual(direct["tree"]["id"], "t3code:root")

    def test_link_only_worker_is_a_single_tree_and_overview_child(self) -> None:
        self.conn.executescript(
            """
            INSERT INTO sessions (id, harness, external_id, started_at)
            VALUES
              ('t3code:link-root', 't3code', 'link-root',
               '2026-08-10T10:04:00+00:00'),
              ('codex:link-worker', 'codex', 'link-worker',
               '2026-08-10T10:05:00+00:00');
            INSERT INTO session_links
              (source_session_id, target_session_id, link_type,
               target_harness, target_external_id, link_role,
               confidence, evidence_json)
            VALUES ('t3code:link-root', 'codex:link-worker',
                    'provider_backing', 'codex', 'link-worker', 'worker',
                    'observed', '{}');
            """
        )
        self.conn.commit()
        tree = orchestration_tree(self.conn, "t3code:link-root")
        assert tree is not None
        children = tree["tree"]["children"]
        self.assertEqual([child["id"] for child in children], ["codex:link-worker"])
        self.assertEqual(children[0]["relationship"], "provider_worker")
        self.assertEqual(children[0]["logical_harness"], "t3code")

        overview = orchestration_overview(self.conn, _tr())
        rows = {item["id"]: item for item in overview["items"]}
        self.assertEqual(rows["t3code:link-root"]["child_count"], 1)
        self.assertEqual(overview["child_sessions"], 4)

    def test_logical_harness_filter_hides_backing_and_attributes_workers(self) -> None:
        t3_rows = {
            item["id"]: item
            for item in list_sessions_v2(self.conn, _tr(), harness=["t3code"])[
                "items"
            ]
        }
        self.assertEqual(
            set(t3_rows),
            {"t3code:root"},
        )
        self.assertEqual(t3_rows["t3code:root"]["runtime_harness"], "codex")

        codex_rows = list_sessions_v2(self.conn, _tr(), harness=["codex"])[
            "items"
        ]
        self.assertEqual([item["id"] for item in codex_rows], ["codex:unlinked"])

    def test_model_filter_uses_linked_root_transcript(self) -> None:
        self.conn.executescript(
            """
            UPDATE sessions
            SET model_canonical = 'gpt-5.5'
            WHERE id IN ('t3code:root', 't3code:child', 'codex:worker',
                         'codex:grandchild', 'codex:unlinked');
            UPDATE sessions
            SET model_canonical = 'gpt-5.6-sol'
            WHERE id = 'codex:backing';
            UPDATE sessions
            SET effort = 'high'
            WHERE id = 'codex:backing';
            UPDATE messages
            SET model_canonical = 'gpt-5.5'
            WHERE id = 't3-a';
            UPDATE messages
            SET model_canonical = 'gpt-5.6-sol'
            WHERE id = 'cx-a';
            """
        )
        self.conn.commit()
        result = list_sessions_v2(
            self.conn, _tr(), model=["gpt-5.6-sol"]
        )
        self.assertEqual([item["id"] for item in result["items"]], ["t3code:root"])
        self.assertEqual(
            result["items"][0]["transcript_session_id"], "codex:backing"
        )
        self.assertEqual(result["items"][0]["model"], "gpt-5.6-sol")
        self.assertEqual(result["items"][0]["effort"], "high")
        by_effort = list_sessions_v2(self.conn, _tr(), effort=["high"])
        self.assertEqual([item["id"] for item in by_effort["items"]], ["t3code:root"])

    def test_fallback_keeps_historical_backing_but_uses_current_t3_episode(self) -> None:
        self.conn.executescript(
            """
            UPDATE sessions
            SET model = 'grok-4.5', model_canonical = 'grok-4.5',
                provider = 'xai', agent_profile = 'grok'
            WHERE id = 't3code:root';
            UPDATE sessions
            SET provider = 'openai', agent_profile = 'codex'
            WHERE id = 'codex:backing';
            INSERT INTO messages
              (id, session_id, seq, role, timestamp, model, model_canonical,
               provider, agent_profile, text, content_hash)
            VALUES
              ('t3-grok-u', 't3code:root', 3, 'user',
               '2026-08-10T10:09:30+00:00', NULL, NULL, NULL, NULL,
               'continue after fallback', 't3-grok-u'),
              ('t3-grok-a', 't3code:root', 4, 'assistant',
               '2026-08-10T10:09:31+00:00', 'grok-4.5', 'grok-4.5', 'xai', 'grok',
               'post-switch Grok result', 't3-grok-a');
            INSERT INTO token_usage
              (id, session_id, seq, granularity, usage_source, model,
               input_tokens, output_tokens, total_tokens)
            VALUES ('fallback-token', 't3code:root', 1, 'session_cumulative',
                    'fixture', 'grok-4.5', 30, 10, 40);
            """
        )
        self.conn.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
        self.conn.commit()

        projection = logical_projection(self.conn, "t3code:root", "t3code")
        self.assertEqual(projection["logical_harness"], "t3code")
        self.assertEqual(projection["runtime_harness"], "t3code")
        self.assertIsNone(projection["transcript_session_id"])
        self.assertIn("codex:backing", provider_root_shadow_ids(self.conn))

        listed = {item["id"]: item for item in list_sessions_v2(self.conn, _tr())["items"]}
        self.assertNotIn("codex:backing", listed)
        self.assertEqual(listed["t3code:root"]["model"], "grok-4.5")
        self.assertEqual(listed["t3code:root"]["message_count"], 6)
        self.assertEqual(listed["t3code:root"]["runtime_harness"], "t3code")

        detail = session_detail_v2(self.conn, "t3code:root")
        assert detail is not None
        self.assertEqual(detail["transcript"]["id"], "t3code:root")
        self.assertEqual(detail["messages"][-1]["text"], "post-switch Grok result")
        self.assertEqual(detail["session"]["model"], "grok-4.5")

        hit = search_messages(self.conn, _tr(), q="post-switch")
        self.assertEqual(hit["total"], 1)
        self.assertEqual(hit["items"][0]["session_id"], "t3code:root")
        self.assertEqual(hit["items"][0]["physical_session_id"], "t3code:root")
        self.assertEqual(hit["items"][0]["harness"], "t3code")

        token_detail = tokens.session_tokens(self.conn, "t3code:root")
        assert token_detail is not None
        self.assertEqual(token_detail["transcript_session_id"], "t3code:root")
        self.assertEqual(token_detail["totals"]["total_tokens"], 40)

        visible = visible_logical_sessions(
            self.conn,
            self.conn.execute("SELECT id, harness FROM sessions ORDER BY id").fetchall(),
        )
        metric_ids = {item.session_id: item.metric_session_id for item in visible}
        self.assertEqual(metric_ids["t3code:root"], "t3code:root")
        self.assertNotIn("codex:backing", metric_ids)
        ledger = ledger_counts(self.conn, _tr())
        self.assertEqual(ledger["sessions"], 5)
        self.assertEqual(ledger["messages"], 7)

    def test_intervening_provider_episode_prevents_later_codex_reuse(self) -> None:
        self.conn.executescript(
            """
            UPDATE sessions
            SET provider = 'openai', agent_profile = 'codex',
                model = 'gpt-5.6-sol', model_canonical = 'gpt-5.6-sol'
            WHERE id = 't3code:root';
            UPDATE sessions
            SET provider = 'openai', agent_profile = 'codex'
            WHERE id = 'codex:backing';
            INSERT INTO messages
              (id, session_id, seq, role, timestamp, model, model_canonical,
               provider, agent_profile, text, content_hash)
            VALUES ('intervening-grok', 't3code:root', 3, 'assistant',
                    '2026-08-10T10:09:30+00:00', 'grok-4.5', 'grok-4.5',
                    'xai', 'grok', 'temporary fallback response', 'intervening-grok');
            """
        )
        self.conn.commit()
        projection = logical_projection(self.conn, "t3code:root", "t3code")
        self.assertEqual(projection["runtime_harness"], "t3code")
        self.assertIsNone(projection["transcript_session_id"])

    def test_open_or_overlapping_backing_cannot_hide_grok_episode(self) -> None:
        self.conn.executescript(
            """
            UPDATE sessions
            SET provider = 'openai', agent_profile = 'codex',
                model = 'gpt-5.6-sol', model_canonical = 'gpt-5.6-sol'
            WHERE id = 't3code:root';
            UPDATE sessions
            SET provider = 'openai', agent_profile = 'codex', ended_at = NULL
            WHERE id = 'codex:backing';
            INSERT INTO messages
              (id, session_id, seq, role, timestamp, model, model_canonical,
               provider, agent_profile, text, content_hash)
            VALUES ('overlapping-grok', 't3code:root', 3, 'assistant',
                    '2026-08-10T10:01:30+00:00', 'grok-4.5', 'grok-4.5',
                    'xai', 'grok', 'overlapping fallback response', 'overlapping-grok');
            """
        )
        self.conn.commit()
        projection = logical_projection(self.conn, "t3code:root", "t3code")
        self.assertEqual(projection["runtime_harness"], "t3code")
        self.assertIsNone(projection["transcript_session_id"])

    def test_multiple_backings_do_not_select_a_transcript(self) -> None:
        self.conn.execute(
            """
            INSERT INTO sessions
              (id, harness, external_id, started_at, repo)
            VALUES ('codex:other', 'codex', 'other', '2026-08-10T10:03:00+00:00', '/repo')
            """
        )
        self.conn.execute(
            """
            INSERT INTO session_links
              (source_session_id, target_session_id, link_type,
               target_harness, target_external_id, link_role,
               confidence, evidence_json)
            VALUES ('t3code:root', 'codex:other', 'provider_backing',
                    'codex', 'other', 'root', 'observed', '{}')
            """
        )
        self.conn.commit()
        projection = logical_projection(self.conn, "t3code:root", "t3code")
        self.assertEqual(projection["runtime_harness"], "t3code")
        self.assertIsNone(projection["transcript_session_id"])
        self.assertEqual(len(projection["provider_backings"]), 2)
        self.assertEqual(
            provider_root_shadow_ids(self.conn),
            {"codex:backing", "codex:other"},
        )
        listed = {
            item["id"] for item in list_sessions_v2(self.conn, _tr())["items"]
        }
        self.assertIn("t3code:root", listed)
        self.assertNotIn("codex:backing", listed)
        self.assertNotIn("codex:other", listed)
        visible = visible_logical_sessions(
            self.conn,
            self.conn.execute("SELECT id, harness FROM sessions").fetchall(),
        )
        self.assertNotIn("codex:backing", {item.session_id for item in visible})
        self.assertNotIn("codex:other", {item.session_id for item in visible})

    def test_worker_backing_does_not_replace_explicit_root_transcript(self) -> None:
        self.conn.execute(
            """
            INSERT INTO session_links
              (source_session_id, target_session_id, link_type,
               target_harness, target_external_id, link_role,
               confidence, evidence_json)
            VALUES ('t3code:root', 'codex:worker', 'provider_backing',
                    'codex', 'worker', 'worker', 'observed', '{}')
            """
        )
        self.conn.commit()
        projection = logical_projection(self.conn, "t3code:root", "t3code")
        self.assertEqual(projection["transcript_session_id"], "codex:backing")
        self.assertEqual(
            [row["link_role"] for row in projection["provider_backings"]],
            ["root", "worker"],
        )

    def test_single_explicit_worker_link_is_not_a_root_transcript(self) -> None:
        self.conn.executescript(
            """
            INSERT INTO sessions (id, harness, external_id, started_at)
            VALUES
              ('t3code:worker-only', 't3code', 'worker-only',
               '2026-08-10T10:04:00+00:00'),
              ('codex:single-worker', 'codex', 'single-worker',
               '2026-08-10T10:05:00+00:00');
            INSERT INTO messages (id, session_id, seq, role, text, content_hash)
            VALUES ('single-worker-a', 'codex:single-worker', 1, 'assistant',
                    'worker response', 'single-worker-a');
            INSERT INTO session_links
              (source_session_id, target_session_id, link_type,
               target_harness, target_external_id, link_role,
               confidence, evidence_json)
            VALUES ('t3code:worker-only', 'codex:single-worker',
                    'provider_backing', 'codex', 'single-worker', 'worker',
                    'observed', '{}');
            """
        )
        self.conn.commit()
        projection = logical_projection(self.conn, "t3code:worker-only", "t3code")
        self.assertEqual(projection["runtime_harness"], "t3code")
        self.assertIsNone(projection["transcript_session_id"])
        self.assertNotIn(
            "codex:single-worker", provider_root_shadow_ids(self.conn)
        )
        rows = {
            item["id"]: item for item in list_sessions_v2(self.conn, _tr())["items"]
        }
        self.assertIn("t3code:worker-only", rows)
        self.assertNotIn("codex:single-worker", rows)
        self.assertEqual(rows["t3code:worker-only"]["message_count"], 1)
        self.assertEqual(rows["t3code:worker-only"]["child_count"], 1)

    def test_unknown_singleton_link_uses_legacy_root_fallback(self) -> None:
        self.conn.executescript(
            """
            INSERT INTO sessions (id, harness, external_id, started_at)
            VALUES
              ('t3code:legacy', 't3code', 'legacy',
               '2026-08-10T10:04:00+00:00'),
              ('codex:legacy', 'codex', 'legacy',
               '2026-08-10T10:05:00+00:00');
            INSERT INTO session_links
              (source_session_id, target_session_id, link_type,
               target_harness, target_external_id, link_role,
               confidence, evidence_json)
            VALUES ('t3code:legacy', 'codex:legacy', 'provider_backing',
                    'codex', 'legacy', 'unknown', 'observed', '{}');
            """
        )
        self.conn.commit()
        projection = logical_projection(self.conn, "t3code:legacy", "t3code")
        self.assertEqual(projection["transcript_session_id"], "codex:legacy")
        self.assertIn("codex:legacy", provider_root_shadow_ids(self.conn))

    def test_v025_backfills_missing_roles_as_unknown(self) -> None:
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE TABLE session_links (source_session_id TEXT)")
            conn.execute("INSERT INTO session_links VALUES ('legacy')")
            apply_v025(conn)
            self.assertEqual(
                conn.execute("SELECT link_role FROM session_links").fetchone()[0],
                "unknown",
            )
        finally:
            conn.close()

    def test_new_session_links_default_to_unknown_role(self) -> None:
        columns = {
            str(column["name"]): column
            for column in self.conn.execute("PRAGMA table_info(session_links)")
        }
        self.assertEqual(columns["link_role"]["dflt_value"], "'unknown'")

    def test_shadow_helpers_preserve_workers_and_mark_root_backings(self) -> None:
        self.assertEqual(
            provider_backing_shadow_ids(self.conn),
            {"codex:backing", "codex:worker", "codex:grandchild"},
        )
        self.assertEqual(provider_root_shadow_ids(self.conn), {"codex:backing"})
        sql = provider_backing_exclusion_sql("s")
        count = self.conn.execute(
            f"SELECT COUNT(*) AS c FROM sessions s WHERE {sql}"
        ).fetchone()["c"]
        self.assertEqual(count, 1)

    def test_multi_owner_backing_stays_physical_and_explicit(self) -> None:
        self.conn.execute(
            """
            INSERT INTO sessions (id, harness, external_id, started_at)
            VALUES ('t3code:other-root', 't3code', 'other-root',
                    '2026-08-10T10:04:00+00:00')
            """
        )
        self.conn.execute(
            """
            INSERT INTO session_links
              (source_session_id, target_session_id, link_type,
               target_harness, target_external_id, confidence, evidence_json)
            VALUES ('t3code:other-root', 'codex:backing', 'provider_backing',
                    'codex', 'backing', 'observed', '{}')
            """
        )
        self.conn.commit()
        projection = logical_projection(self.conn, "codex:backing", "codex")
        self.assertEqual(projection["logical_harness"], "codex")
        self.assertIsNone(projection["orchestrator_session_id"])
        self.assertNotIn("codex:backing", provider_backing_shadow_ids(self.conn))
        root_projection = logical_projection(self.conn, "t3code:root", "t3code")
        other_projection = logical_projection(
            self.conn, "t3code:other-root", "t3code"
        )
        self.assertIsNone(root_projection["transcript_session_id"])
        self.assertIsNone(other_projection["transcript_session_id"])
        rows = self.conn.execute(
            "SELECT id, harness FROM sessions ORDER BY id"
        ).fetchall()
        visible = visible_logical_sessions(self.conn, rows)
        visible_ids = {row.session_id for row in visible}
        self.assertIn("t3code:root", visible_ids)
        self.assertIn("t3code:other-root", visible_ids)
        self.assertIn("codex:backing", visible_ids)
        listed = {
            row["id"]: row
            for row in list_sessions_v2(self.conn, _tr())["items"]
        }
        self.assertIn("codex:backing", listed)
        self.assertIsNone(listed["t3code:root"]["transcript_session_id"])
        self.assertIsNone(listed["t3code:other-root"]["transcript_session_id"])

    def test_orchestration_overview_collapses_backing_root_once(self) -> None:
        self.conn.execute(
            """
            UPDATE sessions
            SET model = 'backing-model', model_canonical = 'backing-model', effort = 'high'
            WHERE id = 'codex:backing'
            """
        )
        self.conn.commit()
        result = orchestration_overview(self.conn, _tr())
        rows = {item["id"]: item for item in result["items"]}
        self.assertEqual(result["supervisor_roots"], 2)
        self.assertNotIn("codex:backing", rows)
        root = rows["t3code:root"]
        self.assertEqual(root["harness"], "t3code")
        self.assertEqual(root["logical_harness"], "t3code")
        self.assertEqual(root["runtime_harness"], "codex")
        self.assertEqual(root["orchestrator_session_id"], "t3code:root")
        self.assertEqual(root["transcript_session_id"], "codex:backing")
        self.assertEqual(root["model"], "backing-model")
        self.assertEqual(root["effort"], "high")
        self.assertEqual(root["child_count"], 2)
        self.assertEqual(root["message_count"], 2)
        worker = rows["codex:worker"]
        self.assertEqual(worker["logical_harness"], "t3code")
        self.assertEqual(worker["runtime_harness"], "codex")
        self.assertEqual(worker["orchestrator_session_id"], "t3code:root")

    def test_overview_rehydrates_t3_root_from_backing_only(self) -> None:
        self.conn.execute("DELETE FROM sessions WHERE id = 't3code:child'")
        self.conn.commit()
        result = orchestration_overview(self.conn, _tr())
        rows = {item["id"]: item for item in result["items"]}
        self.assertEqual(result["supervisor_roots"], 2)
        self.assertIn("t3code:root", rows)
        self.assertNotIn("codex:backing", rows)
        root = rows["t3code:root"]
        self.assertEqual(root["harness"], "t3code")
        self.assertEqual(root["runtime_harness"], "codex")
        self.assertEqual(root["child_count"], 1)
        self.assertEqual(root["message_count"], 2)

    def test_projection_context_scans_identity_once_per_operation(self) -> None:
        self.conn.executemany(
            """
            INSERT INTO sessions (id, harness, external_id, started_at)
            VALUES (?, 'codex', ?, '2026-08-10T08:00:00+00:00')
            """,
            [(f"codex:bulk-{i}", f"bulk-{i}") for i in range(700)],
        )
        self.conn.commit()

        def identity_scans(run) -> tuple[int, int]:
            statements: list[str] = []
            self.conn.set_trace_callback(statements.append)
            try:
                run()
            finally:
                self.conn.set_trace_callback(None)
            lower = [statement.lower() for statement in statements]
            return (
                sum("from session_links l" in statement for statement in lower),
                sum(
                    "select id, harness, external_id, parent_session_id from sessions"
                    in statement
                    for statement in lower
                ),
            )

        self.assertEqual(identity_scans(lambda: list_sessions_v2(self.conn, _tr())), (1, 1))
        self.assertEqual(identity_scans(lambda: session_detail_v2(self.conn, "t3code:root")), (1, 1))
        self.assertEqual(identity_scans(lambda: orchestration_tree(self.conn, "t3code:root")), (1, 1))

    def test_bare_parent_external_id_does_not_cross_harnesses(self) -> None:
        self.conn.execute(
            """
            INSERT INTO sessions
              (id, harness, external_id, parent_session_id, started_at)
            VALUES
              ('claude:collision-child', 'claude', 'collision-child', 'backing',
               '2026-08-10T10:04:00+00:00')
            """
        )
        self.conn.commit()

        projection = logical_projection(
            self.conn, "claude:collision-child", harness="claude"
        )

        self.assertEqual(projection["logical_harness"], "claude")
        self.assertIsNone(projection["orchestrator_session_id"])
        self.assertNotIn(
            "claude:collision-child", provider_backing_shadow_ids(self.conn)
        )


if __name__ == "__main__":
    unittest.main()
