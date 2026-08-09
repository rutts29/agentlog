from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agentlog.analysis.performance.outcomes import (
    DIRECTIONAL_LICENSE_NOTE,
    PER_PROTOCOL_BIAS_NOTE,
    SCOPE_LIMITATION,
)
from agentlog.analysis.performance.stats import (
    IntervalEstimate,
    cluster_bootstrap_median_diff,
    risk_difference_wilson,
    wilson_interval,
)


@dataclass
class ArmStats:
    model: str
    n: int
    events: int
    rate: IntervalEstimate | None = None
    values: list[float] = field(default_factory=list)


@dataclass
class ExperimentAnalysis:
    experiment_id: str
    claim_status: str  # causal | descriptive_progress | withheld
    claim_language: str
    scope_limitation: str
    directional_license_note: str
    per_protocol_bias_note: str
    target_n_per_arm: int
    enrolled_per_arm: dict[str, int]
    compliance_rate: float | None
    compliance_threshold: float
    primary_metric: str
    primary_kind: str
    itt: dict[str, Any]
    per_protocol: dict[str, Any]
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "experiment_id": self.experiment_id,
            "claim_status": self.claim_status,
            "claim_language": self.claim_language,
            "scope_limitation": self.scope_limitation,
            "directional_license_note": self.directional_license_note,
            "per_protocol_bias_note": self.per_protocol_bias_note,
            "target_n_per_arm": self.target_n_per_arm,
            "enrolled_per_arm": dict(self.enrolled_per_arm),
            "compliance_rate": self.compliance_rate,
            "compliance_threshold": self.compliance_threshold,
            "primary_metric": self.primary_metric,
            "primary_kind": self.primary_kind,
            "itt": self.itt,
            "per_protocol": self.per_protocol,
            "reasons": list(self.reasons),
        }


def _binary_arm(model: str, values: list[float]) -> ArmStats:
    n = len(values)
    events = int(sum(1 for v in values if v >= 0.5))
    rate = wilson_interval(events, n) if n else None
    return ArmStats(model=model, n=n, events=events, rate=rate, values=values)


def _continuous_arm(model: str, values: list[float]) -> ArmStats:
    return ArmStats(model=model, n=len(values), events=0, values=values)


def _interval_dict(iv: IntervalEstimate | None) -> dict[str, Any] | None:
    if iv is None:
        return None
    return {
        "estimate": iv.estimate,
        "low": iv.low,
        "high": iv.high,
        "n": iv.n,
    }


