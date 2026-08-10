"""Ungated descriptive analytics and browse/search queries.

Counts, distributions, listings, and full-text search are always defensible.
Comparative rate claims and quality judgments stay in queries.py behind gates.
"""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from agentlog.api.identity_aggregates import (
    VisibleLogicalSession,
    visible_logical_sessions,
)
from agentlog.api.model_rollup import collapse_by_model, strict_message_model_sql
from agentlog.api.ranges import TimeRange, session_time_clause as _session_time_clause
from agentlog.session_identity import (
    build_identity_context,
    logical_orchestrator_id,
    logical_root_session_id,
    logical_projection,
    provider_backings,
    provider_root_backings,
    provider_root_shadow_ids,
)
from agentlog.normalize.model_identity import display_model

_SORT_COLUMNS = {
    "started_at": "COALESCE(s.started_at, '')",
    "duration": "duration_seconds",
    "messages": "message_count",
    "tools": "tool_count",
    "windows": "window_count",
    "model": "COALESCE(s.model_canonical, '')",
    "harness": "s.harness",
    "effort": "COALESCE(s.effort, '')",
    "project": "project_label",
    "branch": "COALESCE(s.branch, '')",
}


def _project_label(repo: str | None, cwd: str | None) -> str:
    if repo:
        text = repo.strip()
        if text.startswith("http"):
            path = urlparse(text).path.rstrip("/")
            name = path.split("/")[-1]
            return name.removesuffix(".git") or text
        if "/" in text or text.startswith("-") or text.startswith("Users-"):
            return text.split("/")[-1].lstrip("-") or text
        return text
    if cwd:
        return cwd.rstrip("/").split("/")[-1] or "(unknown)"
    return "(unknown)"


def _duration_seconds_sql(alias: str = "s") -> str:
    # SQLite julianday difference → seconds when both timestamps parse.
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


def _fts_match_query(raw: str) -> str | None:
    tokens = re.findall(r"[A-Za-z0-9_./+-]+", raw or "")
    if not tokens:
        return None
    return " AND ".join(f'"{t}"' for t in tokens[:24])


def _aggregate_sessions(
    conn: sqlite3.Connection, tr: TimeRange
) -> list[VisibleLogicalSession]:
    where, params = _session_time_clause(tr)
    rows = conn.execute(
        f"""
        SELECT s.id, s.harness, s.started_at, s.model_canonical, s.effort,
               s.branch, s.repo, s.cwd, s.parent_session_id
        FROM sessions s
        WHERE {where}
        """,
        params,
    ).fetchall()
    return visible_logical_sessions(conn, rows)


def _metric_rows(
    conn: sqlite3.Connection, sessions: list[VisibleLogicalSession]
) -> dict[str, sqlite3.Row]:
    ids = sorted({session.metric_session_id for session in sessions})
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT id, model_canonical FROM sessions WHERE id IN ({placeholders})",
        ids,
    ).fetchall()
    return {str(row["id"]): row for row in rows}


def _model_label(row: sqlite3.Row | None) -> str:
    value = row["model_canonical"] if row is not None else None
    return str(value or "(unknown)")


def ledger_counts(conn: sqlite3.Connection, tr: TimeRange) -> dict[str, Any]:
    sessions = _aggregate_sessions(conn, tr)
    metric_ids = sorted({session.metric_session_id for session in sessions})
    if not metric_ids:
        return {
            "sessions": 0,
            "messages": 0,
            "tool_events": 0,
            "windows": 0,
            "skill_exposures": 0,
            "auto_reviews": 0,
            "worker_briefs": 0,
            "child_sessions": 0,
        }
    placeholders = ",".join("?" for _ in metric_ids)
    row = conn.execute(
        f"""
        SELECT
          (SELECT COUNT(*) FROM messages WHERE session_id IN ({placeholders})) AS messages,
          (SELECT COUNT(*) FROM tool_events WHERE session_id IN ({placeholders})) AS tool_events,
          (SELECT COUNT(*) FROM exchange_windows WHERE session_id IN ({placeholders})) AS windows,
          (SELECT COUNT(*) FROM skill_exposures WHERE session_id IN ({placeholders})) AS skill_exposures,
          (SELECT COUNT(*) FROM auto_review_observations a
             JOIN exchange_windows w ON w.id = a.window_id
             WHERE w.session_id IN ({placeholders})) AS auto_reviews,
          (SELECT COUNT(*) FROM worker_task_observations wt
             JOIN exchange_windows w ON w.id = wt.window_id
             WHERE w.session_id IN ({placeholders})) AS worker_briefs
        """,
        [*metric_ids, *metric_ids, *metric_ids, *metric_ids, *metric_ids, *metric_ids],
    ).fetchone()
    return {
        "sessions": len(sessions),
        "messages": int(row["messages"] or 0),
        "tool_events": int(row["tool_events"] or 0),
        "windows": int(row["windows"] or 0),
        "skill_exposures": int(row["skill_exposures"] or 0),
        "auto_reviews": int(row["auto_reviews"] or 0),
        "worker_briefs": int(row["worker_briefs"] or 0),
        "child_sessions": sum(
            1 for session in sessions if session.row["parent_session_id"] is not None
        ),
    }


def sessions_daily_by(
    conn: sqlite3.Connection, tr: TimeRange, *, by: str
) -> list[dict[str, Any]]:
    if by not in {"harness", "model"}:
        raise ValueError("by must be harness or model")
    sessions = _aggregate_sessions(conn, tr)
    metrics = _metric_rows(conn, sessions)
    by_day: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    dims: set[str] = set()
    for session in sessions:
        started_at = session.row["started_at"]
        if not started_at:
            continue
        day = str(started_at)[:10]
        dim = (
            session.logical_harness
            if by == "harness"
            else _model_label(metrics.get(session.metric_session_id))
        )
        by_day[day][dim] += 1
        dims.add(dim)
    # Cap model series to top 8 + other for chart readability.
    if by == "model" and len(dims) > 8:
        totals = defaultdict(int)
        for day_map in by_day.values():
            for d, n in day_map.items():
                totals[d] += n
        keep = {d for d, _ in sorted(totals.items(), key=lambda x: -x[1])[:8]}
        dims = keep | {"(other)"}
        rebuilt: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for day, day_map in by_day.items():
            for d, n in day_map.items():
                key = d if d in keep else "(other)"
                rebuilt[day][key] += n
        by_day = rebuilt
    out: list[dict[str, Any]] = []
    for day in sorted(by_day):
        item: dict[str, Any] = {"day": day, "total": sum(by_day[day].values())}
        for d in sorted(dims):
            item[d] = by_day[day].get(d, 0)
        out.append(item)
    return out


