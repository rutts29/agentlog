from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from agentlog.api.app import create_app
from agentlog.api.descriptive import (
    list_sessions_v2,
    orchestration_tree,
    session_detail_v2,
    session_facets,
)
from agentlog.api.ranges import TimeRange, session_time_clause
from agentlog.db.schema import connect, init_db
from agentlog.session_identity import INTERNAL_APPROVAL_GUARDIAN_THREAD_SOURCE


def _range(start: datetime | None = None) -> TimeRange:
    return TimeRange(
        key="custom" if start else "all",
        start=start,
        end=datetime(2026, 8, 12, tzinfo=timezone.utc),
        prev_start=None,
        prev_end=None,
    )


class SessionsTreeBrowsingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "sessions.db"
        self.conn = connect(self.path)
        init_db(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def session(
        self,
        session_id: str,
        *,
        harness: str = "codex",
        external_id: str | None = None,
        parent: str | None = None,
        started_at: str = "2026-08-10T10:00:00+00:00",
        ended_at: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        repo: str | None = None,
        branch: str | None = None,
        originator: str | None = None,
        thread_source: str | None = None,
        workflow_group_id: str | None = None,
        workflow_group_label: str | None = None,
        workflow_group_position: int | None = None,
        transcript_storage: str = "legacy_materialized",
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO sessions
              (id, harness, external_id, parent_session_id, started_at,
               ended_at, model, model_canonical, effort, repo, branch,
               originator, thread_source, workflow_group_id,
               workflow_group_label, workflow_group_position, transcript_storage)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                harness,
                external_id or session_id.split(":", 1)[-1],
                parent,
                started_at,
                ended_at,
                model,
                model,
                effort,
                repo,
                branch,
                originator,
                thread_source,
                workflow_group_id,
                workflow_group_label,
                workflow_group_position,
                transcript_storage,
            ),
        )

    def assistant_message(
        self, session_id: str, message_id: str, *, model: str, text: str
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO messages
              (id, session_id, seq, role, model, model_canonical, text,
               content_hash)
            VALUES (?, ?, 1, 'assistant', ?, ?, ?, ?)
            """,
            (message_id, session_id, model, model, text, message_id),
        )

    def test_recent_descendant_qualifies_old_root_and_scopes_filters(self) -> None:
        self.session(
            "codex:old-root",
            external_id="old-root",
            started_at="2026-06-01T08:00:00+00:00",
            ended_at="2026-06-01T08:10:00+00:00",
            model="gpt-5.5",
            effort="low",
            repo="/repo/old-root-project",
            branch="old-branch",
        )
        self.session(
            "codex:stale-worker",
            external_id="stale-worker",
            parent="old-root",
            started_at="2026-06-02T08:00:00+00:00",
            ended_at="2026-06-02T08:10:00+00:00",
            model="gpt-5.4",
            effort="medium",
            repo="/repo/stale-project",
            branch="old-branch",
        )
        self.session(
            "codex:recent-worker",
            external_id="recent-worker",
            parent="old-root",
            started_at="2026-08-10T12:00:00+00:00",
            ended_at="2026-08-10T12:30:00+00:00",
            model="gpt-5.6-sol",
            effort="high",
            repo="/repo/child-only-project",
            branch="new-branch",
        )
        self.assistant_message(
            "codex:recent-worker",
            "worker-answer",
            model="gpt-5.6-sol",
            text="local worker result",
        )
        self.conn.commit()
        tr = _range(datetime(2026, 8, 1, tzinfo=timezone.utc))

        result = list_sessions_v2(self.conn, tr)
        self.assertEqual([item["id"] for item in result["items"]], ["codex:old-root"])
        self.assertEqual(result["count_scope"], "full_conversation")
        self.assertIn("counts cover each full conversation", result["note"])
        root = result["items"][0]
        self.assertEqual(root["started_at"], "2026-06-01T08:00:00+00:00")
        self.assertEqual(root["activity_at"], "2026-08-10T12:30:00+00:00")
        self.assertEqual(
            root["latest_descendant_at"], "2026-08-10T12:30:00+00:00"
        )
        self.assertFalse(root["matched_in_descendant"])
        self.assertEqual(root["matching_descendant_count"], 0)
        for kwargs in (
            {"q": "recent-worker"},
            {"model": ["gpt-5.6-sol"]},
            {"effort": ["high"]},
            {"project": ["child-only-project"]},
            {"branch": ["new-branch"]},
            {"harness": ["codex"]},
        ):
            with self.subTest(kwargs=kwargs):
                filtered = list_sessions_v2(self.conn, tr, **kwargs)
                self.assertEqual(
                    [item["id"] for item in filtered["items"]],
                    ["codex:old-root"],
                )
        branch_match = list_sessions_v2(
            self.conn, tr, q="recent-worker"
        )["items"][0]
        self.assertTrue(branch_match["matched_in_descendant"])
        self.assertEqual(branch_match["matching_descendant_count"], 1)
        for kwargs in (
            {"q": "old-root"},
            {"q": "stale-worker"},
            {"model": ["gpt-5.5"]},
            {"model": ["gpt-5.4"]},
            {"effort": ["low"]},
            {"effort": ["medium"]},
            {"project": ["old-root-project"]},
            {"project": ["stale-project"]},
            {"branch": ["old-branch"]},
        ):
            with self.subTest(out_of_range=kwargs):
                self.assertEqual(list_sessions_v2(self.conn, tr, **kwargs)["items"], [])

        facets = session_facets(self.conn, tr, view="roots")
        facet_values = {
            key: {item["value"] for item in facets[key]}
            for key in ("model", "effort", "branch", "project")
        }
        self.assertEqual(facet_values["model"], {"gpt-5.6-sol"})
        self.assertEqual(facet_values["effort"], {"high"})
        self.assertEqual(facet_values["branch"], {"new-branch"})
        self.assertEqual(facet_values["project"], {"child-only-project"})

    def test_workflow_groups_sort_real_children_without_changing_counts(self) -> None:
        self.session("grok:root", harness="grok", external_id="root")
        self.session(
            "grok:reviewer",
            harness="grok",
            external_id="reviewer",
            parent="root",
            thread_source="subagent",
            started_at="2026-08-10T10:01:00+00:00",
        )
        self.session(
            "grok:second",
            harness="grok",
            external_id="second",
            parent="root",
            thread_source="subagent",
            workflow_group_id="later",
            workflow_group_label="Later sweep",
            workflow_group_position=20,
            started_at="2026-08-10T10:02:00+00:00",
        )
        self.session(
            "grok:first",
            harness="grok",
            external_id="first",
            parent="root",
            thread_source="subagent",
            workflow_group_id="early",
            workflow_group_label="Early sweep",
            workflow_group_position=10,
            started_at="2026-08-10T10:03:00+00:00",
        )
        self.conn.commit()

        tree = orchestration_tree(self.conn, "grok:root")
        assert tree is not None
        self.assertEqual(
            [node["id"] for node in tree["tree"]["children"]],
            ["grok:first", "grok:second", "grok:reviewer"],
        )
        self.assertEqual(tree["tree"]["descendant_count"], 3)
        self.assertEqual(tree["bounds"]["total_node_count"], 4)
        self.assertEqual(
            tree["tree"]["children"][0]["workflow_group_label"],
            "Early sweep",
        )
        self.assertIsNone(tree["tree"]["children"][2]["workflow_group_id"])

        detail = session_detail_v2(self.conn, "grok:root")
        assert detail is not None
        self.assertEqual(detail["anatomy"]["child_count"], 3)
        self.assertEqual(
            [child["id"] for child in detail["children"]],
            ["grok:first", "grok:second", "grok:reviewer"],
        )

    def test_mixed_offsets_use_instant_range_for_roots_children_and_facets(self) -> None:
        self.session(
            "codex:offset-parent",
            external_id="offset-parent",
            started_at="2026-06-01T08:00:00+00:00",
            branch="parent-before-range",
        )
        self.session(
            "codex:offset-before",
            external_id="offset-before",
            parent="offset-parent",
            started_at="2026-08-01T05:29:59+05:30",
            branch="old-offset-branch",
        )
        self.session(
            "codex:offset-inside",
            external_id="offset-inside",
            parent="offset-parent",
            started_at="2026-07-31T20:30:00-04:30",
            branch="new-offset-branch",
        )
        self.session(
            "codex:start-utc",
            external_id="start-utc",
            started_at="2026-08-01T00:00:00+00:00",
            branch="start-utc",
        )
        self.session(
            "codex:start-offset",
            external_id="start-offset",
            started_at="2026-08-01T05:30:00+05:30",
            branch="start-offset",
        )
        self.session(
            "codex:end-offset",
            external_id="end-offset",
            started_at="2026-08-02T05:30:00+05:30",
            branch="end-offset",
        )
        self.conn.commit()
        tr = TimeRange(
            key="custom",
            start=datetime(2026, 8, 1, tzinfo=timezone.utc),
            end=datetime(2026, 8, 2, tzinfo=timezone.utc),
            prev_start=None,
            prev_end=None,
        )

        result = list_sessions_v2(self.conn, tr)
        self.assertEqual(
            {item["id"] for item in result["items"]},
            {
                "codex:offset-parent",
                "codex:start-utc",
                "codex:start-offset",
            },
        )
        self.assertEqual(
            [
                item["id"]
                for item in list_sessions_v2(
                    self.conn, tr, branch=["new-offset-branch"]
                )["items"]
            ],
            ["codex:offset-parent"],
        )
        self.assertEqual(
            list_sessions_v2(
                self.conn, tr, branch=["old-offset-branch"]
            )["items"],
            [],
        )

        facets = session_facets(self.conn, tr, view="roots")
        branches = {item["value"] for item in facets["branch"]}
        self.assertEqual(
            branches,
            {"new-offset-branch", "start-utc", "start-offset"},
        )

        where, params = session_time_clause(tr)
        plan = self.conn.execute(
            f"EXPLAIN QUERY PLAN SELECT s.id FROM sessions s WHERE {where}",
            params,
        ).fetchall()
        self.assertTrue(
            any("idx_sessions_started" in str(row["detail"]) for row in plan)
        )

    def test_root_facets_count_conversations_with_descendant_values_once(self) -> None:
        self.session("codex:root", model="gpt-5.5", repo="/repo/root")
        self.session(
            "codex:worker-a",
            parent="root",
            model="gpt-5.6-sol",
            effort="high",
            repo="/repo/worker",
        )
        self.session(
            "codex:worker-b",
            parent="root",
            model="gpt-5.6-sol",
            effort="high",
            repo="/repo/worker",
        )
        self.conn.commit()

        facets = session_facets(self.conn, _range(), view="roots")
        models = {item["value"]: item["count"] for item in facets["model"]}
        efforts = {item["value"]: item["count"] for item in facets["effort"]}
        projects = {item["value"]: item["count"] for item in facets["project"]}
        self.assertEqual(models["gpt-5.6-sol"], 1)
        self.assertEqual(efforts["high"], 1)
        self.assertEqual(projects["worker"], 1)

        self.conn.close()
        client = TestClient(create_app(self.path))
        response = client.get("/api/facets", params={"range": "all", "view": "roots"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            {item["value"]: item["count"] for item in response.json()["model"]}[
                "gpt-5.6-sol"
            ],
            1,
        )
        invalid = client.get("/api/facets", params={"range": "all", "view": "flat"})
        self.assertEqual(invalid.status_code, 400)
        self.conn = connect(self.path)

    def test_hidden_backing_is_the_range_filter_and_facet_witness(self) -> None:
        self.session(
            "t3code:stale-owner",
            harness="t3code",
            external_id="stale-owner",
            started_at="2026-06-01T08:00:00+00:00",
            model="gpt-5.5",
            repo="/repo/stale-owner-project",
            branch="old-owner-branch",
        )
        self.session(
            "codex:recent-backing",
            external_id="recent-backing",
            started_at="2026-08-10T12:00:00+00:00",
            model="gpt-5.6-sol",
            effort="high",
            repo="/repo/recent-backing-project",
            branch="new-backing-branch",
        )
        self.conn.execute(
            """
            INSERT INTO session_links
              (source_session_id, target_session_id, link_type,
               target_harness, target_external_id, link_role,
               confidence, evidence_json)
            VALUES (?, ?, 'provider_backing', 'codex', ?, 'root',
                    'observed', '{}')
            """,
            (
                "t3code:stale-owner",
                "codex:recent-backing",
                "recent-backing",
            ),
        )
        self.conn.commit()
        tr = _range(datetime(2026, 8, 1, tzinfo=timezone.utc))

        recent = list_sessions_v2(
            self.conn, tr, branch=["new-backing-branch"]
        )
        self.assertEqual(
            [item["id"] for item in recent["items"]],
            ["t3code:stale-owner"],
        )
        self.assertTrue(recent["items"][0]["matched_in_descendant"])
        self.assertEqual(
            list_sessions_v2(
                self.conn, tr, branch=["old-owner-branch"]
            )["items"],
            [],
        )
        self.assertEqual(
            list_sessions_v2(self.conn, tr, q="stale-owner-project")["items"],
            [],
        )

        facets = session_facets(self.conn, tr, view="roots")
        self.assertEqual(
            {item["value"] for item in facets["branch"]},
            {"new-backing-branch"},
        )
        self.assertEqual(
            {item["value"] for item in facets["project"]},
            {"recent-backing-project"},
        )

    def test_unresolved_parent_stays_visible_as_orphan_root(self) -> None:
        self.session("claude:orphan", harness="claude", parent="missing-parent")
        self.conn.commit()

        result = list_sessions_v2(self.conn, _range())
        self.assertEqual([item["id"] for item in result["items"]], ["claude:orphan"])
        self.assertTrue(result["items"][0]["is_orphan"])
        tree = orchestration_tree(self.conn, "claude:orphan")
        assert tree is not None
        self.assertEqual(tree["root_id"], "claude:orphan")
        self.assertTrue(tree["tree"]["is_orphan"])

    def test_originator_only_t3_codex_root_remains_navigable(self) -> None:
        self.session(
            "codex:t3-shadow-root",
            external_id="t3-shadow-root",
            originator="t3code_desktop",
        )
        self.session(
            "codex:t3-shadow-worker",
            external_id="t3-shadow-worker",
            parent="t3-shadow-root",
        )
        self.conn.commit()

        result = list_sessions_v2(self.conn, _range(), harness=["t3code"])
        self.assertEqual(
            [item["id"] for item in result["items"]], ["codex:t3-shadow-root"]
        )
        root = result["items"][0]
        self.assertEqual(root["navigation_id"], "codex:t3-shadow-root")
        self.assertEqual(root["logical_harness"], "t3code")
        self.assertEqual(root["runtime_harness"], "codex")
        self.assertIsNone(root["orchestrator_session_id"])
        self.assertEqual(root["descendant_count"], 1)

        tree = orchestration_tree(self.conn, "codex:t3-shadow-worker")
        assert tree is not None
        self.assertEqual(tree["root_id"], "codex:t3-shadow-root")
        self.assertEqual(tree["tree"]["children"][0]["logical_harness"], "t3code")

    def test_detail_exposes_non_counting_inherited_provenance(self) -> None:
        self.session("codex:parent", external_id="parent")
        self.session("codex:worker", external_id="worker", parent="parent")
        self.assistant_message(
            "codex:worker", "local-message", model="gpt-5.6-sol", text="local only"
        )
        self.conn.execute(
            """
            UPDATE sessions
            SET inherited_message_count = 7,
                inherited_record_count = 21,
                fork_context_status = 'verified_parent',
                fork_context_boundary = 'response_item:42'
            WHERE id = 'codex:worker'
            """
        )
        self.conn.commit()

        detail = session_detail_v2(self.conn, "codex:worker")
        assert detail is not None
        self.assertEqual(
            detail["inherited_context"],
            {
                "status": "verified_parent",
                "message_count": 7,
                "record_count": 21,
                "boundary": "response_item:42",
                "parent_navigation_id": "codex:parent",
            },
        )
        self.assertEqual(detail["session"]["parent_navigation_id"], "codex:parent")
        self.assertEqual([message["text"] for message in detail["messages"]], ["local only"])

    def test_detail_rejects_foreign_raw_parent_for_navigation(self) -> None:
        self.session("t3code:owner", harness="t3code", external_id="owner")
        self.session(
            "codex:foreign-child",
            external_id="foreign-child",
            parent="t3code:owner",
        )
        self.conn.commit()

        detail = session_detail_v2(self.conn, "codex:foreign-child")
        assert detail is not None
        self.assertIsNone(detail["session"]["parent_navigation_id"])
        self.assertEqual(detail["session"]["parent_session_id"], "t3code:owner")

    def test_cross_harness_collision_and_overlapping_links_do_not_duplicate(self) -> None:
        self.session("t3code:root", harness="t3code", external_id="root")
        self.session("codex:backing", external_id="backing")
        self.session("codex:worker", external_id="worker", parent="backing")
        self.session("codex:grandchild", external_id="grandchild", parent="worker")
        self.session("codex:other-root", external_id="other-root")
        self.session(
            "codex:linked-worker",
            external_id="linked-worker",
            parent="other-root",
        )
        self.session(
            "claude:collision",
            harness="claude",
            external_id="collision",
            parent="backing",
        )
        self.session(
            "cursor:qualified-collision",
            harness="cursor",
            external_id="qualified-collision",
            parent="codex:backing",
        )
        self.conn.executemany(
            """
            INSERT INTO session_links
              (source_session_id, target_session_id, link_type,
               target_harness, target_external_id, link_role,
               confidence, evidence_json)
            VALUES ('t3code:root', ?, 'provider_backing', 'codex', ?, ?,
                    'observed', '{}')
            """,
            (
                ("codex:backing", "backing", "root"),
                ("codex:worker", "worker", "worker"),
                ("codex:grandchild", "grandchild", "worker"),
                ("codex:linked-worker", "linked-worker", "worker"),
            ),
        )
        self.conn.commit()

        tree = orchestration_tree(self.conn, "t3code:root")
        assert tree is not None
        ids: list[str] = []
        pending = [tree["tree"]]
        while pending:
            node = pending.pop()
            ids.append(node["id"])
            pending.extend(node["children"])
        self.assertEqual(ids.count("codex:worker"), 1)
        self.assertEqual(ids.count("codex:grandchild"), 1)
        self.assertEqual(ids.count("codex:linked-worker"), 1)
        self.assertNotIn("codex:backing", ids)
        self.assertNotIn("claude:collision", ids)
        self.assertNotIn("cursor:qualified-collision", ids)

        detail = session_detail_v2(self.conn, "t3code:root")
        assert detail is not None
        self.assertNotIn("claude:collision", {child["id"] for child in detail["children"]})
        collision_tree = orchestration_tree(self.conn, "claude:collision")
        assert collision_tree is not None
        self.assertEqual(collision_tree["root_id"], "claude:collision")
        qualified_tree = orchestration_tree(
            self.conn, "cursor:qualified-collision"
        )
        assert qualified_tree is not None
        self.assertEqual(
            qualified_tree["root_id"], "cursor:qualified-collision"
        )
        listed = {
            item["id"]: item
            for item in list_sessions_v2(self.conn, _range())["items"]
        }
        self.assertEqual(
            listed["cursor:qualified-collision"]["logical_harness"],
            "cursor",
        )
        self.assertTrue(listed["cursor:qualified-collision"]["is_orphan"])

        other_tree = orchestration_tree(self.conn, "codex:other-root")
        assert other_tree is not None
        self.assertEqual(other_tree["tree"]["children"], [])

    def test_cycle_is_broken_into_a_finite_orphan_tree(self) -> None:
        self.session("codex:cycle-a", external_id="cycle-a", parent="cycle-b")
        self.session("codex:cycle-b", external_id="cycle-b", parent="cycle-a")
        self.conn.commit()

        result = list_sessions_v2(self.conn, _range())
        self.assertEqual([item["id"] for item in result["items"]], ["codex:cycle-a"])
        self.assertTrue(result["items"][0]["is_orphan"])
        tree = orchestration_tree(self.conn, "codex:cycle-b")
        assert tree is not None
        self.assertEqual(tree["root_id"], "codex:cycle-a")
        self.assertEqual(tree["tree"]["descendant_count"], 1)

    def test_deep_tree_api_is_bounded_and_reports_omissions(self) -> None:
        self.session("codex:deep-0", external_id="deep-0")
        for index in range(1, 1050):
            self.session(
                f"codex:deep-{index}",
                external_id=f"deep-{index}",
                parent=f"deep-{index - 1}",
            )
        self.conn.commit()

        self.conn.close()
        with TestClient(create_app(self.path)) as client:
            response = client.get("/api/sessions/codex%3Adeep-1049/tree")
        self.conn = connect(self.path)

        self.assertEqual(response.status_code, 200)
        tree = response.json()
        count = 0
        pending = [tree["tree"]]
        while pending:
            node = pending.pop()
            count += 1
            pending.extend(node["children"])
        self.assertEqual(tree["root_id"], "codex:deep-0")
        self.assertEqual(count, 65)
        self.assertEqual(tree["tree"]["descendant_count"], 1049)
        self.assertEqual(
            tree["bounds"],
            {
                "max_nodes": 500,
                "max_depth": 64,
                "returned_node_count": 65,
                "total_node_count": 1050,
                "truncated": True,
                "omitted_node_count": 985,
            },
        )
        deepest = tree["tree"]
        for _ in range(64):
            deepest = deepest["children"][0]
        self.assertEqual(deepest["id"], "codex:deep-64")
        self.assertEqual(deepest["children"], [])
        self.assertTrue(deepest["children_truncated"])
        self.assertEqual(deepest["omitted_descendant_count"], 985)

    def test_tree_exposes_thread_source_for_worker_labeling(self) -> None:
        self.session("codex:thread-root", external_id="thread-root")
        self.session(
            "codex:thread-worker",
            external_id="thread-worker",
            parent="thread-root",
            thread_source="subagent",
        )
        self.conn.commit()

        tree = orchestration_tree(self.conn, "codex:thread-root")
        assert tree is not None
        self.assertEqual(tree["tree"]["children"][0]["thread_source"], "subagent")

    def test_session_list_exposes_root_thread_source(self) -> None:
        self.session(
            "grok:autonomous-root",
            harness="grok",
            thread_source="autonomous_agent_unlinked",
        )
        self.conn.commit()

        listed = list_sessions_v2(self.conn, _range())

        self.assertEqual(listed["items"][0]["thread_source"], "autonomous_agent_unlinked")

    def test_internal_approval_guardian_is_hidden_from_tree(self) -> None:
        self.session("codex:guardian-root", external_id="guardian-root")
        self.session(
            "codex:approval-guardian",
            external_id="approval-guardian",
            parent="guardian-root",
            thread_source=INTERNAL_APPROVAL_GUARDIAN_THREAD_SOURCE,
        )
        self.session(
            "codex:native-guardian",
            external_id="native-guardian",
            parent="guardian-root",
            thread_source="subagent",
        )
        self.conn.commit()

        tree = orchestration_tree(self.conn, "codex:guardian-root")

        assert tree is not None
        self.assertEqual(tree["bounds"]["total_node_count"], 2)
        self.assertEqual(
            [child["id"] for child in tree["tree"]["children"]],
            ["codex:native-guardian"],
        )

    def test_wide_tree_and_detail_children_are_bounded(self) -> None:
        self.session("codex:wide-root", external_id="wide-root")
        for index in range(600):
            self.session(
                f"codex:wide-{index}",
                external_id=f"wide-{index}",
                parent="wide-root",
            )
        self.conn.commit()

        detail = session_detail_v2(self.conn, "codex:wide-root")
        assert detail is not None
        self.assertEqual(len(detail["children"]), 200)
        self.assertEqual(detail["session"]["child_count"], 600)
        self.assertEqual(detail["anatomy"]["child_count"], 600)
        self.assertEqual(
            detail["children_bounds"],
            {
                "limit": 200,
                "returned_child_count": 200,
                "total_child_count": 600,
                "truncated": True,
                "omitted_child_count": 400,
            },
        )

        tree = orchestration_tree(self.conn, "codex:wide-root")
        assert tree is not None
        self.assertEqual(len(tree["tree"]["children"]), 499)
        self.assertEqual(tree["bounds"]["returned_node_count"], 500)
        self.assertEqual(tree["bounds"]["total_node_count"], 601)
        self.assertEqual(tree["bounds"]["omitted_node_count"], 101)
        self.assertTrue(tree["bounds"]["truncated"])
        self.assertTrue(tree["tree"]["children_truncated"])
        self.assertEqual(tree["tree"]["omitted_descendant_count"], 101)

    def test_session_list_projects_transcript_storage_without_hydration(self) -> None:
        self.conn.executescript(
            """
            INSERT INTO artifacts
              (harness, path, size, mtime_ns, content_hash, parsed_offset,
               parser_version, transcript_storage)
            VALUES
              ('t3code', '/tmp/source-owner.jsonl', 1, 1, 'source-owner', 0,
               'test', 'source_backed'),
              ('codex', '/tmp/source-owner-backing.jsonl', 1, 1,
               'source-owner-backing', 0, 'test', 'legacy_materialized'),
              ('codex', '/tmp/legacy-owner-backing.jsonl', 1, 1,
               'legacy-owner-backing', 0, 'test', 'source_backed');
            """
        )
        self.session(
            "t3code:source-owner",
            harness="t3code",
            external_id="source-owner",
            transcript_storage="source_backed",
        )
        self.session(
            "codex:source-owner-backing", external_id="source-owner-backing"
        )
        self.session(
            "t3code:legacy-owner",
            harness="t3code",
            external_id="legacy-owner",
        )
        self.session(
            "codex:legacy-owner-backing",
            external_id="legacy-owner-backing",
            transcript_storage="source_backed",
        )
        self.session("codex:legacy", external_id="legacy")
        self.conn.execute(
            """
            INSERT INTO session_links
              (source_session_id, target_session_id, link_type, target_harness,
               target_external_id, link_role)
            VALUES
              ('t3code:source-owner', 'codex:source-owner-backing',
               'provider_backing', 'codex', 'source-owner-backing', 'root'),
              ('t3code:legacy-owner', 'codex:legacy-owner-backing',
               'provider_backing', 'codex', 'legacy-owner-backing', 'root')
            """
        )
        self.conn.execute(
            "UPDATE sessions SET artifact_id = 1 WHERE id = 't3code:source-owner'"
        )
        self.conn.execute(
            "UPDATE sessions SET artifact_id = 2 "
            "WHERE id = 'codex:source-owner-backing'"
        )
        self.conn.execute(
            "UPDATE sessions SET artifact_id = 3 "
            "WHERE id = 'codex:legacy-owner-backing'"
        )
        self.conn.commit()
        self.conn.close()

        app = create_app(self.path)
        source_reader = mock.Mock(
            side_effect=AssertionError("unexpected source read")
        )
        app.state.source_transcript_reader = source_reader
        with TestClient(app) as client:
            response = client.get("/api/sessions", params={"range": "all"})
        self.conn = connect(self.path)

        self.assertEqual(response.status_code, 200)
        rows = {item["id"]: item for item in response.json()["items"]}
        self.assertIsNone(rows["t3code:source-owner"]["transcript_session_id"])
        self.assertEqual(
            rows["t3code:source-owner"]["transcript_storage"], "source_backed"
        )
        self.assertEqual(
            rows["t3code:legacy-owner"]["transcript_session_id"],
            "codex:legacy-owner-backing",
        )
        self.assertEqual(
            rows["t3code:legacy-owner"]["transcript_storage"], "source_backed"
        )
        self.assertEqual(
            rows["codex:legacy"]["transcript_storage"], "legacy_materialized"
        )
        source_reader.assert_not_called()


if __name__ == "__main__":
    unittest.main()
