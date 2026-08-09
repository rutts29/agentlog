from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from agentlog.analysis.performance.stats import (
    IntervalEstimate,
    cluster_bootstrap_median,
    wilson_interval,
)

# Versioned precision-gate bounds (§4.7). Binding; n tiers never override these.
BOUND_VERSION = "precision_gates_v1"
WILSON_MAX_HALF_WIDTH = 0.10
CLUSTER_EVENT_FLOOR = 10
CLUSTER_N_HARD_FLOOR = 5
CONTINUOUS_RELATIVE_HALF_WIDTH = 0.40
# Absolute half-width floor for near-zero continuous rates (provisional, §4.7).
CONTINUOUS_ABSOLUTE_HALF_WIDTH_FLOOR = 0.5

Status = Literal["ok", "abstain", "unavailable"]
EvidenceTier = Literal["very_low", "low", "adequate"]


@dataclass
class AggregateCell:
    status: Status
    reason: str | None = None
    message: str | None = None
    estimate: float | None = None
    interval_low: float | None = None
    interval_high: float | None = None
    interval_method: str | None = None
    n_clusters: int = 0
    n_events: int = 0
    availability: float | None = None
    evidence_tier: EvidenceTier | None = None
    flags: list[str] = field(default_factory=list)
    session_ids: list[str] = field(default_factory=list)
    bound_version: str = BOUND_VERSION
    metric: str | None = None
    kind: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "message": self.message,
            "metric": self.metric,
            "kind": self.kind,
            "estimate": self.estimate,
            "interval": {
                "low": self.interval_low,
                "high": self.interval_high,
                "method": self.interval_method,
            },
            "n_clusters": self.n_clusters,
            "n_events": self.n_events,
            "availability": self.availability,
            "evidence_tier": self.evidence_tier,
            "flags": list(self.flags),
            "session_ids": list(self.session_ids),
            "bound_version": self.bound_version,
        }


def _evidence_tier(n: int) -> EvidenceTier:
    if n < 15:
        return "very_low"
    if n < 30:
        return "low"
    return "adequate"


def unavailable_cell(
    *,
    metric: str,
    kind: str,
    message: str,
    flags: list[str] | None = None,
    session_ids: list[str] | None = None,
) -> AggregateCell:
    return AggregateCell(
        status="unavailable",
        reason="metric_unavailable",
        message=message,
        metric=metric,
        kind=kind,
        flags=list(flags or ["source_capability"]),
        session_ids=list(session_ids or []),
    )


def abstain_cell(
    *,
    metric: str,
    kind: str,
    reason: str,
    message: str,
    n_clusters: int = 0,
    n_events: int = 0,
    availability: float | None = None,
    flags: list[str] | None = None,
    session_ids: list[str] | None = None,
    interval: IntervalEstimate | None = None,
    interval_method: str | None = None,
) -> AggregateCell:
    flags = list(flags or [])
    if "small_sample" not in flags and (
        n_clusters < 30 or reason in {"insufficient_precision", "insufficient_sample"}
    ):
        flags.append("small_sample")
    return AggregateCell(
        status="abstain",
        reason=reason,
        message=message,
        metric=metric,
        kind=kind,
        estimate=None,
        interval_low=interval.low if interval is not None else None,
        interval_high=interval.high if interval is not None else None,
        interval_method=interval_method,
        n_clusters=n_clusters,
        n_events=n_events,
        availability=availability,
        evidence_tier=None,
        flags=flags,
        session_ids=list(session_ids or []),
    )


