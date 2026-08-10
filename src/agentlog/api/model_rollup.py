"""One row per model identity for every model-facing payload.

Model surfaces must key on the value they render. Grouping by
``(model, harness)`` while the UI prints only the model produced duplicate
rows for the same model, so the collapse happens here instead of in each
endpoint.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from agentlog.normalize.model_identity import (
    UNKNOWN_MODEL_LABEL,
    normalize_model_key,
)
from agentlog.registry.models import AGENT_PROFILES, PLACEHOLDERS, PROVIDERS

# Model grains. A metric may only group by one of these, and every numerator
# and denominator in that metric must use the same one. Session counts belong
# to SESSION_START_MODEL; message and token counts belong to MESSAGE_MODEL.
SESSION_START_MODEL = "session_start_model"
MESSAGE_MODEL = "message_model"

GRAIN_DESCRIPTIONS: dict[str, str] = {
    SESSION_START_MODEL: (
        "sessions.model_canonical — the model the session was recorded under. "
        "Counts sessions, not messages; a model switched to mid-session does "
        "not appear here."
    ),
    MESSAGE_MODEL: (
        "the model resolved per message (messages.model_canonical, falling "
        "back to the session model), which is what token usage rows attach "
        "to. Captures mid-session model switches."
    ),
}

SESSION_START_MODEL_SQL = (
    "COALESCE(NULLIF(s.model_canonical, ''), '(unknown)')"
)
MESSAGE_MODEL_SQL = (
    "COALESCE("
    "NULLIF(m.model_canonical, ''), "
    "NULLIF(s.model_canonical, ''), "
    "'(unknown)')"
)


def strict_message_model_sql(
    *, message_alias: str = "m", session_alias: str = "s"
) -> str:
    """Resolve directly observed message models without backfilling a switched session."""
    direct = (
        f"COALESCE(NULLIF({message_alias}.model_canonical, ''), '(unknown)')"
    )
    fallback = (
        f"COALESCE(NULLIF({session_alias}.model_canonical, ''), '(unknown)')"
    )
    return (
        "CASE WHEN EXISTS ("
        "SELECT 1 FROM messages observed_model "
        f"WHERE observed_model.session_id = {session_alias}.id "
        "AND observed_model.role = 'assistant' "
        "AND observed_model.model_canonical IS NOT NULL "
        "AND observed_model.model_canonical <> ''"
        f") THEN {direct} ELSE {fallback} END"
    )
# Usage rows are message-level, so they resolve through their linked message
# before falling back to the session.
USAGE_MODEL_SQL = (
    "COALESCE("
    "NULLIF(u.model_canonical, ''), "
    "NULLIF(m.model_canonical, ''), "
    "NULLIF(s.model_canonical, ''), "
    "'(unknown)')"
)

UNKNOWN_REASONS: dict[str, str] = {
    "no_model_recorded": "harness recorded no model on the session",
    "agent_profile": "agent/profile identity with no declared base model",
    "provider_name": "provider name written into the model field",
    "placeholder": "placeholder value such as default/auto/synthetic",
}


def collapse_by_model(
    rows: Iterable[Mapping[str, Any]],
    *,
    count_key: str = "sessions",
    model_key: str = "model",
    harness_key: str = "harness",
) -> list[dict[str, Any]]:
    """Merge harness-split rows into a single row per model label.

    Each result keeps a per-harness breakdown so nothing that was visible
    before is lost, and callers can still show which harnesses a model ran
    under without inventing extra rows.
    """
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for row in rows:
        model = str(row[model_key])
        count = int(row[count_key] or 0)
        harness = row.get(harness_key)
        entry = merged.get(model)
        if entry is None:
            entry = {model_key: model, count_key: 0, "harnesses": {}}
            merged[model] = entry
            order.append(model)
        entry[count_key] += count
        if harness is not None:
            key = str(harness)
            entry["harnesses"][key] = entry["harnesses"].get(key, 0) + count
    out: list[dict[str, Any]] = []
    for model in order:
        entry = merged[model]
        breakdown = sorted(
            entry["harnesses"].items(), key=lambda kv: (-kv[1], kv[0])
        )
        out.append(
            {
                model_key: model,
                count_key: entry[count_key],
                "harnesses": [
                    {"harness": h, count_key: n} for h, n in breakdown
                ],
            }
        )
    out.sort(key=lambda item: (-item[count_key], item[model_key]))
    return out


def unknown_reason(raw_model: str | None) -> str:
    """Why a session/message could not be resolved to a model."""
    if raw_model is None or not str(raw_model).strip():
        return "no_model_recorded"
    key = normalize_model_key(str(raw_model))
    if key in AGENT_PROFILES:
        return "agent_profile"
    if key in PROVIDERS:
        return "provider_name"
    if key in PLACEHOLDERS:
        return "placeholder"
    return "no_model_recorded"


def unknown_breakdown(
    rows: Iterable[Mapping[str, Any]],
    *,
    raw_key: str = "model_raw",
    count_key: str = "sessions",
) -> dict[str, Any]:
    """Explain the ``(unknown)`` bucket instead of leaving it opaque."""
    by_reason: dict[str, dict[str, Any]] = {}
    total = 0
    for row in rows:
        count = int(row[count_key] or 0)
        total += count
        reason = unknown_reason(row.get(raw_key))
        entry = by_reason.setdefault(
            reason,
            {
                "reason": reason,
                "description": UNKNOWN_REASONS[reason],
                count_key: 0,
                "raw_values": {},
            },
        )
        entry[count_key] += count
        raw = row.get(raw_key)
        label = str(raw).strip() if raw is not None and str(raw).strip() else "(null)"
        entry["raw_values"][label] = entry["raw_values"].get(label, 0) + count
    items = []
    for entry in by_reason.values():
        raws = sorted(entry["raw_values"].items(), key=lambda kv: (-kv[1], kv[0]))
        items.append(
            {
                "reason": entry["reason"],
                "description": entry["description"],
                count_key: entry[count_key],
                "raw_values": [
                    {"value": v, count_key: n} for v, n in raws
                ],
            }
        )
    items.sort(key=lambda item: (-item[count_key], item["reason"]))
    return {
        "label": UNKNOWN_MODEL_LABEL,
        count_key: total,
        "reasons": items,
    }
