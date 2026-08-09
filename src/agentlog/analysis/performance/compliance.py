from __future__ import annotations

import re
from dataclasses import dataclass


def normalize_model(model: str | None) -> str | None:
    if model is None:
        return None
    text = model.strip().lower()
    if not text:
        return None
    text = re.sub(r"[_\s]+", "-", text)
    return text


def models_match(assigned: str, observed: str | None) -> bool:
    a = normalize_model(assigned)
    o = normalize_model(observed)
    if a is None or o is None:
        return False
    if a == o:
        return True
    # Allow prefix/family equality when one side is a longer variant id.
    return a.startswith(o) or o.startswith(a)


@dataclass(frozen=True)
class ComplianceResult:
    status: str  # pending | complied | deviated | abandoned_before_start
    as_treated_model: str | None
    reason: str


def classify_compliance(
    *,
    assigned_model: str,
    as_treated_model: str | None,
    session_started: bool,
) -> ComplianceResult:
    if not session_started:
        return ComplianceResult(
            status="abandoned_before_start",
            as_treated_model=None,
            reason="no_linked_session",
        )
    if as_treated_model is None:
        return ComplianceResult(
            status="pending",
            as_treated_model=None,
            reason="model_unresolved",
        )
    if models_match(assigned_model, as_treated_model):
        return ComplianceResult(
            status="complied",
            as_treated_model=as_treated_model,
            reason="assigned_model_used",
        )
    return ComplianceResult(
        status="deviated",
        as_treated_model=as_treated_model,
        reason="different_model_used",
    )


def dominant_model_from_messages(
    message_models: list[str | None],
    *,
    share_threshold: float = 0.80,
) -> str | None:
    """Dominant assistant model when one variant has ≥ share_threshold of exchanges."""
    counts: dict[str, int] = {}
    for m in message_models:
        norm = normalize_model(m)
        if norm is None:
            continue
        counts[norm] = counts.get(norm, 0) + 1
    total = sum(counts.values())
    if total <= 0:
        return None
    best_model, best_n = max(counts.items(), key=lambda kv: kv[1])
    if best_n / total >= share_threshold:
        return best_model
    return None
