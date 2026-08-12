"""Activity calendar and descriptive harness/model rollups."""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import datetime, time, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from agentlog.api.deps import get_conn
from agentlog.api.identity_aggregates import visible_logical_sessions
from agentlog.api.model_rollup import (
    GRAIN_DESCRIPTIONS,
    MESSAGE_MODEL,
    MESSAGE_MODEL_SQL,
    SESSION_START_MODEL,
    SESSION_START_MODEL_SQL,
)
from agentlog.api.ranges import (
    DEFAULT_RANGE_KEY,
    TimeRange,
    parse_global_range,
    range_params,
    session_time_clause as _session_time_clause,
)
from agentlog.api import tokens as token_metrics
router = APIRouter(tags=["activity"])

def _parse_range_dep(
    range: str = Query(DEFAULT_RANGE_KEY, alias="range"),
    start: str | None = None,
    end: str | None = None,
) -> TimeRange:
    try:
        return parse_global_range(range, custom_start=start, custom_end=end)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _duration_seconds_sql(alias: str = "s") -> str:
    return f"""
    CASE
      WHEN {alias}.started_at IS NOT NULL AND {alias}.ended_at IS NOT NULL
           AND julianday({alias}.ended_at) IS NOT NULL
           AND julianday({alias}.started_at) IS NOT NULL
      THEN CAST(
        (julianday({alias}.ended_at) - julianday({alias}.started_at)) * 86400
        AS INTEGER
      )
      ELSE NULL
    END
    """


def _day_list(tr: TimeRange) -> list[str]:
    if tr.key == "24h":
        return [tr.end.astimezone(timezone.utc).date().isoformat()]
    end = tr.end.astimezone(timezone.utc).date()
    if tr.start is None:
        return []
    start = tr.start.astimezone(timezone.utc).date()
    if tr.end.astimezone(timezone.utc).time() == time.min:
        end -= timedelta(days=1)
    days: list[str] = []
    cur = start
    while cur <= end:
        days.append(cur.isoformat())
        cur += timedelta(days=1)
    return days


def _calendar_day(timestamp: str, tr: TimeRange) -> str:
    if tr.key == "24h":
        return tr.end.astimezone(timezone.utc).date().isoformat()
    return timestamp[:10]


def _rolling_token_day(
    conn: sqlite3.Connection, tr: TimeRange
) -> dict[str, dict[str, Any]]:
    series = token_metrics.timeseries_daily(conn, tr)["series"]
    if tr.key != "24h":
        return {item["day"]: item for item in series}
    totals: dict[str, int | None] = {}
    for key in (
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "total_tokens",
    ):
        values = [item["totals"].get(key) for item in series]
        known = [int(value) for value in values if value is not None]
        totals[key] = sum(known) if known else None
    return {
        tr.end.astimezone(timezone.utc).date().isoformat(): {
            "sessions_with_usage": sum(
                int(item["sessions_with_usage"]) for item in series
            ),
            "totals": totals,
        }
    }


def _streaks(active_days: list[str], *, end: datetime) -> dict[str, int]:
    """Current streak ends on the latest calendar day <= end with activity."""
    if not active_days:
        return {"current": 0, "longest": 0}
    ordered = sorted(set(active_days), reverse=True)
    current = 1
    for i in range(1, len(ordered)):
        prev = datetime.fromisoformat(ordered[i - 1]).date()
        cur = datetime.fromisoformat(ordered[i]).date()
        if (prev - cur).days == 1:
            current += 1
        else:
            break
    # If the most recent active day is not today/yesterday relative to end,
    # the current streak is broken (GitHub-style: must include recent day).
    end_day = end.astimezone(timezone.utc).date()
    latest = datetime.fromisoformat(ordered[0]).date()
    gap = (end_day - latest).days
    if gap > 1:
        current = 0
    longest = 1
    run = 1
    for i in range(1, len(ordered)):
        prev = datetime.fromisoformat(ordered[i - 1]).date()
        cur = datetime.fromisoformat(ordered[i]).date()
        if (prev - cur).days == 1:
            run += 1
            longest = max(longest, run)
        else:
            run = 1
    return {"current": current, "longest": max(longest, current if current else 1)}