def evaluate_binary_rate(
    *,
    metric: str,
    successes: int,
    n_clusters: int,
    session_ids: list[str],
    availability: float | None = None,
    extra_flags: list[str] | None = None,
) -> AggregateCell:
    """Apply §4.7 precision gate to a binary rate. Never returns a bare point estimate."""
    flags = list(extra_flags or [])
    if availability is not None and availability < 0.70:
        flags.append("outcome_missingness")
        return abstain_cell(
            metric=metric,
            kind="binary",
            reason="availability_too_low",
            message=(
                "Insufficient precision to aggregate: metric availability is below "
                "70% in this cell."
            ),
            n_clusters=n_clusters,
            n_events=successes,
            availability=availability,
            flags=flags,
            session_ids=session_ids,
        )

    if n_clusters < CLUSTER_N_HARD_FLOOR:
        return abstain_cell(
            metric=metric,
            kind="binary",
            reason="insufficient_sample",
            message=(
                "Insufficient data: fewer than 5 root clusters. "
                f"Showing {n_clusters} session(s) only — no aggregate."
            ),
            n_clusters=n_clusters,
            n_events=successes,
            availability=availability,
            flags=flags,
            session_ids=session_ids,
        )

    if n_clusters < CLUSTER_EVENT_FLOOR:
        iv = wilson_interval(successes, n_clusters)
        return abstain_cell(
            metric=metric,
            kind="binary",
            reason="insufficient_sample",
            message=(
                "Insufficient data: cluster-adjusted event basis is below 10. "
                "Sessions are listed; no rate is shown."
            ),
            n_clusters=n_clusters,
            n_events=successes,
            availability=availability,
            flags=flags,
            session_ids=session_ids,
            interval=iv,
            interval_method="wilson_95",
        )

    iv = wilson_interval(successes, n_clusters)
    half_width = (iv.high - iv.low) / 2.0
    if half_width > WILSON_MAX_HALF_WIDTH:
        return abstain_cell(
            metric=metric,
            kind="binary",
            reason="insufficient_precision",
            message=(
                "Insufficient precision to aggregate: Wilson 95% half-width "
                f"({half_width:.3f}) exceeds the {WILSON_MAX_HALF_WIDTH:.2f} gate. "
                "Sessions are listed; no point estimate is shown."
            ),
            n_clusters=n_clusters,
            n_events=successes,
            availability=availability,
            flags=flags,
            session_ids=session_ids,
            interval=iv,
            interval_method="wilson_95",
        )

    return AggregateCell(
        status="ok",
        metric=metric,
        kind="binary",
        estimate=iv.estimate,
        interval_low=iv.low,
        interval_high=iv.high,
        interval_method="wilson_95",
        n_clusters=n_clusters,
        n_events=successes,
        availability=availability,
        evidence_tier=_evidence_tier(n_clusters),
        flags=flags,
        session_ids=list(session_ids),
        message=None,
    )


def evaluate_continuous_rate(
    *,
    metric: str,
    per_cluster_values: list[float],
    session_ids: list[str],
    availability: float | None = None,
    extra_flags: list[str] | None = None,
    seed: int = 0,
) -> AggregateCell:
    """Apply §4.7 continuous/rate precision gate (cluster-bootstrap)."""
    flags = list(extra_flags or [])
    n_clusters = len(per_cluster_values)
    n_events = n_clusters

    if availability is not None and availability < 0.70:
        flags.append("outcome_missingness")
        return abstain_cell(
            metric=metric,
            kind="continuous",
            reason="availability_too_low",
            message=(
                "Insufficient precision to aggregate: metric availability is below "
                "70% in this cell."
            ),
            n_clusters=n_clusters,
            n_events=n_events,
            availability=availability,
            flags=flags,
            session_ids=session_ids,
        )

    if n_clusters < CLUSTER_N_HARD_FLOOR:
        return abstain_cell(
            metric=metric,
            kind="continuous",
            reason="insufficient_sample",
            message=(
                "Insufficient data: fewer than 5 root clusters. "
                "Sessions are listed; no aggregate."
            ),
            n_clusters=n_clusters,
            n_events=n_events,
            availability=availability,
            flags=flags,
            session_ids=session_ids,
        )

    if n_clusters < CLUSTER_EVENT_FLOOR:
        return abstain_cell(
            metric=metric,
            kind="continuous",
            reason="insufficient_sample",
            message=(
                "Insufficient data: cluster-adjusted event basis is below 10. "
                "Sessions are listed; no rate is shown."
            ),
            n_clusters=n_clusters,
            n_events=n_events,
            availability=availability,
            flags=flags,
            session_ids=session_ids,
        )

    iv = cluster_bootstrap_median(per_cluster_values, seed=seed)
    half_width = (iv.high - iv.low) / 2.0
    allowed = max(
        CONTINUOUS_RELATIVE_HALF_WIDTH * abs(iv.estimate),
        CONTINUOUS_ABSOLUTE_HALF_WIDTH_FLOOR,
    )
    if half_width > allowed:
        return abstain_cell(
            metric=metric,
            kind="continuous",
            reason="insufficient_precision",
            message=(
                "Insufficient precision to aggregate: cluster-bootstrap 95% "
                f"half-width ({half_width:.3f}) exceeds the versioned bound "
                f"({allowed:.3f}). Sessions are listed; no point estimate is shown."
            ),
            n_clusters=n_clusters,
            n_events=n_events,
            availability=availability,
            flags=flags,
            session_ids=session_ids,
            interval=iv,
            interval_method="cluster_bootstrap_median_95",
        )

    return AggregateCell(
        status="ok",
        metric=metric,
        kind="continuous",
        estimate=iv.estimate,
        interval_low=iv.low,
        interval_high=iv.high,
        interval_method="cluster_bootstrap_median_95",
        n_clusters=n_clusters,
        n_events=n_events,
        availability=availability,
        evidence_tier=_evidence_tier(n_clusters),
        flags=flags,
        session_ids=list(session_ids),
        message=None,
    )
