from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from agentlog.analysis.performance.analysis import analyze_primary
from agentlog.analysis.performance.compliance import (
    classify_compliance,
    dominant_model_from_messages,
)
from agentlog.analysis.performance.experiments import (
    ExperimentService,
    ProtocolMutationError,
    protocol_hash,
)
from agentlog.analysis.performance.outcomes import (
    DEFAULT_COMPLIANCE_THRESHOLD,
    DEFAULT_TARGET_N_PER_ARM,
    PRIMARY_OUTCOME,
)
from agentlog.db.schema import connect, init_db


class _TestDb:
    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "test.db"
        self.conn = connect(self.path)
        init_db(self.conn)

    def close(self) -> None:
        self.conn.close()
        self._tmp.cleanup()


def _db() -> _TestDb:
    return _TestDb()


def _insert_session(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    model: str,
    parent: str | None = None,
    assistant_models: list[str] | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO artifacts (harness, path, size, mtime_ns, content_hash, parsed_offset, parser_version)
        VALUES ('codex', ?, 1, 1, 'h', 0, '1')
        """,
        (f"/tmp/{session_id}.jsonl",),
    )
    art = conn.execute("SELECT id FROM artifacts WHERE path = ?", (f"/tmp/{session_id}.jsonl",)).fetchone()
    assert art is not None
    external = session_id.split(":", 1)[-1]
    conn.execute(
        """
        INSERT INTO sessions (
            id, harness, external_id, parent_session_id, artifact_id,
            started_at, model
        ) VALUES (?, 'codex', ?, ?, ?, '2026-08-01T00:00:00Z', ?)
        """,
        (session_id, external, parent, int(art["id"]), model),
    )
    models = assistant_models if assistant_models is not None else [model] * 5
    for i, m in enumerate(models):
        conn.execute(
            """
            INSERT INTO messages (
                id, session_id, seq, role, timestamp, model, text, content_hash
            ) VALUES (?, ?, ?, 'assistant', '2026-08-01T00:00:01Z', ?, 'hi', 'c')
            """,
            (f"{session_id}:m:{i}", session_id, i, m),
        )
    conn.commit()


class RandomAssignmentTests(unittest.TestCase):
    def test_coin_flip_is_balanced_over_many_draws(self) -> None:
        db = _db()
        try:
            svc = ExperimentService(db.conn)
            exp = svc.register(
                model_a="gpt-5.5",
                model_b="gpt-5.6-sol",
                harness="codex",
                eligible_tasks=["debug"],
            )
            counts = {"gpt-5.5": 0, "gpt-5.6-sol": 0}
            n = 2000
            for i in range(n):
                result = svc.enroll_and_assign(
                    experiment_id=exp["id"],
                    primary_task="debug",
                    harness="codex",
                    owner_affirm_comparable=True,
                )
                self.assertTrue(result["enrolled"])
                counts[result["assigned_model"]] += 1
            self.assertGreater(counts["gpt-5.5"], n * 0.40)
            self.assertGreater(counts["gpt-5.6-sol"], n * 0.40)
            self.assertEqual(sum(counts.values()), n)
        finally:
            db.close()

    def test_system_random_not_constant(self) -> None:
        db = _db()
        try:
            svc = ExperimentService(db.conn)
            exp = svc.register(
                model_a="a-model",
                model_b="b-model",
                harness="codex",
                eligible_tasks=["debug"],
            )
            seen = set()
            for _ in range(40):
                result = svc.enroll_and_assign(
                    experiment_id=exp["id"],
                    primary_task="debug",
                    harness="codex",
                    owner_affirm_comparable=True,
                )
                seen.add(result["assigned_model"])
            self.assertEqual(seen, {"a-model", "b-model"})
        finally:
            db.close()


class EligibilityTests(unittest.TestCase):
    def test_ineligible_tasks_refused(self) -> None:
        db = _db()
        try:
            svc = ExperimentService(db.conn)
            exp = svc.register(
                model_a="gpt-5.5",
                model_b="grok-4.5-build",
                harness="codex",
                eligible_tasks=["debug"],
            )
            cases = [
                {"primary_task": "docs", "owner_affirm_comparable": True},
                {"primary_task": "debug", "owner_affirm_comparable": False},
                {
                    "primary_task": "debug",
                    "owner_affirm_comparable": True,
                    "is_auto_review": True,
                },
                {
                    "primary_task": "debug",
                    "owner_affirm_comparable": True,
                    "is_subagent": True,
                },
                {
                    "primary_task": "mixed",
                    "owner_affirm_comparable": True,
                },
            ]
            for kwargs in cases:
                with self.subTest(kwargs=kwargs):
                    result = svc.enroll_and_assign(
                        experiment_id=exp["id"],
                        harness="codex",
                        **kwargs,
                    )
                    self.assertFalse(result["enrolled"])
                    self.assertIsNotNone(result["exclusion_id"])
                    self.assertTrue(result["eligibility"]["reasons"])
        finally:
            db.close()


class ImmutabilityTests(unittest.TestCase):
    def test_cannot_mutate_after_enrollment_starts(self) -> None:
        db = _db()
        try:
            svc = ExperimentService(db.conn)
            exp = svc.register(
                model_a="gpt-5.5",
                model_b="gpt-5.6-sol",
                harness="codex",
                eligible_tasks=["debug"],
            )
            pre = exp["pre_registration_hash"]
            result = svc.enroll_and_assign(
                experiment_id=exp["id"],
                primary_task="debug",
                harness="codex",
                owner_affirm_comparable=True,
            )
            self.assertTrue(result["enrolled"])
            refreshed = svc.get_experiment(exp["id"])
            assert refreshed is not None
            self.assertEqual(refreshed["status"], "enrolling")
            self.assertEqual(refreshed["pre_registration_hash"], pre)

            with self.assertRaises(ProtocolMutationError):
                svc.mutate_protocol_forbidden(exp["id"], harness="claude")

            v2 = svc.register(
                model_a="gpt-5.5",
                model_b="gpt-5.6-sol",
                harness="claude",
                eligible_tasks=["debug", "refactor"],
                supersedes_id=exp["id"],
            )
            self.assertNotEqual(v2["id"], exp["id"])
            self.assertEqual(v2["protocol_version"], 2)
            old = svc.get_experiment(exp["id"])
            assert old is not None
            self.assertEqual(old["pre_registration_hash"], pre)
            self.assertEqual(old["harness"], "codex")
        finally:
            db.close()

    def test_protocol_hash_stable(self) -> None:
        from agentlog.analysis.performance.experiments import build_protocol

        p1 = build_protocol(
            model_a="a",
            model_b="b",
            harness="codex",
            eligible_tasks=["debug", "refactor"],
        )
        p2 = build_protocol(
            model_a="a",
            model_b="b",
            harness="codex",
            eligible_tasks=["refactor", "debug"],
        )
        self.assertEqual(protocol_hash(p1), protocol_hash(p2))


class ComplianceTests(unittest.TestCase):
    def test_noncompliance_detected_from_transcript(self) -> None:
        db = _db()
        try:
            svc = ExperimentService(db.conn)
            exp = svc.register(
                model_a="gpt-5.5",
                model_b="gpt-5.6-sol",
                harness="codex",
                eligible_tasks=["debug"],
            )

            class AlwaysA:
                def randrange(self, n: int) -> int:
                    return 0

            result = svc.enroll_and_assign(
                experiment_id=exp["id"],
                primary_task="debug",
                harness="codex",
                owner_affirm_comparable=True,
                rng=AlwaysA(),  # type: ignore[arg-type]
            )
            self.assertEqual(result["assigned_model"], "gpt-5.5")
            _insert_session(
                db.conn,
                session_id="codex:deviated-1",
                model="gpt-5.6-sol",
                assistant_models=["gpt-5.6-sol"] * 4,
            )
            linked = svc.link_session(
                assignment_id=result["assignment_id"],
                root_session_id="codex:deviated-1",
            )
            self.assertEqual(linked["compliance_status"], "deviated")
            self.assertEqual(linked["as_treated_model"], "gpt-5.6-sol")
        finally:
            db.close()

    def test_compliance_complied_when_assigned_used(self) -> None:
        db = _db()
        try:
            svc = ExperimentService(db.conn)
            exp = svc.register(
                model_a="gpt-5.5",
                model_b="gpt-5.6-sol",
                harness="codex",
                eligible_tasks=["debug"],
            )

            class AlwaysB:
                def randrange(self, n: int) -> int:
                    return 1

            result = svc.enroll_and_assign(
                experiment_id=exp["id"],
                primary_task="debug",
                harness="codex",
                owner_affirm_comparable=True,
                rng=AlwaysB(),  # type: ignore[arg-type]
            )
            self.assertEqual(result["assigned_model"], "gpt-5.6-sol")
            _insert_session(
                db.conn,
                session_id="codex:ok-1",
                model="gpt-5.6-sol",
                assistant_models=["gpt-5.6-sol"] * 5,
            )
            linked = svc.link_session(
                assignment_id=result["assignment_id"],
                root_session_id="codex:ok-1",
            )
            self.assertEqual(linked["compliance_status"], "complied")
        finally:
            db.close()

    def test_dominant_model_requires_share(self) -> None:
        self.assertIsNone(
            dominant_model_from_messages(["a", "b", "a", "b"], share_threshold=0.80)
        )
        self.assertEqual(
            dominant_model_from_messages(["a"] * 8 + ["b"] * 2, share_threshold=0.80),
            "a",
        )

    def test_classify_abandoned_before_start(self) -> None:
        r = classify_compliance(
            assigned_model="gpt-5.5",
            as_treated_model=None,
            session_started=False,
        )
        self.assertEqual(r.status, "abandoned_before_start")


class UnderEnrollmentTests(unittest.TestCase):
    def test_under_enrollment_withholds_causal_claim(self) -> None:
        report = analyze_primary(
            experiment_id="exp_test",
            model_a="a",
            model_b="b",
            primary_metric=PRIMARY_OUTCOME.name,
            primary_kind="binary",
            primary_direction=PRIMARY_OUTCOME.direction,
            target_n_per_arm=DEFAULT_TARGET_N_PER_ARM,
            compliance_threshold=DEFAULT_COMPLIANCE_THRESHOLD,
            rows=[
                {
                    "assigned_model": "a",
                    "as_treated_model": "a",
                    "compliance_status": "complied",
                    "outcome": 1.0,
                },
                {
                    "assigned_model": "b",
                    "as_treated_model": "b",
                    "compliance_status": "complied",
                    "outcome": 0.0,
                },
            ],
        )
        self.assertEqual(report.claim_status, "descriptive_progress")
        self.assertIn("under_enrolled", report.reasons)
        self.assertIn("No causal claim", report.claim_language)

    def test_low_compliance_withholds_even_at_target(self) -> None:
        rows = []
        for i in range(DEFAULT_TARGET_N_PER_ARM):
            rows.append(
                {
                    "assigned_model": "a",
                    "as_treated_model": "b",
                    "compliance_status": "deviated",
                    "outcome": 0.0,
                }
            )
            rows.append(
                {
                    "assigned_model": "b",
                    "as_treated_model": "b",
                    "compliance_status": "complied",
                    "outcome": 1.0,
                }
            )
        report = analyze_primary(
            experiment_id="exp_test",
            model_a="a",
            model_b="b",
            primary_metric=PRIMARY_OUTCOME.name,
            primary_kind="binary",
            primary_direction=PRIMARY_OUTCOME.direction,
            target_n_per_arm=DEFAULT_TARGET_N_PER_ARM,
            compliance_threshold=DEFAULT_COMPLIANCE_THRESHOLD,
            rows=rows,
        )
        self.assertEqual(report.claim_status, "withheld")
        self.assertIn("compliance_below_threshold", report.reasons)

    def test_adequate_enrollment_and_compliance_allows_causal(self) -> None:
        rows = []
        for _ in range(DEFAULT_TARGET_N_PER_ARM):
            rows.append(
                {
                    "assigned_model": "a",
                    "as_treated_model": "a",
                    "compliance_status": "complied",
                    "outcome": 1.0,
                }
            )
            rows.append(
                {
                    "assigned_model": "b",
                    "as_treated_model": "b",
                    "compliance_status": "complied",
                    "outcome": 0.0,
                }
            )
        report = analyze_primary(
            experiment_id="exp_test",
            model_a="a",
            model_b="b",
            primary_metric=PRIMARY_OUTCOME.name,
            primary_kind="binary",
            primary_direction=PRIMARY_OUTCOME.direction,
            target_n_per_arm=DEFAULT_TARGET_N_PER_ARM,
            compliance_threshold=DEFAULT_COMPLIANCE_THRESHOLD,
            rows=rows,
        )
        self.assertEqual(report.claim_status, "causal")
        self.assertEqual(report.itt["arms"]["a"]["events"], DEFAULT_TARGET_N_PER_ARM)
        self.assertIn("intention-to-treat", report.claim_language.lower())
        # Primary outcome license must stay experiment-scoped in copy.
        self.assertIn("random assignment", report.directional_license_note.lower())


class PrimaryOutcomeContractTests(unittest.TestCase):
    def test_primary_is_directional_rejected_redo(self) -> None:
        self.assertEqual(PRIMARY_OUTCOME.name, "had_rejected_redo")
        self.assertEqual(PRIMARY_OUTCOME.direction, "higher_is_worse")
        self.assertEqual(PRIMARY_OUTCOME.license, "randomized_experiment_only")

    def test_schema_records_license_fields(self) -> None:
        db = _db()
        try:
            svc = ExperimentService(db.conn)
            exp = svc.register(
                model_a="gpt-5.5",
                model_b="gpt-5.6-sol",
                harness="codex",
                eligible_tasks=["debug"],
            )
            self.assertEqual(exp["primary_metric_name"], "had_rejected_redo")
            self.assertEqual(exp["primary_metric_direction"], "higher_is_worse")
            self.assertEqual(exp["primary_metric_license"], "randomized_experiment_only")
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