def activity_calendar(conn: sqlite3.Connection, tr: TimeRange) -> dict[str, Any]:
    """GitHub-style daily activity matrix over visible logical sessions."""
    time_sql, params = _session_time_clause(tr)
    sessions = visible_logical_sessions(
        conn,
        conn.execute(
            f"""
            SELECT s.id, s.harness, s.started_at
            FROM sessions s
            WHERE s.started_at IS NOT NULL AND {time_sql}
            """,
            params,
        ).fetchall(),
    )
    by_metric = {session.metric_session_id: session for session in sessions}
    by_day: dict[str, dict[str, Any]] = {}
    for session in sessions:
        day = _calendar_day(str(session.row["started_at"]), tr)
        entry = by_day.setdefault(
            day, {"sessions": 0, "messages": 0, "tool_events": 0, "harnesses": set()}
        )
        entry["sessions"] += 1
        entry["harnesses"].add(session.logical_harness)
    metric_ids = sorted(by_metric)
    if metric_ids:
        placeholders = ",".join("?" for _ in metric_ids)
        for table, key in (("messages", "messages"), ("tool_events", "tool_events")):
            rows = conn.execute(
                f"SELECT session_id, COUNT(*) AS c FROM {table} "
                f"WHERE session_id IN ({placeholders}) GROUP BY session_id",
                metric_ids,
            ).fetchall()
            for row in rows:
                session = by_metric[str(row["session_id"])]
                day = _calendar_day(str(session.row["started_at"]), tr)
                by_day[day][key] += int(row["c"])
    token_days = _rolling_token_day(conn, tr)
    days = _day_list(tr)
    if not days and by_day:
        days = sorted(by_day)

    cells: list[dict[str, Any]] = []
    max_sessions = 0
    max_messages = 0
    max_tool_events = 0
    max_total_tokens = 0
    active_days: list[str] = []

    for day in days:
        row = by_day.get(day)
        if row is None:
            cell = {
                "date": day,
                "sessions": 0,
                "messages": 0,
                "tool_events": 0,
                "active_harnesses": [],
                "total_tokens": None,
                "input_tokens": None,
                "output_tokens": None,
                "sessions_with_tokens": 0,
                "tokens_known": False,
            }
        else:
            token_row = token_days.get(day)
            totals = token_row["totals"] if token_row else {}
            total_tokens = totals.get("total_tokens")
            # Prefer total_tokens; if only input/output present, sum those.
            if total_tokens is None:
                inp = totals.get("input_tokens")
                out = totals.get("output_tokens")
                if inp is not None or out is not None:
                    total_tokens = int(inp or 0) + int(out or 0)
            cell = {
                "date": day,
                "sessions": int(row["sessions"]),
                "messages": int(row["messages"]),
                "tool_events": int(row["tool_events"]),
                "active_harnesses": sorted(row["harnesses"]),
                "total_tokens": total_tokens,
                "input_tokens": (
                    int(totals["input_tokens"])
                    if totals.get("input_tokens") is not None
                    else None
                ),
                "output_tokens": (
                    int(totals["output_tokens"])
                    if totals.get("output_tokens") is not None
                    else None
                ),
                "sessions_with_tokens": int(
                    token_row["sessions_with_usage"] if token_row else 0
                ),
                "tokens_known": total_tokens is not None,
            }
            if cell["sessions"] > 0:
                active_days.append(day)
        cells.append(cell)
        max_sessions = max(max_sessions, cell["sessions"])
        max_messages = max(max_messages, cell["messages"])
        max_tool_events = max(max_tool_events, cell["tool_events"])
        if cell["total_tokens"] is not None:
            max_total_tokens = max(max_total_tokens, cell["total_tokens"])

    streaks = _streaks(active_days, end=tr.end)
    return {
        "days": cells,
        "max": {
            "sessions": max_sessions,
            "messages": max_messages,
            "tool_events": max_tool_events,
            "total_tokens": max_total_tokens,
        },
        "streaks": {
            "current_days": streaks["current"],
            "longest_days": streaks["longest"],
        },
        "active_days": len(active_days),
        "note": (
            "One cell per calendar day. The 24h window is one rolling cell; "
            "empty days are included for longer windows. "
            "total_tokens sums additive contributions only (Claude message "
            "usage + Codex final session_cumulative); null when unknown. "
            "Cursor/Warp contribute activity counts but not tokens."
        ),
    }