def tool_usage(
    conn: sqlite3.Connection, tr: TimeRange, *, limit: int = 40
) -> dict[str, Any]:
    sessions = _aggregate_sessions(conn, tr)
    by_metric = {session.metric_session_id: session for session in sessions}
    if not by_metric:
        return {
            "total": 0,
            "distinct_tools": 0,
            "items": [],
            "note": "Tool call frequencies from canonical logical sessions. Descriptive only.",
        }
    metric_ids = sorted(by_metric)
    placeholders = ",".join("?" for _ in metric_ids)
    rows = conn.execute(
        f"""
        SELECT t.tool_name, t.session_id
        FROM tool_events t
        WHERE t.session_id IN ({placeholders})
        """,
        metric_ids,
    ).fetchall()
    by_tool: dict[str, dict[str, Any]] = {}
    for r in rows:
        name = r["tool_name"]
        session = by_metric[str(r["session_id"])]
        entry = by_tool.setdefault(
            name, {"tool": name, "count": 0, "by_harness": defaultdict(int)}
        )
        entry["count"] += 1
        entry["by_harness"][session.logical_harness] += 1
    ranked = sorted(by_tool.values(), key=lambda x: -x["count"])[:limit]
    total = len(rows)
    items = [
        {
            "tool": e["tool"],
            "count": e["count"],
            "share": e["count"] / total if total else 0.0,
            "by_harness": dict(e["by_harness"]),
        }
        for e in ranked
    ]
    return {
        "total": total,
        "distinct_tools": len(by_tool),
        "items": items,
        "note": "Tool call frequencies from canonical logical sessions. Descriptive only.",
    }


def request_kind_distribution(
    conn: sqlite3.Connection, tr: TimeRange
) -> dict[str, Any]:
    sessions = _aggregate_sessions(conn, tr)
    metric_ids = sorted({session.metric_session_id for session in sessions})
    if not metric_ids:
        return {
            "total": 0,
            "items": [],
            "orchestration_signals": {},
            "note": "Deterministic request-kind classifications over canonical logical sessions. Counts of observed traffic — not quality scores.",
        }
    placeholders = ",".join("?" for _ in metric_ids)
    rows = conn.execute(
        f"""
        SELECT d.request_kind, COUNT(*) AS c
        FROM window_det_classifications d
        JOIN exchange_windows w ON w.id = d.window_id
        WHERE w.session_id IN ({placeholders})
        GROUP BY d.request_kind
        ORDER BY c DESC
        """,
        metric_ids,
    ).fetchall()
    total = sum(int(r["c"]) for r in rows) or 1
    items = [
        {
            "request_kind": r["request_kind"],
            "count": int(r["c"]),
            "share": int(r["c"]) / total,
        }
        for r in rows
    ]
    orchestration = {
        k: next((i["count"] for i in items if i["request_kind"] == k), 0)
        for k in (
            "auto_review",
            "worker_brief",
            "inter_agent_handoff",
            "task_notification",
            "substantive",
            "cursor_wrapped",
        )
    }
    return {
        "total": sum(i["count"] for i in items),
        "items": items,
        "orchestration_signals": orchestration,
        "note": (
            "Deterministic request-kind classifications over canonical logical sessions. "
            "Counts of observed traffic — not quality scores."
        ),
    }


def model_monthly_mix(conn: sqlite3.Connection, tr: TimeRange) -> dict[str, Any]:
    sessions = _aggregate_sessions(conn, tr)
    metrics = _metric_rows(conn, sessions)
    months: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for session in sessions:
        started_at = session.row["started_at"]
        if not started_at:
            continue
        months[str(started_at)[:7]].append(
            {
                "model": _model_label(metrics.get(session.metric_session_id)),
                "harness": session.logical_harness,
                "sessions": 1,
            }
        )
    series = []
    for month in sorted(months):
        items = collapse_by_model(months[month])
        observed = sum(i["sessions"] for i in items)
        total = observed or 1
        series.append(
            {
                "month": month,
                "total": observed,
                "items": [
                    {**i, "share": i["sessions"] / total} for i in items
                ],
            }
        )
    return {
        "series": series,
        "note": "Monthly model-selection mix. Descriptive usage — not a ranking.",
    }


def duration_and_volume(
    conn: sqlite3.Connection, tr: TimeRange
) -> dict[str, Any]:
    sessions = _aggregate_sessions(conn, tr)
    metric_ids = sorted({session.metric_session_id for session in sessions})
    if metric_ids:
        placeholders = ",".join("?" for _ in metric_ids)
        metric_rows = conn.execute(
            f"""
            SELECT
              s.id,
              {_duration_seconds_sql()} AS duration_seconds,
              (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id) AS message_count,
              (SELECT COUNT(*) FROM tool_events t WHERE t.session_id = s.id) AS tool_count
            FROM sessions s
            WHERE s.id IN ({placeholders})
            """,
            metric_ids,
        ).fetchall()
    else:
        metric_rows = []
    by_metric = {str(row["id"]): row for row in metric_rows}
    rows = [
        by_metric[session.metric_session_id]
        for session in sessions
        if session.metric_session_id in by_metric
    ]

    def _bucket_duration(seconds: int | None) -> str | None:
        if seconds is None or seconds < 0:
            return None
        if seconds < 60:
            return "<1m"
        if seconds < 300:
            return "1–5m"
        if seconds < 900:
            return "5–15m"
        if seconds < 3600:
            return "15–60m"
        if seconds < 7200:
            return "1–2h"
        return "2h+"

    def _bucket_msgs(n: int) -> str:
        if n <= 2:
            return "1–2"
        if n <= 10:
            return "3–10"
        if n <= 40:
            return "11–40"
        if n <= 100:
            return "41–100"
        return "100+"

    dur_order = ["<1m", "1–5m", "5–15m", "15–60m", "1–2h", "2h+"]
    msg_order = ["1–2", "3–10", "11–40", "41–100", "100+"]
    dur_counts: dict[str, int] = defaultdict(int)
    msg_counts: dict[str, int] = defaultdict(int)
    durations: list[int] = []
    for r in rows:
        d = r["duration_seconds"]
        if d is not None and d >= 0:
            durations.append(int(d))
            b = _bucket_duration(int(d))
            if b:
                dur_counts[b] += 1
        msg_counts[_bucket_msgs(int(r["message_count"]))] += 1

    def _pct(sorted_vals: list[int], p: float) -> int | None:
        if not sorted_vals:
            return None
        idx = min(len(sorted_vals) - 1, max(0, int(round((p / 100.0) * (len(sorted_vals) - 1)))))
        return sorted_vals[idx]

    durations.sort()
    return {
        "sessions": len(rows),
        "identity_grain": "logical_sessions",
        "with_duration": len(durations),
        "duration_seconds": {
            "p50": _pct(durations, 50),
            "p90": _pct(durations, 90),
            "p99": _pct(durations, 99),
            "max": durations[-1] if durations else None,
        },
        "duration_buckets": [
            {"bucket": b, "count": dur_counts.get(b, 0)} for b in dur_order
        ],
        "message_buckets": [
            {"bucket": b, "count": msg_counts.get(b, 0)} for b in msg_order
        ],
        "note": "Observed logical-session duration and message-volume distributions.",
    }


