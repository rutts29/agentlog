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

from agentlog.api.model_rollup import collapse_by_model, strict_message_model_sql
from agentlog.api.ranges import TimeRange, session_time_clause as _session_time_clause
from agentlog.normalize.model_identity import display_model, sql_coalesce_model

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


def ledger_counts(conn: sqlite3.Connection, tr: TimeRange) -> dict[str, Any]:
    where, params = _session_time_clause(tr)
    row = conn.execute(
        f"""
        SELECT
          (SELECT COUNT(*) FROM sessions s WHERE {where}) AS sessions,
          (SELECT COUNT(*) FROM messages m
             JOIN sessions s ON s.id = m.session_id WHERE {where}) AS messages,
          (SELECT COUNT(*) FROM tool_events t
             JOIN sessions s ON s.id = t.session_id WHERE {where}) AS tool_events,
          (SELECT COUNT(*) FROM exchange_windows w
             JOIN sessions s ON s.id = w.session_id WHERE {where}) AS windows,
          (SELECT COUNT(*) FROM skill_exposures se
             JOIN sessions s ON s.id = se.session_id WHERE {where}) AS skill_exposures,
          (SELECT COUNT(*) FROM auto_review_observations a
             JOIN exchange_windows w ON w.id = a.window_id
             JOIN sessions s ON s.id = w.session_id WHERE {where}) AS auto_reviews,
          (SELECT COUNT(*) FROM worker_task_observations wt
             JOIN exchange_windows w ON w.id = wt.window_id
             JOIN sessions s ON s.id = w.session_id WHERE {where}) AS worker_briefs,
          (SELECT COUNT(*) FROM sessions s
             WHERE {where} AND s.parent_session_id IS NOT NULL) AS child_sessions
        """,
        params,
    ).fetchone()
    return {k: int(row[k] or 0) for k in row.keys()}


def sessions_daily_by(
    conn: sqlite3.Connection, tr: TimeRange, *, by: str
) -> list[dict[str, Any]]:
    if by not in {"harness", "model"}:
        raise ValueError("by must be harness or model")
    where, params = _session_time_clause(tr)
    dim = (
        "s.harness"
        if by == "harness"
        else sql_coalesce_model("s.model_canonical")
    )
    rows = conn.execute(
        f"""
        SELECT substr(s.started_at, 1, 10) AS day, {dim} AS dim, COUNT(*) AS sessions
        FROM sessions s
        WHERE s.started_at IS NOT NULL AND {where}
        GROUP BY day, dim
        ORDER BY day, dim
        """,
        params,
    ).fetchall()
    by_day: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    dims: set[str] = set()
    for r in rows:
        by_day[r["day"]][r["dim"]] = int(r["sessions"])
        dims.add(r["dim"])
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
    where, params = _session_time_clause(tr)
    rows = conn.execute(
        f"""
        SELECT t.tool_name, s.harness, COUNT(*) AS c
        FROM tool_events t
        JOIN sessions s ON s.id = t.session_id
        WHERE {where}
        GROUP BY t.tool_name, s.harness
        ORDER BY c DESC
        """,
        params,
    ).fetchall()
    by_tool: dict[str, dict[str, Any]] = {}
    for r in rows:
        name = r["tool_name"]
        entry = by_tool.setdefault(
            name, {"tool": name, "count": 0, "by_harness": defaultdict(int)}
        )
        entry["count"] += int(r["c"])
        entry["by_harness"][r["harness"]] += int(r["c"])
    ranked = sorted(by_tool.values(), key=lambda x: -x["count"])[:limit]
    total = sum(int(r["c"]) for r in rows)
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
        "note": "Tool call frequencies from observed tool_events. Descriptive only.",
    }


