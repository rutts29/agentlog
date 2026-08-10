from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agentlog.analysis.performance.analysis import ExperimentAnalysis, analyze_primary
from agentlog.analysis.performance.compliance import (
    classify_compliance,
    dominant_model_from_messages,
    models_match,
    normalize_model,
)
from agentlog.analysis.performance.eligibility import (
    EligibilityResult,
    assess_eligibility,
)
from agentlog.analysis.performance.outcomes import (
    DEFAULT_COMPLIANCE_THRESHOLD,
    DEFAULT_TARGET_N_PER_ARM,
    DIRECTIONAL_LICENSE_NOTE,
    PRIMARY_OUTCOME,
    SCOPE_LIMITATION,
    SECONDARY_OUTCOMES,
)
from agentlog.safety.write_guard import assert_writable


class ProtocolMutationError(RuntimeError):
    """Raised when a running experiment's pre-registration would be mutated."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def protocol_hash(protocol: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(protocol).encode("utf-8")).hexdigest()


def build_protocol(
    *,
    model_a: str,
    model_b: str,
    harness: str,
    eligible_tasks: list[str],
    primary_metric_name: str = PRIMARY_OUTCOME.name,
    primary_metric_method_version: str = PRIMARY_OUTCOME.method_version,
    primary_metric_direction: str = PRIMARY_OUTCOME.direction,
    target_n_per_arm: int = DEFAULT_TARGET_N_PER_ARM,
    compliance_threshold: float = DEFAULT_COMPLIANCE_THRESHOLD,
    calendar_weeks: int = 8,
) -> dict[str, Any]:
    if primary_metric_name != PRIMARY_OUTCOME.name:
        # Allow override only for explicit directional experiment metrics.
        direction = primary_metric_direction
        method_version = primary_metric_method_version
    else:
        direction = PRIMARY_OUTCOME.direction
        method_version = PRIMARY_OUTCOME.method_version

    secondary = [
        {
            "name": s.name,
            "direction": s.direction,
            "kind": s.kind,
            "method_version": s.method_version,
            "license": s.license,
            "summary": s.summary,
        }
        for s in SECONDARY_OUTCOMES
    ]
    planned = {
        "primary_analysis": "intention_to_treat",
        "secondary_analysis": "per_protocol",
        "per_protocol_note": (
            "Per-protocol reintroduces selection bias; report alongside ITT, "
            "never as the sole causal claim."
        ),
        "uncertainty": {
            "binary": "wilson_95",
            "continuous": "cluster_bootstrap_median_diff_95",
            "independence_unit": "root_cluster",
        },
        "segregation": [
            "auto_review",
            "subagent_independence",
            "cursor_synthetic_followup",
            "skill_body_dump",
            "image_only",
        ],
        "stop_rule": {
            "target_n_per_arm": target_n_per_arm,
            "calendar_weeks": calendar_weeks,
            "no_early_stop_for_significance": True,
            "under_enrollment": "no_causal_claim",
        },
        "compliance_threshold": compliance_threshold,
        "scope_limitation": SCOPE_LIMITATION,
        "directional_license_note": DIRECTIONAL_LICENSE_NOTE,
    }
    return {
        "shortlist": [model_a, model_b],
        "harness": harness,
        "eligible_tasks": sorted(eligible_tasks),
        "primary_metric": {
            "name": primary_metric_name,
            "method_version": method_version,
            "direction": direction,
            "kind": PRIMARY_OUTCOME.kind
            if primary_metric_name == PRIMARY_OUTCOME.name
            else "binary",
            "license": "randomized_experiment_only",
            "summary": PRIMARY_OUTCOME.summary
            if primary_metric_name == PRIMARY_OUTCOME.name
            else "",
        },
        "secondary_metrics": secondary,
        "planned_analysis": planned,
        "target_n_per_arm": target_n_per_arm,
        "compliance_threshold": compliance_threshold,
    }


class ExperimentService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def register(
        self,
        *,
        model_a: str,
        model_b: str,
        harness: str,
        eligible_tasks: list[str],
        target_n_per_arm: int = DEFAULT_TARGET_N_PER_ARM,
        compliance_threshold: float = DEFAULT_COMPLIANCE_THRESHOLD,
        supersedes_id: str | None = None,
    ) -> dict[str, Any]:
        model_a = normalize_model(model_a) or model_a
        model_b = normalize_model(model_b) or model_b
        if model_a == model_b:
            raise ValueError("shortlist requires two distinct models")
        if len(eligible_tasks) == 0:
            raise ValueError("eligible_tasks must be non-empty")

        protocol = build_protocol(
            model_a=model_a,
            model_b=model_b,
            harness=harness,
            eligible_tasks=eligible_tasks,
            target_n_per_arm=target_n_per_arm,
            compliance_threshold=compliance_threshold,
        )
        pre_hash = protocol_hash(protocol)
        exp_id = _uid("exp")
        version = 1
        if supersedes_id:
            prev = self.get_experiment(supersedes_id)
            if prev is None:
                raise ValueError(f"unknown experiment to supersede: {supersedes_id}")
            version = int(prev["protocol_version"]) + 1

        self.conn.execute(
            """
            INSERT INTO performance_experiments (
                id, protocol_version, supersedes_id, pre_registration_hash,
                protocol_json, shortlist_json, harness, eligible_tasks_json,
                primary_metric_name, primary_metric_method_version,
                primary_metric_direction, primary_metric_license,
                secondary_metrics_json, planned_analysis_json,
                target_n_per_arm, compliance_threshold, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'registered', ?)
            """,
            (
                exp_id,
                version,
                supersedes_id,
                pre_hash,
                _canonical_json(protocol),
                _canonical_json([model_a, model_b]),
                harness,
                _canonical_json(sorted(eligible_tasks)),
                protocol["primary_metric"]["name"],
                protocol["primary_metric"]["method_version"],
                protocol["primary_metric"]["direction"],
                "randomized_experiment_only",
                _canonical_json(protocol["secondary_metrics"]),
                _canonical_json(protocol["planned_analysis"]),
                target_n_per_arm,
                compliance_threshold,
                _now(),
            ),
        )
        self.conn.commit()
        row = self.get_experiment(exp_id)
        assert row is not None
        return row

    def get_experiment(self, experiment_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM performance_experiments WHERE id = ?",
            (experiment_id,),
        ).fetchone()
        return dict(row) if row else None

    def active_experiment(self) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT * FROM performance_experiments
            WHERE status IN ('registered', 'enrolling')
            ORDER BY created_at DESC
            LIMIT 1
            """
        ).fetchone()
        return dict(row) if row else None

    def _assert_mutable_status(self, experiment: dict[str, Any]) -> None:
        if experiment["status"] in {"enrolling", "closed", "abandoned"}:
            if experiment["status"] == "enrolling" or experiment.get(
                "enrollment_started_at"
            ):
                raise ProtocolMutationError(
                    "Pre-registration is frozen after enrollment starts. "
                    "Register a new experiment version instead of mutating this one."
                )

    def mutate_protocol_forbidden(self, experiment_id: str, **_fields: Any) -> None:
        exp = self.get_experiment(experiment_id)
        if exp is None:
            raise ValueError(f"unknown experiment: {experiment_id}")
        if exp["status"] != "registered" or exp.get("enrollment_started_at"):
            raise ProtocolMutationError(
                "Pre-registration is frozen after enrollment starts. "
                "Register a new experiment version instead of mutating this one."
            )
        # Even in registered state, do not silently rewrite protocol_json.
        # Callers must create a superseding registration.
        raise ProtocolMutationError(
            "Protocol fields are immutable. Create a new version with supersedes_id."
        )

    def enroll_and_assign(
        self,
        *,
        experiment_id: str,
        primary_task: str,
        harness: str,
        is_new_root: bool = True,
        is_subagent: bool = False,
        is_auto_review: bool = False,
        is_continuation: bool = False,
        owner_affirm_comparable: bool = False,
        both_models_available: bool = True,
        population_flags: list[str] | None = None,
        root_session_id: str | None = None,
        rng: secrets.SystemRandom | None = None,
    ) -> dict[str, Any]:
        exp = self.get_experiment(experiment_id)
        if exp is None:
            raise ValueError(f"unknown experiment: {experiment_id}")

        shortlist = json.loads(exp["shortlist_json"])
        eligible_tasks = json.loads(exp["eligible_tasks_json"])
        already = False
        if root_session_id:
            existing = self.conn.execute(
                """
                SELECT id FROM performance_experiment_assignments
                WHERE experiment_id = ? AND root_session_id = ?
                """,
                (experiment_id, root_session_id),
            ).fetchone()
            already = existing is not None

        elig = assess_eligibility(
            experiment={
                "status": exp["status"],
                "harness": exp["harness"],
                "eligible_tasks": eligible_tasks,
            },
            primary_task=primary_task,
            harness=harness,
            is_new_root=is_new_root,
            is_subagent=is_subagent,
            is_auto_review=is_auto_review,
            is_continuation=is_continuation,
            owner_affirm_comparable=owner_affirm_comparable,
            both_models_available=both_models_available,
            already_assigned=already,
            population_flags=population_flags,
        )
        if not elig.eligible:
            excl_id = _uid("excl")
            self.conn.execute(
                """
                INSERT INTO performance_experiment_exclusions (
                    id, experiment_id, root_session_id, excluded_at, reason, eligibility_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    excl_id,
                    experiment_id,
                    root_session_id,
                    _now(),
                    ";".join(elig.reasons),
                    _canonical_json(elig.to_json()),
                ),
            )
            self.conn.commit()
            return {
                "enrolled": False,
                "exclusion_id": excl_id,
                "eligibility": elig.to_json(),
                "assigned_model": None,
            }

        coin = rng or secrets.SystemRandom()
        assigned = shortlist[coin.randrange(2)]
        draw_id = _uid("draw")
        assignment_seed = secrets.token_hex(16)
        assignment_id = _uid("asg")
        now = _now()

        if exp["status"] == "registered":
            self.conn.execute(
                """
                UPDATE performance_experiments
                SET status = 'enrolling', enrollment_started_at = ?
                WHERE id = ?
                """,
                (now, experiment_id),
            )

        self.conn.execute(
            """
            INSERT INTO performance_experiment_assignments (
                id, experiment_id, task_cluster_id, root_session_id,
                assigned_model, assignment_seed, draw_id, assigned_at,
                eligibility_json, intent_to_treat_model, as_treated_model,
                compliance_status
            ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, NULL, 'pending')
            """,
            (
                assignment_id,
                experiment_id,
                root_session_id,
                assigned,
                assignment_seed,
                draw_id,
                now,
                _canonical_json(elig.to_json()),
                assigned,
            ),
        )
        self.conn.commit()
        return {
            "enrolled": True,
            "assignment_id": assignment_id,
            "draw_id": draw_id,
            "assigned_model": assigned,
            "shortlist": shortlist,
            "assigned_at": now,
            "eligibility": elig.to_json(),
            "scope_limitation": SCOPE_LIMITATION,
            "instruction": (
                f"Use model `{assigned}` for this task. "
                "Start the session with that model only; compliance is checked "
                "from the transcript, not self-report."
            ),
        }

    def link_session(
        self,
        *,
        assignment_id: str,
        root_session_id: str,
    ) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM performance_experiment_assignments WHERE id = ?",
            (assignment_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown assignment: {assignment_id}")

        session = self.conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (root_session_id,)
        ).fetchone()
        if session is None:
            raise ValueError(f"unknown session: {root_session_id}")
        if session["parent_session_id"]:
            raise ValueError("refusing to link a subagent session as root")

        cluster_id = self._ensure_root_cluster(root_session_id)
        self.conn.execute(
            """
            UPDATE performance_experiment_assignments
            SET root_session_id = ?, task_cluster_id = ?
            WHERE id = ?
            """,
            (root_session_id, cluster_id, assignment_id),
        )
        self.conn.commit()
        return self.refresh_compliance(assignment_id=assignment_id)

    def _ensure_root_cluster(self, root_session_id: str) -> str:
        existing = self.conn.execute(
            """
            SELECT id FROM task_clusters
            WHERE root_session_id = ? AND cluster_kind = 'root'
              AND segment_start_message_id IS NULL
            """,
            (root_session_id,),
        ).fetchone()
        if existing:
            return str(existing["id"])
        cluster_id = _uid("tc")
        self.conn.execute(
            """
            INSERT INTO task_clusters (
                id, root_session_id, segment_start_message_id,
                segment_end_message_id, cluster_kind
            ) VALUES (?, ?, NULL, NULL, 'root')
            """,
            (cluster_id, root_session_id),
        )
        return cluster_id

    def refresh_compliance(self, *, assignment_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM performance_experiment_assignments WHERE id = ?",
            (assignment_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown assignment: {assignment_id}")
        asg = dict(row)
        session_id = asg.get("root_session_id")
        if not session_id:
            result = classify_compliance(
                assigned_model=asg["assigned_model"],
                as_treated_model=None,
                session_started=False,
            )
        else:
            session = self.conn.execute(
                "SELECT model FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            msg_models = [
                r["model"]
                for r in self.conn.execute(
                    """
                    SELECT model FROM messages
                    WHERE session_id = ? AND role = 'assistant' AND model IS NOT NULL
                    """,
                    (session_id,),
                )
            ]
            dominant = dominant_model_from_messages(msg_models)
            as_treated = dominant or (session["model"] if session else None)
            result = classify_compliance(
                assigned_model=asg["assigned_model"],
                as_treated_model=as_treated,
                session_started=True,
            )

        self.conn.execute(
            """
            UPDATE performance_experiment_assignments
            SET as_treated_model = ?, compliance_status = ?
            WHERE id = ?
            """,
            (result.as_treated_model, result.status, assignment_id),
        )
        self.conn.commit()
        out = dict(asg)
        out["as_treated_model"] = result.as_treated_model
        out["compliance_status"] = result.status
        out["compliance_reason"] = result.reason
        return out

    def sync_all_compliance(self, experiment_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT id FROM performance_experiment_assignments
            WHERE experiment_id = ?
            """,
            (experiment_id,),
        ).fetchall()
        return [self.refresh_compliance(assignment_id=str(r["id"])) for r in rows]

    def record_primary_outcome(
        self,
        *,
        assignment_id: str,
        value: float,
        availability: str = "observed",
    ) -> None:
        self.conn.execute(
            """
            UPDATE performance_experiment_assignments
            SET primary_outcome_value = ?, primary_outcome_availability = ?
            WHERE id = ?
            """,
            (value, availability, assignment_id),
        )
        asg = self.conn.execute(
            "SELECT task_cluster_id FROM performance_experiment_assignments WHERE id = ?",
            (assignment_id,),
        ).fetchone()
        if asg and asg["task_cluster_id"]:
            exp = self.conn.execute(
                """
                SELECT e.primary_metric_name, e.primary_metric_method_version
                FROM performance_experiment_assignments a
                JOIN performance_experiments e ON e.id = a.experiment_id
                WHERE a.id = ?
                """,
                (assignment_id,),
            ).fetchone()
            assert exp is not None
            obs_id = _uid("out")
            self.conn.execute(
                """
                INSERT INTO outcome_observations (
                    id, task_cluster_id, metric_name, value_num, value_text,
                    availability, confidence, method_version, evidence_json,
                    derivation_run_id, created_at
                ) VALUES (?, ?, ?, ?, NULL, ?, NULL, ?, ?, NULL, ?)
                ON CONFLICT(task_cluster_id, metric_name, method_version) DO UPDATE SET
                    value_num = excluded.value_num,
                    availability = excluded.availability,
                    evidence_json = excluded.evidence_json,
                    created_at = excluded.created_at
                """,
                (
                    obs_id,
                    asg["task_cluster_id"],
                    exp["primary_metric_name"],
                    value,
                    availability,
                    exp["primary_metric_method_version"],
                    _canonical_json(
                        {
                            "source": "experiment_assignment",
                            "assignment_id": assignment_id,
                            "license": "randomized_experiment_only",
                        }
                    ),
                    _now(),
                ),
            )
        self.conn.commit()

    def detect_rejected_redo_from_ux(self, root_session_id: str) -> float | None:
        """
        Derive had_rejected_redo from ux_observations when the extractor has run.

        Depends on extraction layer tables (ux_observations); returns None if absent.
        """
        tables = {
            str(r[0])
            for r in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "ux_observations" not in tables or "exchange_windows" not in tables:
            return None
        row = self.conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM ux_observations u
            JOIN exchange_windows w ON w.id = u.window_id
            WHERE w.session_id = ? AND u.prior_outcome = 'rejected_redo'
            """,
            (root_session_id,),
        ).fetchone()
        if row is None:
            return None
        return 1.0 if int(row["c"]) > 0 else 0.0

    def analyze(self, experiment_id: str) -> ExperimentAnalysis:
        exp = self.get_experiment(experiment_id)
        if exp is None:
            raise ValueError(f"unknown experiment: {experiment_id}")
        shortlist = json.loads(exp["shortlist_json"])
        model_a, model_b = shortlist[0], shortlist[1]
        assignments = self.conn.execute(
            """
            SELECT * FROM performance_experiment_assignments
            WHERE experiment_id = ?
            ORDER BY assigned_at
            """,
            (experiment_id,),
        ).fetchall()
        rows: list[dict[str, Any]] = []
        for a in assignments:
            outcome = a["primary_outcome_value"]
            if outcome is None and a["root_session_id"]:
                detected = self.detect_rejected_redo_from_ux(str(a["root_session_id"]))
                if detected is not None:
                    outcome = detected
            rows.append(
                {
                    "assigned_model": a["assigned_model"],
                    "as_treated_model": a["as_treated_model"],
                    "compliance_status": a["compliance_status"],
                    "outcome": None if outcome is None else float(outcome),
                }
            )
        kind = "binary"
        return analyze_primary(
            experiment_id=experiment_id,
            model_a=model_a,
            model_b=model_b,
            primary_metric=exp["primary_metric_name"],
            primary_kind=kind,
            primary_direction=exp["primary_metric_direction"],
            target_n_per_arm=int(exp["target_n_per_arm"]),
            compliance_threshold=float(exp["compliance_threshold"]),
            rows=rows,
        )

    def enrollment_progress(self, experiment_id: str) -> dict[str, Any]:
        exp = self.get_experiment(experiment_id)
        if exp is None:
            raise ValueError(f"unknown experiment: {experiment_id}")
        shortlist = json.loads(exp["shortlist_json"])
        counts = {m: 0 for m in shortlist}
        for row in self.conn.execute(
            """
            SELECT assigned_model, COUNT(*) AS c
            FROM performance_experiment_assignments
            WHERE experiment_id = ?
            GROUP BY assigned_model
            """,
            (experiment_id,),
        ):
            counts[str(row["assigned_model"])] = int(row["c"])
        target = int(exp["target_n_per_arm"])
        return {
            "experiment_id": experiment_id,
            "status": exp["status"],
            "target_n_per_arm": target,
            "counts": counts,
            "reached_target": all(counts.get(m, 0) >= target for m in shortlist),
            "scope_limitation": SCOPE_LIMITATION,
            "primary_metric": exp["primary_metric_name"],
            "primary_metric_direction": exp["primary_metric_direction"],
            "primary_metric_license": exp["primary_metric_license"],
            "directional_license_note": DIRECTIONAL_LICENSE_NOTE,
        }

    def write_assignment_card(self, assignment: dict[str, Any], path: Path) -> Path:
        """Persist a low-friction assignment card the developer can keep visible."""
        path = assert_writable(path, purpose="assignment card")
        path.parent.mkdir(parents=True, exist_ok=True)
        card = {
            "assigned_model": assignment.get("assigned_model"),
            "assignment_id": assignment.get("assignment_id"),
            "assigned_at": assignment.get("assigned_at"),
            "instruction": assignment.get("instruction"),
            "scope_limitation": SCOPE_LIMITATION,
            "directional_license_note": DIRECTIONAL_LICENSE_NOTE,
        }
        path.write_text(json.dumps(card, indent=2) + "\n", encoding="utf-8")
        return path


def models_equal(a: str, b: str | None) -> bool:
    return models_match(a, b)