def session_facets(conn: sqlite3.Connection, tr: TimeRange) -> dict[str, Any]:
    sessions = _aggregate_sessions(conn, tr)
    metrics = _metric_rows(conn, sessions)
    buckets: dict[str, dict[str, int]] = {
        "harness": defaultdict(int),
        "model": defaultdict(int),
        "effort": defaultdict(int),
        "branch": defaultdict(int),
        "project": defaultdict(int),
    }
    for session in sessions:
        row = session.row
        buckets["harness"][session.logical_harness] += 1
        buckets["model"][_model_label(metrics.get(session.metric_session_id))] += 1
        buckets["effort"][str(row["effort"] or "(none)")] += 1
        buckets["branch"][str(row["branch"] or "(none)")] += 1
        buckets["project"][_project_label(row["repo"], row["cwd"])] += 1

    def items(key: str) -> list[dict[str, Any]]:
        return [
            {"value": value, "count": count}
            for value, count in sorted(
                buckets[key].items(), key=lambda item: (-item[1], item[0])
            )[:40]
        ]

    return {
        "harness": items("harness"),
        "model": items("model"),
        "effort": items("effort"),
        "branch": items("branch"),
        "project": items("project"),
    }


def list_sessions_v2(
    conn: sqlite3.Connection,
    tr: TimeRange,
    *,
    harness: list[str] | None = None,
    model: list[str] | None = None,
    effort: list[str] | None = None,
    branch: list[str] | None = None,
    project: list[str] | None = None,
    q: str | None = None,
    sort: str = "started_at",
    order: str = "desc",
    cursor: int = 0,
    limit: int = 50,
) -> dict[str, Any]:
    where, params = _session_time_clause(tr)
    clauses = [where]
    if branch:
        ph = ",".join(f":b{i}" for i in range(len(branch)))
        clauses.append(f"COALESCE(NULLIF(s.branch, ''), '(none)') IN ({ph})")
        for i, b in enumerate(branch):
            params[f"b{i}"] = b
    if q:
        clauses.append(
            """(
            s.id LIKE :q
            OR COALESCE(s.repo, '') LIKE :q
            OR COALESCE(s.cwd, '') LIKE :q
            OR COALESCE(s.model, '') LIKE :q
            OR COALESCE(s.model_canonical, '') LIKE :q
            OR COALESCE(s.branch, '') LIKE :q
        )"""
        )
        params["q"] = f"%{q}%"
    where_sql = " AND ".join(clauses)
    sort_key = sort if sort in _SORT_COLUMNS else "started_at"
    direction = "ASC" if order.lower() == "asc" else "DESC"
    needs_counts_for_sort = sort_key in {"messages", "tools", "windows"}

    # Materialize project labels in Python for filter correctness + consistency
    # with _project_label (SQL path-basename is brittle for URLs).
    rows = conn.execute(
        f"""
        SELECT
            s.id, s.harness, s.model_canonical, s.model AS model_raw,
            s.effort, s.repo, s.cwd, s.branch,
            s.started_at, s.ended_at, s.parent_session_id,
            {_duration_seconds_sql()} AS duration_seconds
        FROM sessions s
        WHERE {where_sql}
        """,
        params,
    ).fetchall()

    identity = build_identity_context(conn)
    candidates: list[tuple[sqlite3.Row, dict[str, Any], str]] = []
    shadow_ids = provider_root_shadow_ids(conn, context=identity)
    for r in rows:
        if r["id"] in shadow_ids:
            continue
        projection = logical_projection(
            conn, str(r["id"]), str(r["harness"]), context=identity
        )
        if harness and projection["logical_harness"] not in harness:
            continue
        label = _project_label(r["repo"], r["cwd"])
        if project and label not in project:
            continue
        candidates.append((r, projection, label))

    metric_ids = sorted(
        {
            str(projection["transcript_session_id"] or row["id"])
            for row, projection, _ in candidates
        }
    )
    metric_rows: dict[str, sqlite3.Row] = {}
    if metric_ids:
        metric_ph = ",".join("?" for _ in metric_ids)
        metric_rows = {
            str(metric["id"]): metric
            for metric in conn.execute(
                f"""
                SELECT id, model, model_canonical, effort
                FROM sessions
                WHERE id IN ({metric_ph})
                """,
                metric_ids,
            ).fetchall()
        }

    metric_candidates: list[
        tuple[sqlite3.Row, dict[str, Any], str, str, sqlite3.Row | None]
    ] = []
    for row, projection, label in candidates:
        metric_id = str(projection["transcript_session_id"] or row["id"])
        metric = metric_rows.get(metric_id)
        metric_effort = metric["effort"] if metric is not None else row["effort"]
        effort_value = metric_effort if metric_effort not in (None, "") else "(none)"
        if effort and effort_value not in effort:
            continue
        metric_candidates.append((row, projection, label, metric_id, metric))

    matching_metric_ids: set[str] | None = None
    if model:
        metric_ids = sorted({candidate[3] for candidate in metric_candidates})
        matching_metric_ids = set()
        if metric_ids:
            metric_ph = ",".join("?" for _ in metric_ids)
            model_ph = ",".join("?" for _ in model)
            matching_metric_ids = {
                str(row["session_id"])
                for row in conn.execute(
                    f"""
                    SELECT DISTINCT model_message.session_id
                    FROM messages model_message
                    JOIN sessions model_session
                      ON model_session.id = model_message.session_id
                    WHERE model_message.session_id IN ({metric_ph})
                      AND model_message.role = 'assistant'
                      AND {strict_message_model_sql(message_alias='model_message', session_alias='model_session')} IN ({model_ph})
                    """,
                    [*metric_ids, *model],
                ).fetchall()
            }

    items: list[dict[str, Any]] = []
    for r, projection, label, metric_id, metric in metric_candidates:
        if matching_metric_ids is not None and metric_id not in matching_metric_ids:
            continue
        dur = r["duration_seconds"]
        items.append(
            {
                "id": r["id"],
                "harness": r["harness"],
                **projection,
                "model": display_model(
                    metric["model_canonical"] if metric is not None else r["model_canonical"]
                ),
                "effort": metric["effort"] if metric is not None else r["effort"],
                "project": label,
                "repo": r["repo"],
                "branch": r["branch"],
                "started_at": r["started_at"],
                "ended_at": r["ended_at"],
                "duration_seconds": int(dur) if dur is not None and dur >= 0 else None,
                "message_count": 0,
                "tool_count": 0,
                "window_count": 0,
                "parent_session_id": r["parent_session_id"],
                "status": "observed",
            }
        )

    reverse = direction == "DESC"

    def sort_value(item: dict[str, Any]) -> Any:
        if sort_key == "started_at":
            return item["started_at"] or ""
        if sort_key == "duration":
            return item["duration_seconds"] if item["duration_seconds"] is not None else -1
        if sort_key == "messages":
            return item["message_count"]
        if sort_key == "tools":
            return item["tool_count"]
        if sort_key == "windows":
            return item["window_count"]
        if sort_key == "model":
            return item["model"]
        if sort_key == "harness":
            return item["logical_harness"]
        if sort_key == "effort":
            return item["effort"] or ""
        if sort_key == "project":
            return item["project"]
        if sort_key == "branch":
            return item["branch"] or ""
        return item["started_at"] or ""

    def _attach_counts(target: list[dict[str, Any]]) -> None:
        if not target:
            return
        ids = [it["transcript_session_id"] or it["id"] for it in target]
        placeholders = ",".join("?" * len(ids))
        msg = {
            r["session_id"]: int(r["c"])
            for r in conn.execute(
                f"SELECT session_id, COUNT(*) AS c FROM messages "
                f"WHERE session_id IN ({placeholders}) GROUP BY session_id",
                ids,
            ).fetchall()
        }
        tools = {
            r["session_id"]: int(r["c"])
            for r in conn.execute(
                f"SELECT session_id, COUNT(*) AS c FROM tool_events "
                f"WHERE session_id IN ({placeholders}) GROUP BY session_id",
                ids,
            ).fetchall()
        }
        windows = {
            r["session_id"]: int(r["c"])
            for r in conn.execute(
                f"SELECT session_id, COUNT(*) AS c FROM exchange_windows "
                f"WHERE session_id IN ({placeholders}) GROUP BY session_id",
                ids,
            ).fetchall()
        }
        for it in target:
            sid = it["transcript_session_id"] or it["id"]
            it["message_count"] = msg.get(sid, 0)
            it["tool_count"] = tools.get(sid, 0)
            it["window_count"] = windows.get(sid, 0)

    if needs_counts_for_sort:
        _attach_counts(items)

    items.sort(key=lambda item: str(item["id"]))
    items.sort(key=sort_value, reverse=reverse)
    total = len(items)
    offset = max(0, cursor)
    page = items[offset : offset + limit]
    if not needs_counts_for_sort:
        _attach_counts(page)
    return {
        "total": total,
        "cursor": offset,
        "next_cursor": offset + limit if offset + limit < total else None,
        "sort": sort_key,
        "order": order.lower() if order.lower() in {"asc", "desc"} else "desc",
        "items": page,
    }


