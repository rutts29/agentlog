from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OutcomeSpec:
    name: str
    direction: str  # higher_is_worse | higher_is_better | non_directional
    kind: str  # binary | continuous
    method_version: str
    # Directional interpretation is licensed only under randomized assignment.
    license: str
    role: str  # primary | secondary
    summary: str


# Primary: explicit user rejection/redo has unambiguous orientation (higher = worse).
# Licensed only under prospective randomization; does not transfer to observational Type B.
PRIMARY_OUTCOME = OutcomeSpec(
    name="had_rejected_redo",
    direction="higher_is_worse",
    kind="binary",
    method_version="rejected_redo_v1",
    license="randomized_experiment_only",
    role="primary",
    summary=(
        "Binary root-cluster indicator: at least one UX window with "
        "prior_outcome=rejected_redo. Higher is worse. Causal reading requires "
        "prospective randomization; not a quality ranking for observational history."
    ),
)

SECONDARY_OUTCOMES: tuple[OutcomeSpec, ...] = (
    OutcomeSpec(
        name="likely_abandoned",
        direction="higher_is_worse",
        kind="binary",
        method_version="abandonment_v1",
        license="randomized_experiment_only",
        role="secondary",
        summary=(
            "Binary abandonment proxy. Higher is worse under RCT balance; "
            "confidence-bearing, not a fact."
        ),
    ),
    OutcomeSpec(
        name="active_duration_seconds",
        direction="higher_is_worse",
        kind="continuous",
        method_version="active_duration_v1",
        license="randomized_experiment_only",
        role="secondary",
        summary=(
            "Active duration (idle-capped). Observationally confounded by task size; "
            "under random assignment, arm differences are interpretable as model "
            "effects on time for comparable tasks. Does not license observational "
            "efficiency rankings."
        ),
    ),
    OutcomeSpec(
        name="redirects_brakes_per_10_exchange_windows",
        direction="non_directional",
        kind="continuous",
        method_version="redirect_brake_v1",
        license="descriptive_only",
        role="secondary",
        summary=(
            "Descriptive steering-frequency measure. Higher is not worse. "
            "Reported as interaction style only; never a quality win condition."
        ),
    ),
)

DEFAULT_TARGET_N_PER_ARM = 16
DEFAULT_COMPLIANCE_THRESHOLD = 0.80

SCOPE_LIMITATION = (
    "This experiment randomizes one pre-registered comparison between two "
    "owner-chosen models on eligible tasks. It does not validate the full "
    "model × harness × effort × task matrix, and it does not make other "
    "historical cells causal."
)

DIRECTIONAL_LICENSE_NOTE = (
    "Directional interpretation of the primary outcome is licensed only by "
    "prospective random assignment within this experiment. It does not transfer "
    "to the observational usage & interaction-style profile."
)

PER_PROTOCOL_BIAS_NOTE = (
    "Per-protocol (as-treated) analysis reintroduces selection bias that "
    "randomization was meant to remove. Intention-to-treat is primary."
)