def analyze_primary(
    *,
    experiment_id: str,
    model_a: str,
    model_b: str,
    primary_metric: str,
    primary_kind: str,
    primary_direction: str,
    target_n_per_arm: int,
    compliance_threshold: float,
    # Each row: assigned_model, as_treated_model, compliance_status, outcome (float|None)
    rows: list[dict[str, Any]],
) -> ExperimentAnalysis:
    enrolled: dict[str, int] = {model_a: 0, model_b: 0}
    for row in rows:
        assigned = str(row["assigned_model"])
        if assigned in enrolled:
            enrolled[assigned] += 1

    decided = [
        r
        for r in rows
        if r.get("compliance_status") in {"complied", "deviated", "abandoned_before_start"}
    ]
    complied = [r for r in decided if r.get("compliance_status") == "complied"]
    compliance_rate = (len(complied) / len(decided)) if decided else None

    reasons: list[str] = []
    under = any(enrolled.get(m, 0) < target_n_per_arm for m in (model_a, model_b))
    if under:
        reasons.append("under_enrolled")
    if compliance_rate is not None and compliance_rate < compliance_threshold:
        reasons.append("compliance_below_threshold")
    if not rows:
        reasons.append("no_assignments")

    def collect(population: str) -> tuple[list[float], list[float]]:
        a_vals: list[float] = []
        b_vals: list[float] = []
        for row in rows:
            outcome = row.get("outcome")
            if outcome is None:
                continue
            if population == "itt":
                arm = str(row["assigned_model"])
            else:
                if row.get("compliance_status") != "complied":
                    continue
                arm = str(row.get("as_treated_model") or "")
            if arm == model_a:
                a_vals.append(float(outcome))
            elif arm == model_b:
                b_vals.append(float(outcome))
        return a_vals, b_vals

    def summarize(pop: str) -> dict[str, Any]:
        a_vals, b_vals = collect(pop)
        if primary_kind == "binary":
            arm_a = _binary_arm(model_a, a_vals)
            arm_b = _binary_arm(model_b, b_vals)
            contrast = None
            if arm_a.n and arm_b.n:
                contrast = risk_difference_wilson(
                    arm_a.events, arm_a.n, arm_b.events, arm_b.n
                )
            return {
                "population": pop,
                "arms": {
                    model_a: {
                        "n": arm_a.n,
                        "events": arm_a.events,
                        "rate": _interval_dict(arm_a.rate),
                    },
                    model_b: {
                        "n": arm_b.n,
                        "events": arm_b.events,
                        "rate": _interval_dict(arm_b.rate),
                    },
                },
                "contrast": {
                    "type": "risk_difference",
                    "interpretation": f"{model_a} minus {model_b}",
                    "direction_note": primary_direction,
                    **(_interval_dict(contrast) or {}),
                },
            }
        arm_a = _continuous_arm(model_a, a_vals)
        arm_b = _continuous_arm(model_b, b_vals)
        contrast = None
        if arm_a.values and arm_b.values:
            contrast = cluster_bootstrap_median_diff(arm_a.values, arm_b.values)
        return {
            "population": pop,
            "arms": {
                model_a: {"n": arm_a.n, "values_n": len(arm_a.values)},
                model_b: {"n": arm_b.n, "values_n": len(arm_b.values)},
            },
            "contrast": {
                "type": "median_difference",
                "interpretation": f"median({model_a}) minus median({model_b})",
                "direction_note": primary_direction,
                **(_interval_dict(contrast) or {}),
            },
        }

    itt = summarize("itt")
    pp = summarize("per_protocol")

    if "under_enrolled" in reasons or "no_assignments" in reasons:
        claim_status = "descriptive_progress"
        claim_language = (
            "No causal claim: enrollment has not reached the pre-registered "
            f"target of {target_n_per_arm} root sessions per arm. "
            f"Progress: {model_a}={enrolled.get(model_a, 0)}, "
            f"{model_b}={enrolled.get(model_b, 0)}."
        )
    elif "compliance_below_threshold" in reasons:
        claim_status = "withheld"
        claim_language = (
            "Causal conclusion withheld: observed compliance "
            f"({compliance_rate:.0%}) is below the pre-registered threshold "
            f"({compliance_threshold:.0%}). Intention-to-treat estimates may still "
            "be shown as exploratory progress, not as a causal result."
        )
    else:
        claim_status = "causal"
        worse = "higher" if primary_direction == "higher_is_worse" else "lower"
        claim_language = (
            f"Under this pre-registered randomized comparison, the intention-to-treat "
            f"contrast for {primary_metric} may be read causally for these two models "
            f"on the eligible task set ({worse} values are worse). "
            f"{SCOPE_LIMITATION}"
        )

    return ExperimentAnalysis(
        experiment_id=experiment_id,
        claim_status=claim_status,
        claim_language=claim_language,
        scope_limitation=SCOPE_LIMITATION,
        directional_license_note=DIRECTIONAL_LICENSE_NOTE,
        per_protocol_bias_note=PER_PROTOCOL_BIAS_NOTE,
        target_n_per_arm=target_n_per_arm,
        enrolled_per_arm=enrolled,
        compliance_rate=compliance_rate,
        compliance_threshold=compliance_threshold,
        primary_metric=primary_metric,
        primary_kind=primary_kind,
        itt=itt,
        per_protocol=pp,
        reasons=reasons,
    )