def _model_switch_counts(
    conn: sqlite3.Connection, tr: TimeRange
) -> tuple[dict[str, int], dict[tuple[str, str], int]]:
    """Count mid-session model changes; keyed by harness and (harness, model)."""
    time_sql, params = _session_time_clause(tr)
    rows = conn.execute(
        f"""
        WITH ordered AS (
            SELECT
                m.session_id AS session_id,
                s.harness AS harness,
                {MESSAGE_MODEL_SQL} AS model,
                LAG({MESSAGE_MODEL_SQL}) OVER (
                    PARTITION BY m.session_id ORDER BY m.seq
                ) AS prev_model
            FROM messages m
            JOIN sessions s ON s.id = m.session_id
            WHERE {time_sql}
        )
        SELECT harness, model, COUNT(*) AS switches
        FROM ordered
        WHERE prev_model IS NOT NULL AND prev_model != model
        GROUP BY harness, model
        """,
        params,
    ).fetchall()
    by_harness: dict[str, int] = defaultdict(int)
    by_model: dict[tuple[str, str], int] = defaultdict(int)
    for r in rows:
        h = str(r["harness"])
        model = str(r["model"])
        n = int(r["switches"])
        by_harness[h] += n
        by_model[(h, model)] += n
    return by_harness, by_model