def _with_display_model(row: sqlite3.Row) -> dict[str, Any]:
    """Row dict where ``model`` is the canonical identity, raw kept aside."""
    item = dict(row)
    raw = item.get("model")
    canonical = item.pop("model_canonical", None)
    item["model_raw"] = raw
    item["model"] = display_model(canonical) if (raw or canonical) else None
    return item


def session_detail_v2(
    conn: sqlite3.Connection, session_id: str
) -> dict[str, Any] | None:
    resolved = _resolve_session(conn, session_id)
    if resolved is None:
        return None
    # Aliases are a lookup-boundary concern only; every dependent query below
    # binds the canonical id or the row is internally inconsistent.
    resolved_id = str(resolved["id"])
    s = conn.execute(
        """
        SELECT s.*, a.path AS artifact_path
        FROM sessions s
        LEFT JOIN artifacts a ON a.id = s.artifact_id
        WHERE s.id = ?
        """,
        (resolved_id,),
    ).fetchone()
    if s is None:
        return None
    identity = build_identity_context(conn)
    projection = logical_projection(
        conn, resolved_id, str(s["harness"]), context=identity
    )
    transcript_id = projection["transcript_session_id"] or resolved_id
    transcript = s
    if transcript_id != resolved_id:
        transcript = conn.execute(
            """
            SELECT s.*, a.path AS artifact_path
            FROM sessions s
            LEFT JOIN artifacts a ON a.id = s.artifact_id
            WHERE s.id = ?
            """,
            (transcript_id,),
        ).fetchone()
        if transcript is None:
            transcript = s
            transcript_id = resolved_id

    messages = conn.execute(
        """
        SELECT id, seq, role, timestamp, model, model_canonical, effort, text,
               is_tool_plumbing, authored_by_agent
        FROM messages WHERE session_id = ? ORDER BY seq
        """,
        (transcript_id,),
    ).fetchall()
    tools = conn.execute(
        """
        SELECT id, message_id, seq, tool_name, action, success, duration_ms,
               operation_kind
        FROM tool_events WHERE session_id = ? ORDER BY seq
        """,
        (transcript_id,),
    ).fetchall()
    skills = conn.execute(
        """
        SELECT skill_name, exposure_type, COUNT(*) AS c
        FROM skill_exposures WHERE session_id = ?
        GROUP BY skill_name, exposure_type
        ORDER BY c DESC
        """,
        (transcript_id,),
    ).fetchall()
    skill_msgs = conn.execute(
        """
        SELECT message_id, skill_name FROM skill_exposures
        WHERE session_id = ? AND message_id IS NOT NULL
        """,
        (transcript_id,),
    ).fetchall()
    kinds = conn.execute(
        """
        SELECT w.request_message_id AS message_id, d.request_kind
        FROM exchange_windows w
        JOIN window_det_classifications d ON d.window_id = w.id
        WHERE w.session_id = ?
        """,
        (transcript_id,),
    ).fetchall()
    windows = conn.execute(
        """
        SELECT COUNT(*) AS c FROM exchange_windows WHERE session_id = ?
        """,
        (transcript_id,),
    ).fetchone()
    children = conn.execute(
        """
        SELECT id, harness, model, model_canonical, effort, started_at, ended_at,
               (SELECT COUNT(*) FROM messages m WHERE m.session_id = sessions.id) AS message_count
        FROM sessions
        WHERE parent_session_id IN (?, ?, ?)
        ORDER BY COALESCE(started_at, '') ASC
        """,
        (
            transcript_id,
            transcript["external_id"],
            f"{transcript['harness']}:{transcript['external_id']}",
        ),
    ).fetchall()

    tools_by_msg: dict[str, list[dict[str, Any]]] = defaultdict(list)
    orphan_tools: list[dict[str, Any]] = []
    for t in tools:
        item = dict(t)
        mid = t["message_id"]
        if mid:
            tools_by_msg[mid].append(item)
        else:
            orphan_tools.append(item)

    kind_by_msg = {r["message_id"]: r["request_kind"] for r in kinds}
    skills_by_msg: dict[str, list[str]] = defaultdict(list)
    for r in skill_msgs:
        skills_by_msg[r["message_id"]].append(r["skill_name"])

    timeline: list[dict[str, Any]] = []
    for m in messages:
        timeline.append(
            {
                "kind": "message",
                "id": m["id"],
                "seq": m["seq"],
                "role": m["role"],
                "timestamp": m["timestamp"],
                "model": (
                    display_model(m["model_canonical"])
                    if (m["model"] or m["model_canonical"])
                    else None
                ),
                "model_raw": m["model"],
                "effort": m["effort"],
                "text": m["text"],
                "is_tool_plumbing": bool(m["is_tool_plumbing"]),
                "authored_by_agent": bool(m["authored_by_agent"]),
                "request_kind": kind_by_msg.get(m["id"]),
                "skills": skills_by_msg.get(m["id"], []),
                "tool_events": tools_by_msg.get(m["id"], []),
            }
        )
    for t in orphan_tools:
        timeline.append(
            {
                "kind": "tool",
                "id": t["id"],
                "seq": t["seq"],
                "tool_name": t["tool_name"],
                "action": t["action"],
                "success": t["success"],
                "duration_ms": t["duration_ms"],
                "message_id": None,
            }
        )
    timeline.sort(key=lambda x: (x.get("seq") is None, x.get("seq") or 0))

    dur = None
    if s["started_at"] and s["ended_at"]:
        try:
            a = datetime.fromisoformat(s["started_at"].replace("Z", "+00:00"))
            b = datetime.fromisoformat(s["ended_at"].replace("Z", "+00:00"))
            dur = max(0, int((b - a).total_seconds()))
        except ValueError:
            dur = None

    return {
        "session": {
            "id": s["id"],
            "harness": s["harness"],
            **projection,
            "model": display_model(s["model_canonical"]),
            "model_raw": s["model"],
            "effort": s["effort"],
            "project": _project_label(s["repo"], s["cwd"]),
            "repo": s["repo"],
            "cwd": s["cwd"],
            "branch": s["branch"],
            "commit_sha": s["commit_sha"],
            "started_at": s["started_at"],
            "ended_at": s["ended_at"],
            "duration_seconds": dur,
            "parent_session_id": s["parent_session_id"],
            "artifact_id": s["artifact_id"],
            "artifact_path": s["artifact_path"],
            "external_id": s["external_id"],
        },
        "transcript": {
            "id": transcript["id"],
            "harness": transcript["harness"],
            "artifact_id": transcript["artifact_id"],
            "artifact_path": transcript["artifact_path"],
        },
        "timeline": timeline,
        "messages": [_with_display_model(m) for m in messages],
        "tool_events": [dict(t) for t in tools],
        "skills": [dict(sk) for sk in skills],
        "children": [
            {
                **_with_display_model(c),
                **logical_projection(
                    conn, str(c["id"]), str(c["harness"]), context=identity
                ),
            }
            for c in children
        ],
        "anatomy": {
            "message_count": len(messages),
            "tool_count": len(tools),
            "window_count": int(windows["c"]) if windows else 0,
            "child_count": len(children),
            "tokens": None,
            "cost_est": None,
        },
    }


