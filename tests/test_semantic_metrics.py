"""Behaviour tests for the redirect/brake lead metric.

Observations are seeded through the real storage contract
(`write_ux_observations`) so that a regression in the label field, the run
contract, the denominator or root clustering fails here instead of passing
against synthetic flag shapes.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from agentlog.analysis.extractors.models import ExtractorMeta, ProcessFlags, UxObservation
from agentlog.analysis.extractors.storage import (
    PURPOSE_AUDIT,
    PURPOSE_FULL_CORPUS,
    finish_ux_run,
    publish_ux_run,
    published_ux_run_id,
    start_ux_run,
    write_ux_observations,
)
from agentlog.api.clusters import resolve_session_roots
from agentlog.api.queries import semantic_lead_metric
from agentlog.api.ranges import TimeRange
from agentlog.api.semantic import MIN_COVERAGE, redirect_cell
from agentlog.db.schema import connect, init_db

ALL_TIME = TimeRange(
    key="all",
    start=None,
    end=datetime(2030, 1, 1, tzinfo=timezone.utc),
    prev_start=None,
    prev_end=None,
)


def _observation(window_id: str, kinds: list[str], *, premature: bool = False):
    return UxObservation(
        window_id=window_id,
        extractor=ExtractorMeta(name="ux_v1", version="0.1.0", model="test-model"),
        turn_kind=list(kinds),
        user_stance="neutral",
        agent_stance="executing",
        prior_outcome="accepted_continue",
        flags=ProcessFlags(premature_action_called_out=premature),
    )


class Fixture:
    """Minimal but real corpus: sessions, messages, windows, det classifications."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        conn.execute(
            """
            INSERT INTO artifacts
            (harness, path, size, mtime_ns, content_hash, parsed_offset, parser_version)
            VALUES ('codex', '/tmp/fixture.jsonl', 1, 1, 'h', 0, '1')
            """
        )
        row = conn.execute("SELECT id FROM artifacts").fetchone()
        assert row is not None
        self.artifact_id = int(row["id"])
        self._seq = 0

    def session(
        self,
        session_id: str,
        *,
        harness: str = "codex",
        external_id: str | None = None,
        parent: str | None = None,
        model: str = "gpt-5.5",
    ) -> str:
        self.conn.execute(
            """
            INSERT INTO sessions (
                id, harness, external_id, parent_session_id, artifact_id,
                started_at, model, model_canonical
            ) VALUES (?, ?, ?, ?, ?, '2026-07-01T00:00:00+00:00', ?, ?)
            """,
            (
                session_id,
                harness,
                external_id or session_id.split(":")[-1],
                parent,
                self.artifact_id,
                model,
                model,
            ),
        )
        return session_id

    def window(
        self, session_id: str, window_id: str, *, request_kind: str = "substantive"
    ) -> str:
        self._seq += 2
        req = f"{window_id}-req"
        resp = f"{window_id}-resp"
        for msg_id, seq, role in ((req, self._seq, "user"), (resp, self._seq + 1, "assistant")):
            self.conn.execute(
                """
                INSERT INTO messages (id, session_id, seq, role, text, content_hash)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (msg_id, session_id, seq, role, f"text {msg_id}", msg_id),
            )
        self.conn.execute(
            """
            INSERT INTO exchange_windows (
                id, session_id, request_message_id, response_message_id,
                input_hash, content_hash
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (window_id, session_id, req, resp, window_id, window_id),
        )
        route = {
            "substantive": "ux",
            "cursor_wrapped": "ux",
            "inter_agent_handoff": "ux",
            "worker_brief": "worker_task",
            "auto_review": "auto_review",
        }.get(request_kind, "drop")
        self.conn.execute(
            """
            INSERT INTO window_det_classifications (
                id, window_id, run_id, turn_kinds_json, request_kind, route,
                extractor_name, extractor_version, created_at
            ) VALUES (?, ?, ?, '[]', ?, ?, 'det_v1', '0.1.0', '2026-07-01T00:00:00+00:00')
            """,
            (f"det-{window_id}", window_id, self.det_run, request_kind, route),
        )
        return window_id

    def det_run_start(self) -> None:
        self.det_run = "detrun"
        self.conn.execute(
            """
            INSERT INTO derivation_runs (
                id, kind, extractor_name, extractor_version, started_at, status,
                meta_json
            ) VALUES ('detrun', 'deterministic', 'det_v1', '0.1.0',
                      '2026-07-01T00:00:00+00:00', 'completed', '{}')
            """
        )


class SemanticMetricTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "metrics.db"
        self.conn = connect(self.path)
        init_db(self.conn)
        self.fx = Fixture(self.conn)
        self.fx.det_run_start()
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def publish_run(self, purpose: str = PURPOSE_FULL_CORPUS) -> str:
        run_id = start_ux_run(
            self.conn,
            model="test-model",
            batch_size=1,
            window_count=0,
            gated=False,
            purpose=purpose,
        )
        return run_id

    def complete(self, run_id: str, *, publish: bool = True) -> None:
        finish_ux_run(self.conn, run_id, status="completed", meta={}, gate_passed=True)
        if publish:
            publish_ux_run(self.conn, run_id, published_by="test")


class LabelFieldMappingTests(SemanticMetricTestBase):
    def test_redirect_turn_kinds_reach_the_numerator(self) -> None:
        """C2: labels live in turn_kinds_json, not in the reliability flags."""
        for i in range(12):
            session = self.fx.session(f"codex:s{i}")
            self.fx.window(session, f"w{i}")
        self.conn.commit()
        run_id = self.publish_run()
        write_ux_observations(
            self.conn,
            run_id,
            [_observation(f"w{i}", ["redirect_or_brake"]) for i in range(12)],
        )
        self.complete(run_id)

        cell = redirect_cell(self.conn, ALL_TIME)
        self.assertEqual(cell.status, "ok", cell.message)
        self.assertEqual(cell.estimate, 10.0)
        self.assertEqual(cell.n_clusters, 12)
        self.assertEqual(cell.coverage["observed_eligible_windows"], 12)
        self.assertEqual(cell.coverage["eligible_windows"], 12)

    def test_dont_act_yet_and_premature_flag_also_count(self) -> None:
        for i in range(12):
            session = self.fx.session(f"codex:s{i}")
            self.fx.window(session, f"w{i}")
        self.conn.commit()
        run_id = self.publish_run()
        obs = [_observation("w0", ["dont_act_yet"])]
        obs.append(_observation("w1", ["human_followup"], premature=True))
        obs.extend(_observation(f"w{i}", ["human_followup"]) for i in range(2, 12))
        write_ux_observations(self.conn, run_id, obs)
        self.complete(run_id)

        cell = redirect_cell(self.conn, ALL_TIME)
        self.assertEqual(cell.status, "ok", cell.message)
        self.assertEqual(cell.coverage["redirect_windows"], 2)

    def test_ordinary_turns_do_not_count(self) -> None:
        for i in range(12):
            session = self.fx.session(f"codex:s{i}")
            self.fx.window(session, f"w{i}")
        self.conn.commit()
        run_id = self.publish_run()
        write_ux_observations(
            self.conn,
            run_id,
            [_observation(f"w{i}", ["human_task", "clarifying_question"]) for i in range(12)],
        )
        self.complete(run_id)

        cell = redirect_cell(self.conn, ALL_TIME)
        self.assertEqual(cell.coverage["redirect_windows"], 0)
        self.assertEqual(cell.status, "ok", cell.message)
        self.assertEqual(cell.estimate, 0.0)

    def test_legacy_flag_shape_is_not_a_redirect(self) -> None:
        """The pre-fix implementation read these keys; they carry no turn kind."""
        for i in range(12):
            session = self.fx.session(f"codex:s{i}")
            self.fx.window(session, f"w{i}")
        self.conn.commit()
        run_id = self.publish_run()
        write_ux_observations(
            self.conn,
            run_id,
            [_observation(f"w{i}", ["human_task"]) for i in range(12)],
        )
        self.conn.execute(
            "UPDATE ux_observations SET flags_json = '{\"redirect_brake\": true}'"
        )
        self.conn.commit()
        self.complete(run_id)

        cell = redirect_cell(self.conn, ALL_TIME)
        self.assertEqual(cell.coverage["redirect_windows"], 0)


class EligibleDenominatorTests(SemanticMetricTestBase):
    def test_non_human_supervisor_windows_are_excluded(self) -> None:
        session = self.fx.session("codex:s0")
        for i in range(12):
            self.fx.window(session, f"sub{i}")
        for i, kind in enumerate(
            ["worker_brief", "inter_agent_handoff", "auto_review", "cursor_wrapped"]
        ):
            self.fx.window(session, f"other{i}", request_kind=kind)
        self.conn.commit()
        run_id = self.publish_run()
        obs = [_observation(f"sub{i}", ["human_task"]) for i in range(12)]
        obs.extend(
            _observation(f"other{i}", ["redirect_or_brake"]) for i in range(4)
        )
        write_ux_observations(self.conn, run_id, obs)
        self.complete(run_id)

        cell = redirect_cell(self.conn, ALL_TIME)
        self.assertEqual(cell.coverage["eligible_windows"], 12)
        self.assertEqual(cell.coverage["observed_eligible_windows"], 12)
        self.assertEqual(cell.coverage["redirect_windows"], 0)

    def test_coverage_uses_all_eligible_windows_not_only_observed(self) -> None:
        session = self.fx.session("codex:s0")
        for i in range(20):
            self.fx.window(session, f"w{i}")
        self.conn.commit()
        run_id = self.publish_run()
        write_ux_observations(
            self.conn,
            run_id,
            [_observation(f"w{i}", ["redirect_or_brake"]) for i in range(5)],
        )
        self.complete(run_id)

        cell = redirect_cell(self.conn, ALL_TIME)
        self.assertEqual(cell.coverage["eligible_windows"], 20)
        self.assertEqual(cell.coverage["observed_eligible_windows"], 5)
        self.assertAlmostEqual(cell.coverage["ratio"], 0.25)
        self.assertEqual(cell.status, "abstain")
        self.assertEqual(cell.reason, "coverage_below_gate")
        self.assertIsNone(cell.estimate)
        self.assertIn("5 of 20", cell.message)

    def test_coverage_at_gate_does_not_abstain_for_coverage(self) -> None:
        for i in range(20):
            session = self.fx.session(f"codex:s{i}")
            self.fx.window(session, f"w{i}")
        self.conn.commit()
        run_id = self.publish_run()
        observed = int(20 * MIN_COVERAGE)
        write_ux_observations(
            self.conn,
            run_id,
            [_observation(f"w{i}", ["redirect_or_brake"]) for i in range(observed)],
        )
        self.complete(run_id)

        cell = redirect_cell(self.conn, ALL_TIME)
        self.assertNotEqual(cell.reason, "coverage_below_gate")
        self.assertEqual(cell.coverage["observed_eligible_windows"], observed)


class PublishedRunSelectionTests(SemanticMetricTestBase):
    def _seed_windows(self, count: int = 12) -> None:
        for i in range(count):
            session = self.fx.session(f"codex:s{i}")
            self.fx.window(session, f"w{i}")
        self.conn.commit()

    def test_unpublished_observations_are_not_aggregated(self) -> None:
        self._seed_windows()
        run_id = self.publish_run()
        write_ux_observations(
            self.conn,
            run_id,
            [_observation(f"w{i}", ["redirect_or_brake"]) for i in range(12)],
        )
        finish_ux_run(self.conn, run_id, status="completed", meta={}, gate_passed=True)

        cell = redirect_cell(self.conn, ALL_TIME)
        self.assertEqual(cell.status, "unavailable")
        self.assertIsNone(cell.estimate)

    def test_failed_audit_gate_never_reaches_the_aggregate(self) -> None:
        self._seed_windows()
        audit = self.publish_run(purpose=PURPOSE_AUDIT)
        write_ux_observations(
            self.conn,
            audit,
            [_observation(f"w{i}", ["redirect_or_brake"]) for i in range(12)],
        )
        finish_ux_run(
            self.conn, audit, status="audit_gate_failed", meta={}, gate_passed=False
        )

        with self.assertRaises(ValueError):
            publish_ux_run(self.conn, audit, published_by="test")
        cell = redirect_cell(self.conn, ALL_TIME)
        self.assertEqual(cell.status, "unavailable")

    def test_passing_audit_run_is_still_not_publishable(self) -> None:
        self._seed_windows()
        audit = self.publish_run(purpose=PURPOSE_AUDIT)
        write_ux_observations(
            self.conn,
            audit,
            [_observation(f"w{i}", ["redirect_or_brake"]) for i in range(12)],
        )
        finish_ux_run(
            self.conn, audit, status="completed_audit", meta={}, gate_passed=True
        )
        with self.assertRaises(ValueError):
            publish_ux_run(self.conn, audit, published_by="test")

    def test_reruns_do_not_double_weight_windows(self) -> None:
        self._seed_windows()
        first = self.publish_run()
        write_ux_observations(
            self.conn,
            first,
            [_observation(f"w{i}", ["redirect_or_brake"]) for i in range(12)],
        )
        self.complete(first)
        before = redirect_cell(self.conn, ALL_TIME)

        second = self.publish_run()
        write_ux_observations(
            self.conn,
            second,
            [_observation(f"w{i}", ["redirect_or_brake"]) for i in range(12)],
        )
        finish_ux_run(self.conn, second, status="completed", meta={}, gate_passed=True)
        after = redirect_cell(self.conn, ALL_TIME)

        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM ux_observations").fetchone()[0], 24
        )
        self.assertEqual(before.n_clusters, after.n_clusters)
        self.assertEqual(before.estimate, after.estimate)
        self.assertEqual(
            before.coverage["observed_eligible_windows"],
            after.coverage["observed_eligible_windows"],
        )

    def test_publishing_a_rerun_replaces_the_pointer(self) -> None:
        self._seed_windows()
        first = self.publish_run()
        write_ux_observations(
            self.conn,
            first,
            [_observation(f"w{i}", ["redirect_or_brake"]) for i in range(12)],
        )
        self.complete(first)

        second = self.publish_run()
        write_ux_observations(
            self.conn,
            second,
            [_observation(f"w{i}", ["human_task"]) for i in range(12)],
        )
        self.complete(second)

        self.assertEqual(published_ux_run_id(self.conn), second)
        cell = redirect_cell(self.conn, ALL_TIME)
        self.assertEqual(cell.coverage["redirect_windows"], 0)
        self.assertEqual(cell.coverage["observed_eligible_windows"], 12)