def activity_rollup(conn: sqlite3.Connection, tr: TimeRange) -> dict[str, Any]:
    """Per-harness and per-model descriptive activity rollup."""
    time_sql, params = _session_time_clause(tr)
    dur_sql = _duration_seconds_sql()

    harness_base = list(
        conn.execute(
            f"""
            WITH bounded AS (
                SELECT
                    s.id AS session_id,
                    s.harness AS harness,
                    {dur_sql} AS duration_seconds
                FROM sessions s
                WHERE {time_sql}
            ),
            sess AS (
                SELECT
                    harness,
                    COUNT(*) AS sessions,
                    SUM(CASE WHEN duration_seconds IS NOT NULL THEN 1 ELSE 0 END)
                        AS sessions_with_duration,
                    AVG(duration_seconds) AS mean_duration_seconds
                FROM bounded
                GROUP BY harness
            ),
            msgs AS (
                SELECT b.harness AS harness, COUNT(*) AS messages
                FROM messages m
                JOIN bounded b ON b.session_id = m.session_id
                GROUP BY b.harness
            ),
            tools AS (
                SELECT b.harness AS harness, COUNT(*) AS tool_events
                FROM tool_events t
                JOIN bounded b ON b.session_id = t.session_id
                GROUP BY b.harness
            )
            SELECT
                sess.harness AS harness,
                sess.sessions AS sessions,
                sess.sessions_with_duration AS sessions_with_duration,
                sess.mean_duration_seconds AS mean_duration_seconds,
                COALESCE(msgs.messages, 0) AS messages,
                COALESCE(tools.tool_events, 0) AS tool_events
            FROM sess
            LEFT JOIN msgs ON msgs.harness = sess.harness
            LEFT JOIN tools ON tools.harness = sess.harness
            ORDER BY sess.sessions DESC, sess.harness
            """,
            params,
        )
    )

    # Session grain: one row per (harness, session start model). Every column
    # here counts sessions or their session-scoped children.
    session_model_base = list(
        conn.execute(
            f"""
            WITH bounded AS (
                SELECT
                    s.id AS session_id,
                    s.harness AS harness,
                    {SESSION_START_MODEL_SQL} AS model,
                    {dur_sql} AS duration_seconds
                FROM sessions s
                WHERE {time_sql}
            ),
            sess AS (
                SELECT
                    harness,
                    model,
                    COUNT(*) AS sessions,
                    SUM(CASE WHEN duration_seconds IS NOT NULL THEN 1 ELSE 0 END)
                        AS sessions_with_duration,
                    AVG(duration_seconds) AS mean_duration_seconds
                FROM bounded
                GROUP BY harness, model
            ),
            tools AS (
                SELECT b.harness AS harness, b.model AS model, COUNT(*) AS tool_events
                FROM tool_events t
                JOIN bounded b ON b.session_id = t.session_id
                GROUP BY b.harness, b.model
            )
            SELECT
                sess.harness AS harness,
                sess.model AS model,
                sess.sessions AS sessions,
                sess.sessions_with_duration AS sessions_with_duration,
                sess.mean_duration_seconds AS mean_duration_seconds,
                COALESCE(tools.tool_events, 0) AS tool_events
            FROM sess
            LEFT JOIN tools
              ON tools.harness = sess.harness AND tools.model = sess.model
            ORDER BY sess.sessions DESC, sess.harness, sess.model
            """,
            params,
        )
    )

    # Message grain: driven by the messages themselves, so a model switched to
    # mid-session gets its own row instead of vanishing into a session join.
    message_model_base = list(
        conn.execute(
            f"""
            SELECT
                s.harness AS harness,
                {MESSAGE_MODEL_SQL} AS model,
                COUNT(*) AS messages,
                COUNT(DISTINCT m.session_id) AS sessions_seen
            FROM messages m
            JOIN sessions s ON s.id = m.session_id
            WHERE {time_sql}
            GROUP BY s.harness, {MESSAGE_MODEL_SQL}
            ORDER BY messages DESC, s.harness
            """,
            params,
        )
    )

    effort_by_harness: dict[str, dict[str, int]] = defaultdict(
        lambda: defaultdict(int)
    )
    effort_by_model: dict[tuple[str, str], dict[str, int]] = defaultdict(
        lambda: defaultdict(int)
    )
    for r in conn.execute(
        f"""
        SELECT
            s.harness AS harness,
            {SESSION_START_MODEL_SQL} AS model,
            COALESCE(NULLIF(s.effort, ''), '(none)') AS effort,
            COUNT(*) AS c
        FROM sessions s
        WHERE {time_sql}
        GROUP BY s.harness, model, effort
        """,
        params,
    ):
        effort_by_harness[str(r["harness"])][str(r["effort"])] += int(r["c"])
        effort_by_model[(str(r["harness"]), str(r["model"]))][
            str(r["effort"])
        ] += int(r["c"])

    switches_h, switches_m = _model_switch_counts(conn, tr)

    def _effort_dist(bucket: dict[str, int]) -> list[dict[str, Any]]:
        total = sum(bucket.values()) or 1
        return [
            {
                "effort": name,
                "sessions": n,
                "share": n / total,
            }
            for name, n in sorted(bucket.items(), key=lambda x: (-x[1], x[0]))
        ]

    by_harness = []
    for r in harness_base:
        h = str(r["harness"])
        mean = r["mean_duration_seconds"]
        by_harness.append(
            {
                "harness": h,
                "sessions": int(r["sessions"]),
                "messages": int(r["messages"]),
                "tool_events": int(r["tool_events"]),
                "mean_session_duration_seconds": (
                    float(mean) if mean is not None else None
                ),
                "sessions_with_duration": int(r["sessions_with_duration"] or 0),
                "effort_distribution": _effort_dist(effort_by_harness.get(h, {})),
                "model_switch_count": switches_h.get(h, 0),
            }
        )

    by_session_start_model = []
    for r in session_model_base:
        h = str(r["harness"])
        model = str(r["model"])
        mean = r["mean_duration_seconds"]
        by_session_start_model.append(
            {
                "harness": h,
                "session_start_model": model,
                "sessions": int(r["sessions"]),
                "tool_events": int(r["tool_events"]),
                "mean_session_duration_seconds": (
                    float(mean) if mean is not None else None
                ),
                "sessions_with_duration": int(r["sessions_with_duration"] or 0),
                "effort_distribution": _effort_dist(
                    effort_by_model.get((h, model), {})
                ),
            }
        )

    by_message_model = [
        {
            "harness": str(r["harness"]),
            "message_model": str(r["model"]),
            "messages": int(r["messages"]),
            "sessions_seen": int(r["sessions_seen"]),
            "model_switch_count": switches_m.get(
                (str(r["harness"]), str(r["model"])), 0
            ),
        }
        for r in message_model_base
    ]

    profile_rows = list(
        conn.execute(
            f"""
            SELECT
                s.harness AS harness,
                s.agent_profile AS agent_profile,
                COUNT(*) AS sessions,
                SUM(
                  (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id)
                ) AS messages,
                SUM(
                  (SELECT COUNT(*) FROM tool_events t WHERE t.session_id = s.id)
                ) AS tool_events
            FROM sessions s
            WHERE {time_sql}
              AND s.agent_profile IS NOT NULL
              AND TRIM(s.agent_profile) != ''
            GROUP BY s.harness, s.agent_profile
            ORDER BY sessions DESC, s.harness, s.agent_profile
            """,
            params,
        )
    )
    by_agent_profile = [
        {
            "harness": str(r["harness"]),
            "agent_profile": str(r["agent_profile"]),
            "sessions": int(r["sessions"]),
            "messages": int(r["messages"] or 0),
            "tool_events": int(r["tool_events"] or 0),
        }
        for r in profile_rows
    ]

    return {
        "by_harness": by_harness,
        "by_session_start_model": by_session_start_model,
        "by_message_model": by_message_model,
        "by_agent_profile": by_agent_profile,
        "grains": {
            "by_harness": "harness",
            "by_session_start_model": SESSION_START_MODEL,
            "by_message_model": MESSAGE_MODEL,
            "by_agent_profile": "session_agent_profile",
        },
        "grain_notes": {
            SESSION_START_MODEL: GRAIN_DESCRIPTIONS[SESSION_START_MODEL],
            MESSAGE_MODEL: GRAIN_DESCRIPTIONS[MESSAGE_MODEL],
        },
        "reconciliation": _reconcile(
            by_harness, by_session_start_model, by_message_model
        ),
        "note": (
            "Descriptive activity rollup only at physical-session grain. Model rows are split by grain: "
            "by_session_start_model counts sessions and their tool events; "
            "by_message_model counts messages and reconciles to the harness "
            "message total. by_agent_profile counts agent/profile identities "
            "separately. model_switch_count counts adjacent message pairs whose "
            "resolved model differs. Mean duration uses sessions with both "
            "started_at and ended_at."
        ),
        "identity_grain": "physical_sessions",
    }


