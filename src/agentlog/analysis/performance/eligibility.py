from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


EXCLUDED_POPULATIONS = frozenset(
    {
        "auto_review",
        "subagent",
        "continuation",
        "cursor_synthetic_followup",
        "skill_body_dump",
        "image_only",
        "mixed_task",
        "unknown_task",
    }
)


@dataclass
class EligibilityResult:
    eligible: bool
    reasons: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "eligible": self.eligible,
            "reasons": list(self.reasons),
            "details": dict(self.details),
        }


def assess_eligibility(
    *,
    experiment: dict[str, Any],
    primary_task: str,
    harness: str,
    is_new_root: bool,
    is_subagent: bool,
    is_auto_review: bool,
    is_continuation: bool,
    owner_affirm_comparable: bool,
    both_models_available: bool,
    already_assigned: bool,
    population_flags: list[str] | None = None,
) -> EligibilityResult:
    """Decide whether a prospective task may enroll (pre-assignment checks)."""
    reasons: list[str] = []
    details: dict[str, Any] = {
        "primary_task": primary_task,
        "harness": harness,
        "is_new_root": is_new_root,
        "owner_affirm_comparable": owner_affirm_comparable,
        "both_models_available": both_models_available,
    }
    eligible_tasks = list(experiment.get("eligible_tasks") or [])
    exp_harness = str(experiment.get("harness") or "")

    if str(experiment.get("status")) not in {"registered", "enrolling"}:
        reasons.append("experiment_not_open")
    if harness != exp_harness:
        reasons.append("harness_mismatch")
    if primary_task not in eligible_tasks:
        reasons.append("task_not_eligible")
    if primary_task in {"mixed", "unknown"}:
        reasons.append("task_mixed_or_unknown")
    if not is_new_root:
        reasons.append("not_new_root")
    if is_subagent:
        reasons.append("subagent_excluded")
    if is_auto_review:
        reasons.append("auto_review_excluded")
    if is_continuation:
        reasons.append("continuation_excluded")
    if not owner_affirm_comparable:
        reasons.append("comparability_not_affirmed")
    if not both_models_available:
        reasons.append("models_unavailable")
    if already_assigned:
        reasons.append("already_assigned")

    for flag in population_flags or []:
        if flag in EXCLUDED_POPULATIONS:
            reasons.append(f"population:{flag}")

    return EligibilityResult(
        eligible=len(reasons) == 0,
        reasons=reasons,
        details=details,
    )