def search_messages(
    conn: sqlite3.Connection,
    tr: TimeRange,
    *,
    q: str,
    harness: list[str] | None = None,
    model: list[str] | None = None,
    project: list[str] | None = None,
    cursor: int = 0,
    limit: int = 40,
) -> dict[str, Any]:
    match = _fts_match_query(q)
    if match is None:
        return {
            "q": q,
            "total": 0,
            "cursor": 0,
            "next_cursor": None,
            "items": [],
            "note": "Enter a search term to query messages_fts.",
        }
    sessions = _aggregate_sessions(conn, tr)
    by_metric = {session.metric_session_id: session for session in sessions}
    if not by_metric:
        return {
            "q": q,
            "match": match,
            "total": 0,
            "cursor": max(0, cursor),
            "next_cursor": None,
            "items": [],
            "note": "Full-text search over canonical logical transcripts.",
            "truncated": False,
        }
    params: dict[str, Any] = {"match": match}
    metric_ph = ",".join(f":metric{i}" for i in range(len(by_metric)))
    for i, session_id in enumerate(sorted(by_metric)):
        params[f"metric{i}"] = session_id
    clauses = ["messages_fts MATCH :match", f"m.session_id IN ({metric_ph})"]
    where_sql = " AND ".join(clauses)
    metrics = _metric_rows(conn, sessions)
    # Fetch a bounded candidate set then project-filter in Python.
    fetch_n = min(2000, max(limit * 5, cursor + limit + 200))
    rows = conn.execute(
        f"""
        SELECT
            m.id AS message_id,
            m.session_id,
            m.seq,
            m.role,
            m.timestamp,
            m.model_canonical AS message_model,
            snippet(messages_fts, 0, '«', '»', '…', 18) AS snippet,
            bm25(messages_fts) AS rank,
            s.harness,
            s.model_canonical AS session_model,
            s.effort,
            s.repo,
            s.cwd,
            s.started_at
        FROM messages_fts
        JOIN messages m ON m.rowid = messages_fts.rowid
        JOIN sessions s ON s.id = m.session_id
        WHERE {where_sql}
        ORDER BY rank
        LIMIT :fetch_n
        """,
        {**params, "fetch_n": fetch_n},
    ).fetchall()

    items: list[dict[str, Any]] = []
    for r in rows:
        session = by_metric.get(str(r["session_id"]))
        if session is None:
            continue
        if harness and session.logical_harness not in harness:
            continue
        source_model = _model_label(metrics.get(session.metric_session_id))
        if model and source_model not in model:
            continue
        label = _project_label(session.row["repo"], session.row["cwd"])
        if project and label not in project:
            continue
        items.append(
            {
                "message_id": r["message_id"],
                "session_id": session.session_id,
                "physical_session_id": r["session_id"],
                "seq": r["seq"],
                "role": r["role"],
                "timestamp": r["timestamp"],
                "snippet": r["snippet"],
                "harness": session.logical_harness,
                "runtime_harness": session.runtime_harness,
                "orchestrator_session_id": session.orchestrator_session_id,
                "transcript_session_id": session.metric_session_id,
                "model": display_model(source_model),
                "effort": session.row["effort"],
                "project": label,
                "started_at": session.row["started_at"],
            }
        )
    total = len(items)
    offset = max(0, cursor)
    page = items[offset : offset + limit]
    return {
        "q": q,
        "match": match,
        "total": total,
        "cursor": offset,
        "next_cursor": offset + limit if offset + limit < total else None,
        "items": page,
        "note": (
            "Full-text search over canonical logical transcripts. Snippets use « » "
            "around matches. Click through to the logical session at the matching message."
        ),
        "truncated": total >= fetch_n,
    }