def _reconcile(
    by_harness: list[dict[str, Any]],
    by_session_start_model: list[dict[str, Any]],
    by_message_model: list[dict[str, Any]],
) -> dict[str, Any]:
    """Additive model rows must sum back to their harness totals.

    A mismatch means rows were dropped or double counted by a grain mix, so
    the numbers are reported rather than silently trusted.
    """
    checks: list[dict[str, Any]] = []
    for spec in (
        ("sessions", "sessions", by_session_start_model, SESSION_START_MODEL),
        ("tool_events", "tool_events", by_session_start_model, SESSION_START_MODEL),
        ("messages", "messages", by_message_model, MESSAGE_MODEL),
    ):
        harness_field, model_field, rows, grain = spec
        for h in by_harness:
            name = str(h["harness"])
            expected = int(h[harness_field])
            actual = sum(
                int(r[model_field]) for r in rows if r["harness"] == name
            )
            checks.append(
                {
                    "harness": name,
                    "metric": harness_field,
                    "grain": grain,
                    "harness_total": expected,
                    "model_rows_total": actual,
                    "ok": expected == actual,
                }
            )
    return {
        "checks": checks,
        "ok": all(c["ok"] for c in checks),
        "note": (
            "Every additive model row set sums to its harness total. A false "
            "'ok' means a grain mismatch is dropping or duplicating rows."
        ),
    }


@router.get("/api/activity/calendar")
def calendar_endpoint(
    conn: sqlite3.Connection = Depends(get_conn),
    tr: TimeRange = Depends(_parse_range_dep),
) -> dict:
    return {**range_params(tr), **activity_calendar(conn, tr)}


@router.get("/api/activity/rollup")
def rollup_endpoint(
    conn: sqlite3.Connection = Depends(get_conn),
    tr: TimeRange = Depends(_parse_range_dep),
) -> dict:
    return {**range_params(tr), **activity_rollup(conn, tr)}