def request_kind_distribution(
    conn: sqlite3.Connection, tr: TimeRange
) -> dict[str, Any]:
    where, params = _session_time_clause(tr)
    rows = conn.execute(
        f"""
        SELECT d.request_kind, COUNT(*) AS c
        FROM window_det_classifications d
        JOIN exchange_windows w ON w.id = d.window_id
        JOIN sessions s ON s.id = w.session_id
        WHERE {where}
        GROUP BY d.request_kind
        ORDER BY c DESC
        """,
        params,
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
            "Deterministic request-kind classifications over exchange windows. "
            "Counts of observed traffic — not quality scores."
        ),
    }


def model_monthly_mix(conn: sqlite3.Connection, tr: TimeRange) -> dict[str, Any]:
    where, params = _session_time_clause(tr)
    rows = conn.execute(
        f"""
        SELECT substr(s.started_at, 1, 7) AS month,
               {sql_coalesce_model('s.model_canonical')} AS model,
               s.harness,
               COUNT(*) AS sessions
        FROM sessions s
        WHERE s.started_at IS NOT NULL AND {where}
        GROUP BY month, {sql_coalesce_model('s.model_canonical')}, harness
        ORDER BY month, sessions DESC
        """,
        params,
    ).fetchall()
    months: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        months[r["month"]].append(dict(r))
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
    where, params = _session_time_clause(tr)
    rows = conn.execute(
        f"""
        SELECT
          s.id,
          {_duration_seconds_sql()} AS duration_seconds,
          (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id) AS message_count,
          (SELECT COUNT(*) FROM tool_events t WHERE t.session_id = s.id) AS tool_count
        FROM sessions s
        WHERE {where}
        """,
        params,
    ).fetchall()

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
        "note": "Observed session duration and message-volume distributions.",
    }