def _parent_match_sql(parent_alias: str = "p", child_alias: str = "c") -> str:
    """parent_session_id is often a bare external_id, while sessions.id is harness:external_id."""
    return f"""(
        {child_alias}.parent_session_id = {parent_alias}.id
        OR {child_alias}.parent_session_id = {parent_alias}.external_id
        OR {child_alias}.parent_session_id = ({parent_alias}.harness || ':' || {parent_alias}.external_id)
    )"""


def _resolve_session(
    conn: sqlite3.Connection, session_id: str
) -> sqlite3.Row | None:
    row = conn.execute(
        "SELECT * FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if row is not None:
        return row
    return conn.execute(
        """
        SELECT * FROM sessions
        WHERE external_id = ?
           OR id LIKE '%' || ?
        ORDER BY CASE WHEN external_id = ? THEN 0 ELSE 1 END
        LIMIT 1
        """,
        (session_id, session_id, session_id),
    ).fetchone()


def orchestration_overview(
    conn: sqlite3.Connection, tr: TimeRange, *, limit: int = 40
) -> dict[str, Any]:
    where, params = _session_time_clause(tr, alias="p")
    root_edges = conn.execute(
        f"""
        SELECT
            p.id,
            p.harness,
            p.model AS model_raw,
            p.model_canonical,
            p.effort,
            p.repo,
            p.cwd,
            p.started_at,
            p.ended_at,
            c.id AS child_id
        FROM sessions p
        JOIN sessions c ON {_parent_match_sql("p", "c")}
        WHERE {where}
        """,
        params,
    ).fetchall()

    identity = build_identity_context(conn)
    kind_where, kind_params = _session_time_clause(tr)
    signal_rows = conn.execute(
        f"SELECT s.id, s.harness FROM sessions s WHERE {kind_where}",
        kind_params,
    ).fetchall()
    signal_metrics = sorted(
        {
            item.metric_session_id
            for item in visible_logical_sessions(
                conn, signal_rows, context=identity
            )
        }
    )
    signals: dict[str, int] = {}
    if signal_metrics:
        placeholders = ",".join("?" for _ in signal_metrics)
        kinds = conn.execute(
            f"""
            SELECT d.request_kind, COUNT(*) AS c
            FROM window_det_classifications d
            JOIN exchange_windows w ON w.id = d.window_id
            WHERE w.session_id IN ({placeholders})
              AND d.request_kind IN (
                'worker_brief', 'inter_agent_handoff', 'task_notification', 'auto_review'
              )
            GROUP BY d.request_kind
            """,
            signal_metrics,
        ).fetchall()
        signals = {r["request_kind"]: int(r["c"]) for r in kinds}
    root_shadow_ids = provider_root_shadow_ids(conn, context=identity)
    physical_roots: dict[str, sqlite3.Row] = {}
    children_by_root: dict[str, set[str]] = {}
    for edge in root_edges:
        physical_id = str(edge["id"])
        physical_roots.setdefault(physical_id, edge)
        children_by_root.setdefault(physical_id, set()).add(str(edge["child_id"]))

    link_children_by_source: dict[str, set[str]] = {}
    for source_id, backings in identity.backings_by_source.items():
        for backing in backings:
            target_id = backing["target_session_id"]
            if (
                backing.get("link_role") != "worker"
                or not target_id
                or identity.owners_by_session.get(str(target_id), set())
                != {source_id}
            ):
                continue
            link_children_by_source.setdefault(source_id, set()).add(str(target_id))
    if link_children_by_source:
        link_where, link_params = _session_time_clause(tr, alias="s")
        placeholders = ",".join(
            f":link_source_{index}"
            for index, _ in enumerate(link_children_by_source)
        )
        link_query_params = {
            **link_params,
            **{
                f"link_source_{index}": source_id
                for index, source_id in enumerate(sorted(link_children_by_source))
            },
        }
        link_rows = conn.execute(
            f"""
            SELECT s.id, s.harness, s.model AS model_raw, s.model_canonical,
                   s.effort, s.repo, s.cwd, s.started_at, s.ended_at
            FROM sessions s
            WHERE s.id IN ({placeholders}) AND {link_where}
            """,
            link_query_params,
        ).fetchall()
        for row in link_rows:
            source_id = str(row["id"])
            physical_roots.setdefault(source_id, row)
            children_by_root.setdefault(source_id, set()).update(
                link_children_by_source[source_id]
            )

    def presentation_id(row: sqlite3.Row) -> str:
        physical_id = str(row["id"])
        if physical_id not in root_shadow_ids:
            return physical_id
        return logical_root_session_id(conn, physical_id, context=identity)

    logical_children: dict[str, set[str]] = {}
    for physical_id, row in physical_roots.items():
        logical_id = presentation_id(row)
        logical_children.setdefault(logical_id, set()).update(
            children_by_root[physical_id]
        )

    presentation_rows = dict(physical_roots)
    missing_ids = set(logical_children).difference(presentation_rows)
    if missing_ids:
        placeholders = ",".join("?" for _ in missing_ids)
        rows = conn.execute(
            f"""
            SELECT s.id, s.harness, s.model AS model_raw, s.model_canonical,
                   s.effort, s.repo, s.cwd, s.started_at, s.ended_at
            FROM sessions s
            WHERE s.id IN ({placeholders})
            """,
            sorted(missing_ids),
        ).fetchall()
        presentation_rows.update({str(row["id"]): row for row in rows})

    projections: dict[str, dict[str, Any]] = {}
    message_session_ids: set[str] = set()
    for logical_id in logical_children:
        row = presentation_rows.get(logical_id)
        if row is None:
            continue
        projection = logical_projection(
            conn, logical_id, str(row["harness"]), context=identity
        )
        projections[logical_id] = projection
        message_session_ids.add(projection["transcript_session_id"] or logical_id)
    message_counts: dict[str, int] = {}
    metric_rows: dict[str, sqlite3.Row] = {}
    if message_session_ids:
        placeholders = ",".join("?" for _ in message_session_ids)
        message_counts = {
            str(row["session_id"]): int(row["c"])
            for row in conn.execute(
                f"""
                SELECT session_id, COUNT(*) AS c
                FROM messages
                WHERE session_id IN ({placeholders})
                GROUP BY session_id
                """,
                sorted(message_session_ids),
            ).fetchall()
        }
        metric_rows = {
            str(metric["id"]): metric
            for metric in conn.execute(
                f"""
                SELECT id, model, model_canonical, effort
                FROM sessions
                WHERE id IN ({placeholders})
                """,
                sorted(message_session_ids),
            ).fetchall()
        }

    items = []
    for logical_id, child_ids in logical_children.items():
        row = presentation_rows.get(logical_id)
        projection = projections.get(logical_id)
        if row is None or projection is None:
            continue
        transcript_id = projection["transcript_session_id"] or logical_id
        metric = metric_rows.get(transcript_id)
        metric_model = metric["model_canonical"] if metric is not None else row["model_canonical"]
        metric_model_raw = metric["model"] if metric is not None else row["model_raw"]
        metric_effort = metric["effort"] if metric is not None else row["effort"]
        items.append(
            {
                "id": logical_id,
                "harness": row["harness"],
                **projection,
                "model": display_model(metric_model),
                "model_raw": metric_model_raw,
                "effort": metric_effort,
                "project": _project_label(row["repo"], row["cwd"]),
                "started_at": row["started_at"],
                "ended_at": row["ended_at"],
                "child_count": len(child_ids),
                "message_count": message_counts.get(transcript_id, 0),
            }
        )
    items.sort(
        key=lambda item: (item["child_count"], item["started_at"] or ""),
        reverse=True,
    )
    root_total = len(items)
    return {
        "supervisor_roots": root_total,
        "child_sessions": sum(len(children) for children in logical_children.values()),
        "signals": {
            "worker_brief": signals.get("worker_brief", 0),
            "inter_agent_handoff": signals.get("inter_agent_handoff", 0),
            "task_notification": signals.get("task_notification", 0),
            "auto_review": signals.get("auto_review", 0),
        },
        "items": items[:limit],
        "note": (
            "Supervisor sessions with at least one child via parent_session_id "
            "or an observed worker link. "
            "Navigable tree is available per root."
        ),
    }


def orchestration_tree(
    conn: sqlite3.Connection, session_id: str
) -> dict[str, Any] | None:
    root = _resolve_session(conn, session_id)
    if root is None:
        return None
    identity = build_identity_context(conn)
    metric_rows: dict[str, sqlite3.Row | None] = {}

    def metric_row(session_id: str) -> sqlite3.Row | None:
        if session_id not in metric_rows:
            metric_rows[session_id] = conn.execute(
                """
                SELECT model, model_canonical, effort
                FROM sessions
                WHERE id = ?
                """,
                (session_id,),
            ).fetchone()
        return metric_rows[session_id]

    def children_of(row: sqlite3.Row) -> list[sqlite3.Row]:
        return conn.execute(
            """
            SELECT * FROM sessions
            WHERE parent_session_id IN (?, ?, ?)
            ORDER BY COALESCE(started_at, '') ASC
            """,
            (
                row["id"],
                row["external_id"],
                f"{row['harness']}:{row['external_id']}",
            ),
        ).fetchall()

    def node_ids(nodes: list[dict[str, Any]]) -> set[str]:
        seen: set[str] = set()
        pending = list(nodes)
        while pending:
            node = pending.pop()
            node_id = str(node["id"])
            if node_id in seen:
                continue
            seen.add(node_id)
            pending.extend(node["children"])
        return seen

    def node_for(row: sqlite3.Row, depth: int = 0) -> dict[str, Any]:
        # Guard pathological cycles / extreme fan-out depth.
        if depth > 12:
            kids: list[sqlite3.Row] = []
        else:
            kids = children_of(row)
        relationships: dict[str, str] = {}
        provider_links: list[dict[str, Any]] = []
        root_target_ids: set[str] = set()
        if row["harness"] == "t3code":
            known = {str(k["id"]) for k in kids}
            provider_links = provider_backings(
                conn, str(row["id"]), context=identity
            )
            for backing in provider_root_backings(
                conn, str(row["id"]), context=identity
            ):
                target_id = backing["target_session_id"]
                if target_id:
                    root_target_ids.add(str(target_id))
                if not target_id:
                    continue
                target = conn.execute(
                    "SELECT * FROM sessions WHERE id = ?", (target_id,)
                ).fetchone()
                if target is None:
                    continue
                for child in children_of(target):
                    child_id = str(child["id"])
                    if child_id in known:
                        continue
                    kids.append(child)
                    known.add(child_id)
        projection = logical_projection(
            conn, str(row["id"]), str(row["harness"]), context=identity
        )
        count_session_id = projection["transcript_session_id"] or str(row["id"])
        metric = metric_row(count_session_id) or row
        msg_n = conn.execute(
            "SELECT COUNT(*) AS c FROM messages WHERE session_id = ?",
            (count_session_id,),
        ).fetchone()
        tool_n = conn.execute(
            "SELECT COUNT(*) AS c FROM tool_events WHERE session_id = ?",
            (count_session_id,),
        ).fetchone()
        node = {
            "id": row["id"],
            "harness": row["harness"],
            **projection,
            "model": display_model(metric["model_canonical"]),
            "model_raw": metric["model"],
            "effort": metric["effort"],
            "project": _project_label(row["repo"], row["cwd"]),
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
            "message_count": int(msg_n["c"]) if msg_n else 0,
            "tool_count": int(tool_n["c"]) if tool_n else 0,
            "children": [node_for(k, depth + 1) for k in kids],
        }
        if row["harness"] == "t3code":
            reachable = node_ids(node["children"])
            for backing in provider_links:
                target_id = backing["target_session_id"]
                if not target_id or str(target_id) in root_target_ids:
                    continue
                if str(target_id) in reachable:
                    continue
                target = conn.execute(
                    "SELECT * FROM sessions WHERE id = ?", (target_id,)
                ).fetchone()
                if target is None:
                    continue
                child = node_for(target, depth + 1)
                node["children"].append(child)
                relationships[str(target_id)] = "provider_worker"
                reachable.update(node_ids([child]))
        for child in node["children"]:
            relation = relationships.get(str(child["id"]))
            if relation:
                child["relationship"] = relation
        return node

    # If caller passed a child, walk up to the true root for a full tree.
    walk = root
    seen: set[str] = set()
    while walk["parent_session_id"] and walk["id"] not in seen:
        seen.add(walk["id"])
        parent = _resolve_session(conn, walk["parent_session_id"])
        if parent is None:
            break
        walk = parent

    owner_id = logical_orchestrator_id(conn, str(walk["id"]), context=identity)
    if owner_id:
        owner = conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (owner_id,)
        ).fetchone()
        if owner is not None:
            walk = owner

    tree = node_for(walk)
    return {
        "root_id": walk["id"],
        "requested_id": session_id,
        "tree": tree,
        "note": (
            "Supervisor → worker tree from parent_session_id links "
            "(matches id or bare external_id)."
        ),
    }