class RootClusterTests(SemanticMetricTestBase):
    def test_grandchild_rolls_up_to_one_root(self) -> None:
        self.fx.session("codex:root")
        self.fx.session("codex:child", parent="codex:root")
        self.fx.session("codex:grandchild", parent="child")
        self.conn.commit()
        roots = resolve_session_roots(self.conn)
        self.assertEqual(
            {roots["codex:root"], roots["codex:child"], roots["codex:grandchild"]},
            {"codex:root"},
        )

    def test_external_and_cross_harness_parent_ids_resolve(self) -> None:
        self.fx.session("codex:root", external_id="root")
        # parent recorded as the bare external id rather than the canonical id
        self.fx.session("codex:child", external_id="child", parent="root")
        # cross-harness handoff: claude worker pointing at the codex root
        self.fx.session(
            "claude:worker", harness="claude", external_id="worker", parent="root"
        )
        self.conn.commit()
        roots = resolve_session_roots(self.conn)
        self.assertEqual(roots["codex:child"], "codex:root")
        self.assertEqual(roots["claude:worker"], "codex:root")

    def test_parent_cycle_terminates(self) -> None:
        self.fx.session("codex:a", external_id="a", parent="b")
        self.fx.session("codex:b", external_id="b", parent="a")
        self.conn.commit()
        roots = resolve_session_roots(self.conn)
        self.assertEqual(roots["codex:a"], roots["codex:b"])

    def test_orchestration_tree_is_one_cluster_not_many(self) -> None:
        for tree in range(12):
            root = self.fx.session(f"codex:root{tree}", external_id=f"root{tree}")
            child = self.fx.session(
                f"codex:child{tree}", external_id=f"child{tree}", parent=f"root{tree}"
            )
            grandchild = self.fx.session(
                f"codex:gc{tree}", external_id=f"gc{tree}", parent=f"child{tree}"
            )
            for i, session in enumerate((root, child, grandchild)):
                self.fx.window(session, f"w{tree}-{i}")
        self.conn.commit()
        run_id = self.publish_run()
        write_ux_observations(
            self.conn,
            run_id,
            [
                _observation(f"w{tree}-{i}", ["human_task"])
                for tree in range(12)
                for i in range(3)
            ],
        )
        self.complete(run_id)

        cell = redirect_cell(self.conn, ALL_TIME)
        self.assertEqual(cell.n_clusters, 12)
        self.assertEqual(cell.coverage["observed_eligible_windows"], 36)

    def test_model_cell_includes_descendant_windows(self) -> None:
        for tree in range(12):
            root = self.fx.session(
                f"codex:root{tree}", external_id=f"root{tree}", model="gpt-5.5"
            )
            child = self.fx.session(
                f"codex:child{tree}",
                external_id=f"child{tree}",
                parent=f"root{tree}",
                model="gpt-5.5",
            )
            self.fx.window(root, f"w{tree}-root")
            self.fx.window(child, f"w{tree}-child")
        self.conn.commit()
        run_id = self.publish_run()
        write_ux_observations(
            self.conn,
            run_id,
            [
                _observation(f"w{tree}-{part}", ["redirect_or_brake"])
                for tree in range(12)
                for part in ("root", "child")
            ],
        )
        self.complete(run_id)

        cell = redirect_cell(self.conn, ALL_TIME, model="gpt-5.5")
        self.assertEqual(cell.coverage["observed_eligible_windows"], 24)
        self.assertEqual(cell.n_clusters, 12)