def session_facets(conn: sqlite3.Connection, tr: TimeRange) -> dict[str, Any]:
    where, params = _session_time_clause(tr)
    harness = conn.execute(
        f"""
        SELECT s.harness AS value, COUNT(*) AS c
        FROM sessions s WHERE {where}
        GROUP BY s.harness ORDER BY c DESC
        """,
        params,
    ).fetchall()
    model = conn.execute(
        f"""
        SELECT {sql_coalesce_model('s.model_canonical')} AS value, COUNT(*) AS c
        FROM sessions s WHERE {where}
        GROUP BY value ORDER BY c DESC LIMIT 40
        """,
        params,
    ).fetchall()
    effort = conn.execute(
        f"""
        SELECT COALESCE(NULLIF(s.effort, ''), '(none)') AS value, COUNT(*) AS c
        FROM sessions s WHERE {where}
        GROUP BY value ORDER BY c DESC
        """,
        params,
    ).fetchall()
    branch = conn.execute(
        f"""
        SELECT COALESCE(NULLIF(s.branch, ''), '(none)') AS value, COUNT(*) AS c
        FROM sessions s WHERE {where}
        GROUP BY value ORDER BY c DESC LIMIT 40
        """,
        params,
    ).fetchall()
    rows = conn.execute(
        f"SELECT s.repo, s.cwd FROM sessions s WHERE {where}",
        params,
    ).fetchall()
    projects: dict[str, int] = defaultdict(int)
    for r in rows:
        projects[_project_label(r["repo"], r["cwd"])] += 1
    project_items = sorted(projects.items(), key=lambda x: (-x[1], x[0]))[:40]
    return {
        "harness": [{"value": r["value"], "count": int(r["c"])} for r in harness],
        "model": [{"value": r["value"], "count": int(r["c"])} for r in model],
        "effort": [{"value": r["value"], "count": int(r["c"])} for r in effort],
        "branch": [{"value": r["value"], "count": int(r["c"])} for r in branch],
        "project": [{"value": p, "count": n} for p, n in project_items],
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
    if harness:
        ph = ",".join(f":h{i}" for i in range(len(harness)))
        clauses.append(f"s.harness IN ({ph})")
        for i, h in enumerate(harness):
            params[f"h{i}"] = h
    if model:
        ph = ",".join(f":m{i}" for i in range(len(model)))
        clauses.append(
            f"""EXISTS (
                SELECT 1
                FROM messages model_message
                WHERE model_message.session_id = s.id
                  AND model_message.role = 'assistant'
                  AND {strict_message_model_sql(message_alias='model_message', session_alias='s')} IN ({ph})
            )"""
        )
        for i, m in enumerate(model):
            params[f"m{i}"] = m
    if effort:
        ph = ",".join(f":e{i}" for i in range(len(effort)))
        clauses.append(f"COALESCE(NULLIF(s.effort, ''), '(none)') IN ({ph})")
        for i, e in enumerate(effort):
            params[f"e{i}"] = e
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

    items: list[dict[str, Any]] = []
    for r in rows:
        label = _project_label(r["repo"], r["cwd"])
        if project and label not in project:
            continue
        dur = r["duration_seconds"]
        items.append(
            {
                "id": r["id"],
                "harness": r["harness"],
                "model": display_model(r["model_canonical"]),
                "effort": r["effort"],
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
            return item["harness"]
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
        ids = [it["id"] for it in target]
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
            sid = it["id"]
            it["message_count"] = msg.get(sid, 0)
            it["tool_count"] = tools.get(sid, 0)
            it["window_count"] = windows.get(sid, 0)

    if needs_counts_for_sort:
        _attach_counts(items)

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

    messages = conn.execute(
        """
        SELECT id, seq, role, timestamp, model, model_canonical, effort, text,
               is_tool_plumbing, authored_by_agent
        FROM messages WHERE session_id = ? ORDER BY seq
        """,
        (resolved_id,),
    ).fetchall()
    tools = conn.execute(
        """
        SELECT id, message_id, seq, tool_name, action, success, duration_ms
        FROM tool_events WHERE session_id = ? ORDER BY seq
        """,
        (resolved_id,),
    ).fetchall()
    skills = conn.execute(
        """
        SELECT skill_name, exposure_type, COUNT(*) AS c
        FROM skill_exposures WHERE session_id = ?
        GROUP BY skill_name, exposure_type
        ORDER BY c DESC
        """,
        (resolved_id,),
    ).fetchall()
    skill_msgs = conn.execute(
        """
        SELECT message_id, skill_name FROM skill_exposures
        WHERE session_id = ? AND message_id IS NOT NULL
        """,
        (resolved_id,),
    ).fetchall()
    kinds = conn.execute(
        """
        SELECT w.request_message_id AS message_id, d.request_kind
        FROM exchange_windows w
        JOIN window_det_classifications d ON d.window_id = w.id
        WHERE w.session_id = ?
        """,
        (resolved_id,),
    ).fetchall()
    windows = conn.execute(
        """
        SELECT COUNT(*) AS c FROM exchange_windows WHERE session_id = ?
        """,
        (resolved_id,),
    ).fetchone()
    children = conn.execute(
        """
        SELECT id, harness, model, model_canonical, effort, started_at, ended_at,
               (SELECT COUNT(*) FROM messages m WHERE m.session_id = sessions.id) AS message_count
        FROM sessions
        WHERE parent_session_id IN (?, ?, ?)
        ORDER BY COALESCE(started_at, '') ASC
        """,
        (resolved_id, s["external_id"], f"{s['harness']}:{s['external_id']}"),
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
        "timeline": timeline,
        "messages": [_with_display_model(m) for m in messages],
        "tool_events": [dict(t) for t in tools],
        "skills": [dict(sk) for sk in skills],
        "children": [_with_display_model(c) for c in children],
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
    where, params = _session_time_clause(tr)
    clauses = [where, "messages_fts MATCH :match"]
    params["match"] = match
    if harness:
        ph = ",".join(f":h{i}" for i in range(len(harness)))
        clauses.append(f"s.harness IN ({ph})")
        for i, h in enumerate(harness):
            params[f"h{i}"] = h
    if model:
        ph = ",".join(f":m{i}" for i in range(len(model)))
        clauses.append(
            f"{sql_coalesce_model('s.model_canonical')} IN ({ph})"
        )
        for i, m in enumerate(model):
            params[f"m{i}"] = m
    where_sql = " AND ".join(clauses)
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
        label = _project_label(r["repo"], r["cwd"])
        if project and label not in project:
            continue
        items.append(
            {
                "message_id": r["message_id"],
                "session_id": r["session_id"],
                "seq": r["seq"],
                "role": r["role"],
                "timestamp": r["timestamp"],
                "snippet": r["snippet"],
                "harness": r["harness"],
                "model": display_model(
                    r["session_model"] or r["message_model"]
                ),
                "effort": r["effort"],
                "project": label,
                "started_at": r["started_at"],
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
            "Full-text search over messages_fts. Snippets use « » around matches. "
            "Click through to the session transcript at the matching message."
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
    roots = conn.execute(
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
            COUNT(c.id) AS child_count,
            (SELECT COUNT(*) FROM messages m WHERE m.session_id = p.id) AS message_count
        FROM sessions p
        JOIN sessions c ON {_parent_match_sql("p", "c")}
        WHERE {where}
        GROUP BY p.id
        ORDER BY child_count DESC, COALESCE(p.started_at, '') DESC
        LIMIT :limit
        """,
        {**params, "limit": limit},
    ).fetchall()

    kind_where, kind_params = _session_time_clause(tr)
    kinds = conn.execute(
        f"""
        SELECT d.request_kind, COUNT(*) AS c
        FROM window_det_classifications d
        JOIN exchange_windows w ON w.id = d.window_id
        JOIN sessions s ON s.id = w.session_id
        WHERE {kind_where}
          AND d.request_kind IN (
            'worker_brief', 'inter_agent_handoff', 'task_notification', 'auto_review'
          )
        GROUP BY d.request_kind
        """,
        kind_params,
    ).fetchall()
    signals = {r["request_kind"]: int(r["c"]) for r in kinds}

    child_where, child_params = _session_time_clause(tr)
    child_total = conn.execute(
        f"""
        SELECT COUNT(*) AS c FROM sessions s
        WHERE {child_where} AND s.parent_session_id IS NOT NULL
        """,
        child_params,
    ).fetchone()

    root_total = conn.execute(
        f"""
        SELECT COUNT(*) AS c FROM (
            SELECT p.id
            FROM sessions p
            JOIN sessions c ON {_parent_match_sql("p", "c")}
            WHERE {where}
            GROUP BY p.id
        )
        """,
        params,
    ).fetchone()

    items = []
    for r in roots:
        items.append(
            {
                "id": r["id"],
                "harness": r["harness"],
                "model": display_model(r["model_canonical"]),
                "model_raw": r["model_raw"],
                "effort": r["effort"],
                "project": _project_label(r["repo"], r["cwd"]),
                "started_at": r["started_at"],
                "ended_at": r["ended_at"],
                "child_count": int(r["child_count"]),
                "message_count": int(r["message_count"]),
            }
        )
    return {
        "supervisor_roots": int(root_total["c"]) if root_total else 0,
        "child_sessions": int(child_total["c"]) if child_total else 0,
        "signals": {
            "worker_brief": signals.get("worker_brief", 0),
            "inter_agent_handoff": signals.get("inter_agent_handoff", 0),
            "task_notification": signals.get("task_notification", 0),
            "auto_review": signals.get("auto_review", 0),
        },
        "items": items,
        "note": (
            "Supervisor sessions with at least one child via parent_session_id. "
            "Navigable tree is available per root."
        ),
    }


def orchestration_tree(
    conn: sqlite3.Connection, session_id: str
) -> dict[str, Any] | None:
    root = _resolve_session(conn, session_id)
    if root is None:
        return None

    def node_for(row: sqlite3.Row, depth: int = 0) -> dict[str, Any]:
        # Guard pathological cycles / extreme fan-out depth.
        if depth > 12:
            kids: list[sqlite3.Row] = []
        else:
            kids = conn.execute(
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
        msg_n = conn.execute(
            "SELECT COUNT(*) AS c FROM messages WHERE session_id = ?",
            (row["id"],),
        ).fetchone()
        tool_n = conn.execute(
            "SELECT COUNT(*) AS c FROM tool_events WHERE session_id = ?",
            (row["id"],),
        ).fetchone()
        return {
            "id": row["id"],
            "harness": row["harness"],
            "model": display_model(row["model_canonical"]),
            "model_raw": row["model"],
            "effort": row["effort"],
            "project": _project_label(row["repo"], row["cwd"]),
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
            "message_count": int(msg_n["c"]) if msg_n else 0,
            "tool_count": int(tool_n["c"]) if tool_n else 0,
            "children": [node_for(k, depth + 1) for k in kids],
        }

    # If caller passed a child, walk up to the true root for a full tree.
    walk = root
    seen: set[str] = set()
    while walk["parent_session_id"] and walk["id"] not in seen:
        seen.add(walk["id"])
        parent = _resolve_session(conn, walk["parent_session_id"])
        if parent is None:
            break
        walk = parent

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
    total = conn.execute(
        f"""
        SELECT COUNT(*) AS c
        FROM auto_review_observations a
        JOIN exchange_windows w ON w.id = a.window_id
        JOIN sessions s ON s.id = w.session_id
        WHERE {where}
        """,
        params,
    ).fetchone()
    by_model = conn.execute(
        f"""
        SELECT {sql_coalesce_model('s.model_canonical')} AS model,
               s.harness AS harness,
               COUNT(*) AS count
        FROM auto_review_observations a
        JOIN exchange_windows w ON w.id = a.window_id
        JOIN sessions s ON s.id = w.session_id
        WHERE {where}
        GROUP BY 1, 2
        ORDER BY 3 DESC
        """,
        params,
    ).fetchall()
    by_day = conn.execute(
        f"""
        SELECT substr(COALESCE(s.started_at, a.created_at), 1, 10) AS day,
               COUNT(*) AS c
        FROM auto_review_observations a
        JOIN exchange_windows w ON w.id = a.window_id
        JOIN sessions s ON s.id = w.session_id
        WHERE {where}
        GROUP BY day
        ORDER BY day
        """,
        params,
    ).fetchall()
    recent = conn.execute(
        f"""
        SELECT
            a.id,
            a.window_id,
            a.created_at,
            s.id AS session_id,
            s.harness,
            s.model_canonical,
            s.effort,
            s.repo,
            s.cwd,
            s.started_at,
            a.payload_json
        FROM auto_review_observations a
        JOIN exchange_windows w ON w.id = a.window_id
        JOIN sessions s ON s.id = w.session_id
        WHERE {where}
        ORDER BY COALESCE(a.created_at, s.started_at, '') DESC
        LIMIT :limit
        """,
        {**params, "limit": limit},
    ).fetchall()

    import json

    items = []
    for r in recent:
        try:
            payload = json.loads(r["payload_json"] or "{}")
        except json.JSONDecodeError:
            payload = {}
        items.append(
            {
                "id": r["id"],
                "window_id": r["window_id"],
                "session_id": r["session_id"],
                "harness": r["harness"],
                "model": display_model(r["model_canonical"]),
                "effort": r["effort"],
                "project": _project_label(r["repo"], r["cwd"]),
                "started_at": r["started_at"],
                "created_at": r["created_at"],
                "status": payload.get("status"),
                "route": payload.get("route"),
                "request_kind": payload.get("request_kind", "auto_review"),
            }
        )
    return {
        "total": int(total["c"]) if total else 0,
        "by_model": collapse_by_model(
            [dict(r) for r in by_model], count_key="count"
        )[:30],
        "by_day": [{"day": r["day"], "count": int(r["c"])} for r in by_day if r["day"]],
        "items": items,
        "note": (
            "Auto-review traffic is excluded from UX interaction-style metrics, "
            "but it is real work and listed here as observed volume."
        ),
    }