def auto_review_surface(
    conn: sqlite3.Connection, tr: TimeRange, *, limit: int = 50
) -> dict[str, Any]:
    where, params = _session_time_clause(tr)
    observations = conn.execute(
        f"""
        SELECT
            a.id,
            a.window_id,
            a.created_at,
            a.payload_json,
            s.id AS session_id,
            s.harness
        FROM auto_review_observations a
        JOIN exchange_windows w ON w.id = a.window_id
        JOIN sessions s ON s.id = w.session_id
        WHERE {where}
        """,
        params,
    ).fetchall()

    identity = build_identity_context(conn)
    session_rows = conn.execute(
        f"SELECT s.id, s.harness FROM sessions s WHERE {where}", params
    ).fetchall()
    visible = visible_logical_sessions(conn, session_rows, context=identity)
    canonical_metrics = {
        item.metric_session_id: item for item in visible
    }
    canonical: list[tuple[sqlite3.Row, VisibleLogicalSession]] = []
    for observation in observations:
        physical_session_id = str(observation["session_id"])
        logical = canonical_metrics.get(physical_session_id)
        if logical is None:
            continue
        canonical.append((observation, logical))

    metric_ids = sorted({item.metric_session_id for _, item in canonical})
    display_ids = sorted({item.session_id for _, item in canonical})

    def session_rows(ids: list[str]) -> dict[str, sqlite3.Row]:
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        rows = conn.execute(
            f"""
            SELECT id, model_canonical, effort, repo, cwd, started_at
            FROM sessions WHERE id IN ({placeholders})
            """,
            ids,
        ).fetchall()
        return {str(row["id"]): row for row in rows}

    metric_rows = session_rows(metric_ids)
    display_rows = session_rows(display_ids)
    model_counts: dict[tuple[str, str], int] = defaultdict(int)
    day_counts: dict[str, int] = defaultdict(int)
    normalized: list[dict[str, Any]] = []
    for observation, logical in canonical:
        metric = metric_rows.get(logical.metric_session_id)
        display = display_rows.get(logical.session_id) or metric
        if metric is None or display is None:
            continue
        model = display_model(metric["model_canonical"])
        harness = logical.logical_harness
        model_counts[(model, harness)] += 1
        day = str(display["started_at"] or observation["created_at"] or "")[:10]
        if day:
            day_counts[day] += 1
        normalized.append(
            {
                "observation": observation,
                "logical": logical,
                "session_id": logical.session_id,
                "transcript_session_id": logical.metric_session_id,
                "metric": metric,
                "display": display,
                "model": model,
                "harness": harness,
            }
        )
    normalized.sort(
        key=lambda item: str(
            item["observation"]["created_at"]
            or item["display"]["started_at"]
            or ""
        ),
        reverse=True,
    )

    import json

    items = []
    for entry in normalized[:limit]:
        r = entry["observation"]
        logical = entry["logical"]
        metric = entry["metric"]
        display = entry["display"]
        try:
            payload = json.loads(r["payload_json"] or "{}")
        except json.JSONDecodeError:
            payload = {}
        items.append(
            {
                "id": r["id"],
                "window_id": r["window_id"],
                "session_id": entry["session_id"],
                "physical_session_id": r["session_id"],
                "transcript_session_id": entry["transcript_session_id"],
                "harness": entry["harness"],
                "logical_harness": entry["harness"],
                "runtime_harness": logical.runtime_harness,
                "model": entry["model"],
                "effort": metric["effort"],
                "project": _project_label(display["repo"], display["cwd"]),
                "started_at": display["started_at"],
                "created_at": r["created_at"],
                "status": payload.get("status"),
                "route": payload.get("route"),
                "request_kind": payload.get("request_kind", "auto_review"),
            }
        )
    return {
        "total": len(normalized),
        "by_model": collapse_by_model(
            [
                {"model": model, "harness": harness, "count": count}
                for (model, harness), count in model_counts.items()
            ],
            count_key="count",
        )[:30],
        "by_day": [
            {"day": day, "count": count}
            for day, count in sorted(day_counts.items())
        ],
        "items": items,
        "note": (
            "Auto-review traffic is excluded from UX interaction-style metrics, "
            "but is listed here as logical observed volume."
        ),
    }