class LeadMetricEntryPointTests(SemanticMetricTestBase):
    def test_lead_metric_matches_redirect_cell(self) -> None:
        for i in range(12):
            session = self.fx.session(f"codex:s{i}")
            self.fx.window(session, f"w{i}")
        self.conn.commit()
        run_id = self.publish_run()
        write_ux_observations(
            self.conn,
            run_id,
            [_observation(f"w{i}", ["redirect_or_brake"]) for i in range(12)],
        )
        self.complete(run_id)
        self.assertEqual(
            semantic_lead_metric(self.conn, ALL_TIME).to_dict(),
            redirect_cell(self.conn, ALL_TIME).to_dict(),
        )

    def test_no_published_run_is_unavailable_not_zero(self) -> None:
        session = self.fx.session("codex:s0")
        self.fx.window(session, "w0")
        self.conn.commit()
        cell = semantic_lead_metric(self.conn, ALL_TIME)
        self.assertEqual(cell.status, "unavailable")
        self.assertIsNone(cell.estimate)
        self.assertIn("published", cell.message)

    def test_ungated_published_pointer_abstains(self) -> None:
        """Restore/synthetic labels with gate_passed NULL must not lead."""
        for i in range(12):
            session = self.fx.session(f"codex:s{i}")
            self.fx.window(session, f"w{i}")
        self.conn.commit()
        run_id = self.publish_run()
        write_ux_observations(
            self.conn,
            run_id,
            [_observation(f"w{i}", ["redirect_or_brake"]) for i in range(12)],
        )
        finish_ux_run(self.conn, run_id, status="completed", meta={})
        self.conn.execute(
            """
            INSERT INTO published_derivation_runs
                (kind, run_id, published_at, published_by, note)
            VALUES ('ux_llm', ?, '2026-08-09T00:00:00+00:00', 'test', '')
            """,
            (run_id,),
        )
        self.conn.commit()
        self.assertIsNone(published_ux_run_id(self.conn))
        cell = semantic_lead_metric(self.conn, ALL_TIME)
        self.assertEqual(cell.status, "unavailable")
        self.assertIsNone(cell.estimate)
        self.assertIn("adjudication gate", cell.message)


if __name__ == "__main__":
    unittest.main()
