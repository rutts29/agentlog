"""Single-request payload for the dashboard Overview surface."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import sqlite3
from typing import Any

from agentlog.api import descriptive, queries, tokens
from agentlog.api.ranges import TimeRange, range_params


def request_cache_key(
    range_key: str | None,
    custom_start: str | None,
    custom_end: str | None,
) -> tuple[str, str | None, str | None]:
    """Use the caller's exact dashboard window as the aggregate cache key."""
    key = (range_key or "24h").strip().lower()
    if key == "custom":
        return key, custom_start, custom_end
    return key, None, None


def summary_payload(conn: sqlite3.Connection, tr: TimeRange) -> dict[str, Any]:
    current = queries.count_sessions(conn, tr)
    prev = None
    delta = None
    prev_ledger: dict[str, Any] | None = None
    if tr.prev_start is not None and tr.prev_end is not None:
        prev_tr = TimeRange(
            key="prev",
            start=tr.prev_start,
            end=tr.prev_end,
            prev_start=None,
            prev_end=None,
        )
        prev = queries.count_sessions(conn, prev_tr)
        if prev > 0:
            delta = (current - prev) / prev
        prev_ledger = descriptive.ledger_counts(conn, prev_tr)

    lead = queries.semantic_lead_metric(conn, tr)
    streak = queries.streak_days(conn, tr)
    ledger = descriptive.ledger_counts(conn, tr)
    token_totals = tokens.corpus_totals(conn, tr)

    def count_kpi(key: str, label: str) -> dict[str, Any]:
        value = ledger[key]
        previous = prev_ledger[key] if prev_ledger is not None else None
        ratio = (
            (value - previous) / previous
            if previous is not None and previous > 0
            else None
        )
        return {
            "value": value,
            "previous": previous,
            "delta_ratio": ratio,
            "kind": "count",
            "label": label,
        }

    return {
        **range_params(tr),
        "kpis": {
            "sessions": {
                "value": current,
                "previous": prev,
                "delta_ratio": delta,
                "kind": "count",
                "label": "Sessions",
            },
            "messages": count_kpi("messages", "Messages"),
            "tool_events": count_kpi("tool_events", "Tool calls"),
            "windows": count_kpi("windows", "Exchange windows"),
            "auto_reviews": count_kpi("auto_reviews", "Auto-reviews"),
            "tokens_est": token_totals,
            "cost_est": token_totals.get("cost"),
            "interaction_style": lead.to_dict(),
            "streak": {
                "current_days": streak["current"],
                "longest_days": streak["longest"],
                "label": "Active days in range",
                "note": "Calendar days with at least one session start.",
            },
        },
        "ledger": ledger,
        "flags": lead.flags,
    }


@contextmanager
def read_snapshot(conn: sqlite3.Connection) -> Iterator[None]:
    if conn.in_transaction:
        conn.execute("SAVEPOINT overview_read_snapshot")
        try:
            yield
        finally:
            conn.execute("ROLLBACK TO overview_read_snapshot")
            conn.execute("RELEASE overview_read_snapshot")
        return

    conn.execute("BEGIN")
    try:
        yield
    finally:
        conn.rollback()


def overview_payload(conn: sqlite3.Connection, tr: TimeRange) -> dict[str, Any]:
    """Build all Overview sections on the request's one read-only connection.

    Each section delegates to the same computation used by its standalone API;
    the aggregate is a transport optimization, not a second metric definition.
    Graph and Attention remain independent so their heavier work cannot delay
    the core dashboard response.
    """
    with read_snapshot(conn):
        return _overview_payload(conn, tr)


def _overview_payload(conn: sqlite3.Connection, tr: TimeRange) -> dict[str, Any]:
    summary = summary_payload(conn, tr)
    timeseries = {
        **range_params(tr),
        "by": "harness",
        "series": descriptive.sessions_daily_by(conn, tr, by="harness"),
        "note": "Daily session counts by harness. Descriptive usage only.",
    }
    models = {**range_params(tr), **queries.models_profile(conn, tr)}
    heatmap = {**range_params(tr), **queries.activity_heatmap(conn, tr)}
    projects = {**range_params(tr), "items": queries.top_projects(conn, tr)}
    recent = {
        **range_params(tr),
        "items": queries.recent_sessions(conn, tr, limit=8),
    }
    tools = {
        **range_params(tr),
        **descriptive.tool_usage(conn, tr, limit=12),
    }
    kinds = {
        **range_params(tr),
        **descriptive.request_kind_distribution(conn, tr),
    }
    distributions = {
        **range_params(tr),
        **descriptive.duration_and_volume(conn, tr),
    }
    return {
        **range_params(tr),
        "summary": summary,
        "timeseries": timeseries,
        "models": models,
        "heatmap": heatmap,
        "projects": projects,
        "recent": recent,
        "tools": tools,
        "kinds": kinds,
        "distributions": distributions,
    }
