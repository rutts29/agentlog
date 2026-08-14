"""Ungated descriptive analytics and browse/search queries.

Counts, distributions, listings, and full-text search are always defensible.
Comparative rate claims and quality judgments stay in queries.py behind gates.
"""

from __future__ import annotations

import re
import sqlite3
from threading import Event
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from agentlog.api.identity_aggregates import (
    VisibleLogicalSession,
    visible_logical_sessions,
)
from agentlog.api.model_rollup import collapse_by_model, strict_message_model_sql
from agentlog.api.ranges import TimeRange, session_time_clause as _session_time_clause
from agentlog.api.search import SourceReader, search_messages as _dual_search_messages
from agentlog.session_identity import (
    IdentityContext,
    build_identity_context,
    lineage_parent_ids,
    logical_orchestrator_id,
    logical_root_session_id,
    logical_projection,
    provider_root_shadow_ids,
    resolve_implicit_parent_ids,
    is_suppressed_activity_session,
)
from agentlog.normalize.model_identity import display_model
from agentlog.source_reader import read_source_transcript

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

_TREE_MAX_NODES = 500
_TREE_MAX_DEPTH = 64
_DETAIL_MAX_CHILDREN = 200


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
    parents = lineage_parent_ids(conn)
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
            1 for session in sessions if session.session_id in parents
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


def session_facets(
    conn: sqlite3.Connection, tr: TimeRange, *, view: str | None = None
) -> dict[str, Any]:
    if view == "roots":
        return _root_session_facets(conn, tr)
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


@dataclass
class _SessionTopology:
    rows: dict[str, sqlite3.Row]
    parent_by_id: dict[str, str]
    children_by_id: dict[str, list[str]]
    root_by_id: dict[str, str]
    roots: set[str]
    orphan_roots: set[str]
    hidden_ids: set[str]
    relationship_by_id: dict[str, str]
    descendant_counts: dict[str, int]


def _workflow_child_sort_key(row: sqlite3.Row, session_id: str) -> tuple[Any, ...]:
    group_id = row["workflow_group_id"]
    if group_id:
        position = row["workflow_group_position"]
        return (
            0,
            int(position) if isinstance(position, int) else 2**31 - 1,
            str(row["workflow_group_label"] or group_id).casefold(),
            str(group_id),
            str(row["started_at"] or ""),
            session_id,
        )
    return (1, 0, "", "", str(row["started_at"] or ""), session_id)


def _session_topology(
    conn: sqlite3.Connection, identity: IdentityContext
) -> _SessionTopology:
    rows = {
        str(row["id"]): row
        for row in conn.execute("SELECT * FROM sessions ORDER BY id").fetchall()
    }
    physical_parents = resolve_implicit_parent_ids(rows.values())
    unresolved = {
        session_id
        for session_id, row in rows.items()
        if row["parent_session_id"] and session_id not in physical_parents
    }

    hidden_ids = provider_root_shadow_ids(conn, context=identity) | {
        session_id
        for session_id, row in rows.items()
        if is_suppressed_activity_session(row)
    }
    visible_ids = set(rows).difference(hidden_ids)
    parent_by_id: dict[str, str] = {}
    relationship_by_id: dict[str, str] = {}
    for session_id in sorted(visible_ids):
        physical_parent = physical_parents.get(session_id)
        if physical_parent is None:
            continue
        if physical_parent in hidden_ids:
            owner = logical_orchestrator_id(
                conn, physical_parent, context=identity
            )
            if owner in visible_ids and owner != session_id:
                parent_by_id[session_id] = str(owner)
                relationship_by_id[session_id] = "provider_child"
            else:
                unresolved.add(session_id)
            continue
        if physical_parent in visible_ids:
            parent_by_id[session_id] = physical_parent
            relationship_by_id[session_id] = "child"

    for source_id, backings in identity.backings_by_source.items():
        if source_id not in visible_ids:
            continue
        for backing in backings:
            target_id = backing.get("target_session_id")
            if (
                backing.get("link_role") != "worker"
                or not target_id
                or str(target_id) not in visible_ids
                or identity.owners_by_session.get(str(target_id), set())
                != {source_id}
                or str(target_id) == source_id
            ):
                continue
            target = str(target_id)
            ancestor = parent_by_id.get(target)
            seen_ancestors: set[str] = set()
            while ancestor and ancestor not in seen_ancestors:
                if ancestor == source_id:
                    break
                seen_ancestors.add(ancestor)
                ancestor = parent_by_id.get(ancestor)
            if ancestor != source_id:
                parent_by_id[target] = source_id
                relationship_by_id[target] = "provider_worker"

    cycle_roots: set[str] = set()
    settled: set[str] = set()
    for start in sorted(visible_ids):
        if start in settled:
            continue
        path: list[str] = []
        positions: dict[str, int] = {}
        current: str | None = start
        while current is not None and current not in settled:
            if current in positions:
                cycle = path[positions[current] :]
                break_id = min(cycle)
                parent_by_id.pop(break_id, None)
                relationship_by_id.pop(break_id, None)
                cycle_roots.add(break_id)
                break
            positions[current] = len(path)
            path.append(current)
            current = parent_by_id.get(current)
        settled.update(path)

    children_by_id: dict[str, list[str]] = {
        session_id: [] for session_id in visible_ids
    }
    for child_id, parent_id in parent_by_id.items():
        children_by_id[parent_id].append(child_id)
    for child_ids in children_by_id.values():
        child_ids.sort(
            key=lambda child_id: _workflow_child_sort_key(
                rows[child_id], child_id
            )
        )

    roots = {session_id for session_id in visible_ids if session_id not in parent_by_id}
    root_by_id: dict[str, str] = {}
    descendant_counts: dict[str, int] = {}

    for root_id in sorted(roots):
        stack = [root_id]
        order: list[str] = []
        while stack:
            session_id = stack.pop()
            root_by_id[session_id] = root_id
            order.append(session_id)
            stack.extend(children_by_id[session_id])
        for session_id in reversed(order):
            descendant_counts[session_id] = sum(
                1 + descendant_counts[child_id]
                for child_id in children_by_id[session_id]
            )

    orphan_roots = roots.intersection(unresolved.union(cycle_roots))
    return _SessionTopology(
        rows=rows,
        parent_by_id=parent_by_id,
        children_by_id=children_by_id,
        root_by_id=root_by_id,
        roots=roots,
        orphan_roots=orphan_roots,
        hidden_ids=hidden_ids,
        relationship_by_id=relationship_by_id,
        descendant_counts=descendant_counts,
    )


def _session_node_bases(
    conn: sqlite3.Connection,
    topology: _SessionTopology,
    identity: IdentityContext,
    session_ids: set[str],
    root_navigation_id: str,
) -> dict[str, dict[str, Any]]:
    projections = {
        session_id: logical_projection(
            conn,
            session_id,
            str(topology.rows[session_id]["harness"]),
            context=identity,
        )
        for session_id in session_ids
    }
    metric_id_by_session = {
        session_id: str(
            projections[session_id]["transcript_session_id"] or session_id
        )
        for session_id in session_ids
    }
    metric_ids = sorted(set(metric_id_by_session.values()))

    def counts(table: str) -> dict[str, int]:
        if not metric_ids:
            return {}
        placeholders = ",".join("?" for _ in metric_ids)
        return {
            str(row["session_id"]): int(row["c"])
            for row in conn.execute(
                f"SELECT session_id, COUNT(*) AS c FROM {table} "
                f"WHERE session_id IN ({placeholders}) GROUP BY session_id",
                metric_ids,
            ).fetchall()
        }

    message_counts = counts("messages")
    tool_counts = counts("tool_events")
    bases: dict[str, dict[str, Any]] = {}
    for session_id in session_ids:
        row = topology.rows[session_id]
        projection = projections[session_id]
        metric_id = metric_id_by_session[session_id]
        metric = topology.rows.get(metric_id, row)
        parent_navigation_id = topology.parent_by_id.get(session_id)
        base = {
            "id": session_id,
            "navigation_id": session_id,
            "parent_navigation_id": parent_navigation_id,
            "root_navigation_id": root_navigation_id,
            "harness": row["harness"],
            **projection,
            "model": display_model(metric["model_canonical"]),
            "model_raw": metric["model"],
            "effort": metric["effort"],
            "project": _project_label(row["repo"], row["cwd"]),
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
            "parent_session_id": row["parent_session_id"],
            "thread_source": row["thread_source"],
            "message_count": message_counts.get(metric_id, 0),
            "tool_count": tool_counts.get(metric_id, 0),
            "child_count": len(topology.children_by_id[session_id]),
            "descendant_count": topology.descendant_counts[session_id],
            "is_orphan": session_id in topology.orphan_roots,
            "inherited_message_count": int(metric["inherited_message_count"] or 0),
            "inherited_record_count": int(metric["inherited_record_count"] or 0),
            "fork_context_status": metric["fork_context_status"],
            "fork_context_boundary": metric["fork_context_boundary"],
            "workflow_group_id": row["workflow_group_id"],
            "workflow_group_label": row["workflow_group_label"],
            "workflow_group_position": row["workflow_group_position"],
        }
        relationship = topology.relationship_by_id.get(session_id)
        if relationship:
            base["relationship"] = relationship
        bases[session_id] = base
    return bases


def _conversation_members(
    topology: _SessionTopology, root_id: str
) -> tuple[str, ...]:
    members: list[str] = []
    pending = [root_id]
    while pending:
        session_id = pending.pop()
        members.append(session_id)
        pending.extend(reversed(topology.children_by_id[session_id]))
    return tuple(members)


def _latest_timestamp(
    topology: _SessionTopology, session_ids: tuple[str, ...]
) -> str | None:
    values = [
        str(value)
        for session_id in session_ids
        for value in (
            topology.rows[session_id]["started_at"],
            topology.rows[session_id]["ended_at"],
        )
        if value
    ]
    if not values:
        return None

    def key(value: str) -> tuple[float, str]:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.timestamp(), value
        except ValueError:
            return float("-inf"), value

    return max(values, key=key)


def _eligible_session_ids_by_root(
    conn: sqlite3.Connection,
    tr: TimeRange,
    topology: _SessionTopology,
    identity: IdentityContext,
) -> dict[str, set[str]]:
    range_where, range_params = _session_time_clause(tr)
    in_range_ids = {
        str(row["id"])
        for row in conn.execute(
            f"SELECT s.id FROM sessions s WHERE {range_where}", range_params
        ).fetchall()
    }
    eligible_by_root: dict[str, set[str]] = defaultdict(set)
    for session_id in in_range_ids:
        navigation_id = session_id
        if session_id in topology.hidden_ids:
            owner_id = logical_orchestrator_id(
                conn, session_id, context=identity
            )
            if owner_id not in topology.rows:
                continue
            navigation_id = str(owner_id)
        root_id = topology.root_by_id.get(navigation_id)
        if root_id in topology.roots:
            eligible_by_root[root_id].add(session_id)
    return dict(eligible_by_root)


def _model_values_by_session(
    conn: sqlite3.Connection, session_ids: set[str]
) -> dict[str, set[str]]:
    values: dict[str, set[str]] = {session_id: set() for session_id in session_ids}
    if not session_ids:
        return values
    placeholders = ",".join("?" for _ in session_ids)
    sessions = conn.execute(
        f"SELECT id, model_canonical FROM sessions WHERE id IN ({placeholders})",
        sorted(session_ids),
    ).fetchall()
    for row in sessions:
        values[str(row["id"])].add(display_model(row["model_canonical"]))
    messages = conn.execute(
        f"""
        SELECT model_message.session_id,
               {strict_message_model_sql(message_alias='model_message', session_alias='model_session')} AS model_value
        FROM messages model_message
        JOIN sessions model_session ON model_session.id = model_message.session_id
        WHERE model_message.session_id IN ({placeholders})
          AND model_message.role = 'assistant'
        """,
        sorted(session_ids),
    ).fetchall()
    for row in messages:
        values[str(row["session_id"])].add(display_model(row["model_value"]))
    return values


def _root_session_facets(
    conn: sqlite3.Connection, tr: TimeRange
) -> dict[str, Any]:
    identity = build_identity_context(conn)
    topology = _session_topology(conn, identity)
    eligible_by_root = _eligible_session_ids_by_root(
        conn, tr, topology, identity
    )
    members_by_root = {
        root_id: tuple(sorted(eligible_ids))
        for root_id, eligible_ids in eligible_by_root.items()
    }
    member_ids = {
        session_id
        for members in members_by_root.values()
        for session_id in members
    }
    projections = {
        session_id: logical_projection(
            conn,
            session_id,
            str(topology.rows[session_id]["harness"]),
            context=identity,
        )
        for session_id in member_ids
    }
    metric_id_by_session = {
        session_id: str(
            projections[session_id]["transcript_session_id"] or session_id
        )
        for session_id in member_ids
    }
    model_values = _model_values_by_session(
        conn, set(metric_id_by_session.values())
    )
    buckets: dict[str, dict[str, int]] = {
        key: defaultdict(int)
        for key in ("harness", "model", "effort", "branch", "project")
    }
    for members in members_by_root.values():
        values: dict[str, set[str]] = {
            key: set() for key in buckets
        }
        for session_id in members:
            row = topology.rows[session_id]
            metric_id = metric_id_by_session[session_id]
            metric = topology.rows.get(metric_id, row)
            values["harness"].add(str(projections[session_id]["logical_harness"]))
            values["model"].update(model_values.get(metric_id, set()))
            values["effort"].add(str(metric["effort"] or "(none)"))
            values["branch"].add(str(row["branch"] or "(none)"))
            values["project"].add(_project_label(row["repo"], row["cwd"]))
        for key, distinct_values in values.items():
            for value in distinct_values:
                buckets[key][value] += 1

    def items(key: str) -> list[dict[str, Any]]:
        return [
            {"value": value, "count": count}
            for value, count in sorted(
                buckets[key].items(), key=lambda item: (-item[1], item[0])
            )[:40]
        ]

    return {key: items(key) for key in buckets}


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
    sort_key = sort if sort in _SORT_COLUMNS else "started_at"
    direction = "ASC" if order.lower() == "asc" else "DESC"
    identity = build_identity_context(conn)
    topology = _session_topology(conn, identity)
    eligible_by_root = _eligible_session_ids_by_root(
        conn, tr, topology, identity
    )
    members_by_root = {
        root_id: _conversation_members(topology, root_id)
        for root_id in sorted(eligible_by_root)
    }
    member_ids = {
        session_id
        for members in members_by_root.values()
        for session_id in members
    }
    member_ids.update(
        session_id
        for eligible_ids in eligible_by_root.values()
        for session_id in eligible_ids
    )
    projections = {
        session_id: logical_projection(
            conn,
            session_id,
            str(topology.rows[session_id]["harness"]),
            context=identity,
        )
        for session_id in member_ids
    }
    metric_id_by_session = {
        session_id: str(
            projections[session_id]["transcript_session_id"] or session_id
        )
        for session_id in member_ids
    }
    metric_ids = sorted(set(metric_id_by_session.values()))

    model_values = (
        _model_values_by_session(conn, set(metric_ids))
        if model
        else {}
    )

    def node_matches(session_id: str) -> bool:
        row = topology.rows[session_id]
        projection = projections[session_id]
        metric_id = metric_id_by_session[session_id]
        metric = topology.rows.get(metric_id, row)
        if harness and projection["logical_harness"] not in harness:
            return False
        if model and not set(model).intersection(model_values.get(metric_id, set())):
            return False
        effort_value = metric["effort"] or "(none)"
        if effort and effort_value not in effort:
            return False
        branch_value = row["branch"] or "(none)"
        if branch and branch_value not in branch:
            return False
        if project and _project_label(row["repo"], row["cwd"]) not in project:
            return False
        if q:
            needle = q.casefold()
            values = (
                session_id,
                row["repo"],
                row["cwd"],
                metric["model"],
                metric["model_canonical"],
                row["branch"],
            )
            if not any(
                needle in str(value).casefold()
                for value in values
                if value not in (None, "")
            ):
                return False
        return True

    def counts(table: str) -> dict[str, int]:
        if not metric_ids:
            return {}
        placeholders = ",".join("?" for _ in metric_ids)
        return {
            str(row["session_id"]): int(row["c"])
            for row in conn.execute(
                f"SELECT session_id, COUNT(*) AS c FROM {table} "
                f"WHERE session_id IN ({placeholders}) GROUP BY session_id",
                metric_ids,
            ).fetchall()
        }

    message_counts = counts("messages")
    tool_counts = counts("tool_events")
    window_counts = counts("exchange_windows")
    has_filters = bool(harness or model or effort or branch or project or q)

    items: list[dict[str, Any]] = []
    for root_id, members in members_by_root.items():
        matching_members = [
            session_id
            for session_id in eligible_by_root[root_id]
            if node_matches(session_id)
        ]
        if not matching_members:
            continue
        matching_descendant_count = (
            sum(session_id != root_id for session_id in matching_members)
            if has_filters
            else 0
        )
        row = topology.rows[root_id]
        projection = projections[root_id]
        metric_id = metric_id_by_session[root_id]
        metric = topology.rows.get(metric_id, row)
        conversation_metric_ids = {
            metric_id_by_session[session_id] for session_id in members
        }
        duration_seconds = None
        if row["started_at"] and row["ended_at"]:
            try:
                started = datetime.fromisoformat(
                    str(row["started_at"]).replace("Z", "+00:00")
                )
                ended = datetime.fromisoformat(
                    str(row["ended_at"]).replace("Z", "+00:00")
                )
                duration_seconds = max(0, int((ended - started).total_seconds()))
            except ValueError:
                duration_seconds = None
        items.append(
            {
                "id": root_id,
                "navigation_id": root_id,
                "parent_navigation_id": None,
                "root_navigation_id": root_id,
                "harness": row["harness"],
                **projection,
                "model": display_model(metric["model_canonical"]),
                "effort": metric["effort"],
                "project": _project_label(row["repo"], row["cwd"]),
                "repo": row["repo"],
                "branch": row["branch"],
                "started_at": row["started_at"],
                "activity_at": _latest_timestamp(topology, members),
                "latest_descendant_at": _latest_timestamp(topology, members[1:]),
                "ended_at": row["ended_at"],
                "duration_seconds": duration_seconds,
                "message_count": sum(
                    message_counts.get(session_id, 0)
                    for session_id in conversation_metric_ids
                ),
                "tool_count": sum(
                    tool_counts.get(session_id, 0)
                    for session_id in conversation_metric_ids
                ),
                "window_count": sum(
                    window_counts.get(session_id, 0)
                    for session_id in conversation_metric_ids
                ),
                "child_count": len(topology.children_by_id[root_id]),
                "descendant_count": topology.descendant_counts[root_id],
                "is_orphan": root_id in topology.orphan_roots,
                "matched_in_descendant": bool(
                    has_filters
                    and root_id not in matching_members
                    and matching_descendant_count
                ),
                "matching_descendant_count": matching_descendant_count,
                "parent_session_id": row["parent_session_id"],
                "thread_source": row["thread_source"],
                "transcript_storage": (
                    metric["transcript_storage"] or "legacy_materialized"
                ),
                "inherited_message_count": int(metric["inherited_message_count"] or 0),
                "inherited_record_count": int(metric["inherited_record_count"] or 0),
                "fork_context_status": metric["fork_context_status"],
                "fork_context_boundary": metric["fork_context_boundary"],
                "status": "observed",
            }
        )

    reverse = direction == "DESC"

    def sort_value(item: dict[str, Any]) -> Any:
        if sort_key == "started_at":
            return item["activity_at"] or ""
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
        return item["activity_at"] or ""

    items.sort(key=lambda item: str(item["id"]))
    items.sort(key=sort_value, reverse=reverse)
    total = len(items)
    offset = max(0, cursor)
    page = items[offset : offset + limit]
    return {
        "view": "roots",
        "count_scope": "full_conversation",
        "note": (
            "Range and filters choose matching branches; counts cover each "
            "full conversation."
        ),
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
    conn: sqlite3.Connection,
    session_id: str,
    *,
    source_reader: SourceReader | None = None,
) -> dict[str, Any] | None:
    resolved = _resolve_session(conn, session_id)
    if resolved is None:
        return None
    if is_suppressed_activity_session(resolved):
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
    topology = _session_topology(conn, identity)
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
    source_read = (source_reader or read_source_transcript)(conn, transcript_id)
    if source_read.status != "legacy":
        messages = source_read.messages
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

    navigation_id = resolved_id
    if resolved_id in topology.hidden_ids:
        owner_id = logical_orchestrator_id(conn, resolved_id, context=identity)
        if owner_id in topology.rows:
            navigation_id = str(owner_id)
    root_navigation_id = topology.root_by_id.get(navigation_id, navigation_id)
    parent_navigation_id = topology.parent_by_id.get(navigation_id)
    all_child_ids = topology.children_by_id.get(navigation_id, [])
    child_ids = all_child_ids[:_DETAIL_MAX_CHILDREN]
    total_child_count = len(all_child_ids)
    child_bases = _session_node_bases(
        conn,
        topology,
        identity,
        set(child_ids),
        root_navigation_id,
    )

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

    transcript_payload: dict[str, Any] = {
        "id": transcript["id"],
        "harness": transcript["harness"],
        "artifact_id": transcript["artifact_id"],
        "artifact_path": transcript["artifact_path"],
    }
    if source_read.status != "legacy":
        transcript_payload["source"] = {
            "status": source_read.status,
            "unit_id": source_read.source_unit_id,
            "identity": source_read.source_identity,
            "hash": source_read.source_hash,
            "warning": source_read.warning,
        }

    return {
        "session": {
            "id": s["id"],
            "navigation_id": navigation_id,
            "parent_navigation_id": parent_navigation_id,
            "root_navigation_id": root_navigation_id,
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
            "child_count": total_child_count,
            "descendant_count": topology.descendant_counts.get(navigation_id, 0),
            "is_orphan": navigation_id in topology.orphan_roots,
            "inherited_message_count": int(
                transcript["inherited_message_count"] or 0
            ),
            "inherited_record_count": int(
                transcript["inherited_record_count"] or 0
            ),
            "fork_context_status": transcript["fork_context_status"],
            "fork_context_boundary": transcript["fork_context_boundary"],
            "workflow_group_id": s["workflow_group_id"],
            "workflow_group_label": s["workflow_group_label"],
            "workflow_group_position": s["workflow_group_position"],
            "artifact_id": s["artifact_id"],
            "artifact_path": s["artifact_path"],
            "external_id": s["external_id"],
        },
        "transcript": transcript_payload,
        "timeline": timeline,
        "messages": [_with_display_model(m) for m in messages],
        "tool_events": [dict(t) for t in tools],
        "skills": [dict(sk) for sk in skills],
        "children": [child_bases[child_id] for child_id in child_ids],
        "children_bounds": {
            "limit": _DETAIL_MAX_CHILDREN,
            "returned_child_count": len(child_ids),
            "total_child_count": total_child_count,
            "truncated": len(child_ids) < total_child_count,
            "omitted_child_count": total_child_count - len(child_ids),
        },
        "inherited_context": {
            "status": transcript["fork_context_status"],
            "message_count": int(transcript["inherited_message_count"] or 0),
            "record_count": int(transcript["inherited_record_count"] or 0),
            "boundary": transcript["fork_context_boundary"],
            "parent_navigation_id": parent_navigation_id,
        },
        "anatomy": {
            "message_count": len(messages),
            "tool_count": len(tools),
            "window_count": int(windows["c"]) if windows else 0,
            "child_count": total_child_count,
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
    source_reader: SourceReader | None = None,
    source_scan_limit: int = 200,
    cancelled: Event | None = None,
) -> dict[str, Any]:
    return _dual_search_messages(
        conn,
        tr,
        q=q,
        harness=harness,
        model=model,
        project=project,
        cursor=cursor,
        limit=limit,
        source_reader=source_reader,
        source_scan_limit=source_scan_limit,
        cancelled=cancelled,
    )


def _parent_match_sql(parent_alias: str = "p", child_alias: str = "c") -> str:
    """parent_session_id is often a bare external_id, while sessions.id is harness:external_id."""
    return f"""(
        {child_alias}.harness = {parent_alias}.harness
        AND (
            {child_alias}.parent_session_id = {parent_alias}.id
            OR {child_alias}.parent_session_id IN (
                {parent_alias}.external_id,
                {parent_alias}.harness || ':' || {parent_alias}.external_id
            )
        )
    )"""


def _resolve_session(
    conn: sqlite3.Connection, session_id: str
) -> sqlite3.Row | None:
    row = conn.execute(
        "SELECT * FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if row is not None:
        return row
    candidates = conn.execute(
        """
        SELECT * FROM sessions
        WHERE external_id = ?
           OR external_id LIKE '%/' || ?
           OR id LIKE '%/' || ?
        ORDER BY id
        """,
        (session_id, session_id, session_id),
    ).fetchall()
    unique = {str(candidate["id"]): candidate for candidate in candidates}
    return next(iter(unique.values())) if len(unique) == 1 else None


def orchestration_overview(
    conn: sqlite3.Connection, tr: TimeRange, *, limit: int = 40
) -> dict[str, Any]:
    where, params = _session_time_clause(tr, alias="p")
    candidate_rows = conn.execute(
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
            p.ended_at
        FROM sessions p
        WHERE {where}
        """,
        params,
    ).fetchall()
    candidate_rows = [
        row for row in candidate_rows if not is_suppressed_activity_session(row)
    ]
    candidates_by_id = {str(row["id"]): row for row in candidate_rows}
    parent_rows = conn.execute(
        "SELECT id, harness, external_id, parent_session_id, thread_source FROM sessions"
    ).fetchall()
    parent_rows = [
        row for row in parent_rows if not is_suppressed_activity_session(row)
    ]
    implicit_parents = resolve_implicit_parent_ids(
        parent_rows
    )
    children_by_root: dict[str, set[str]] = {}
    for child_id, parent_id in implicit_parents.items():
        if parent_id in candidates_by_id:
            children_by_root.setdefault(parent_id, set()).add(child_id)

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
    physical_roots = {
        root_id: candidates_by_id[root_id]
        for root_id in children_by_root
    }

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
    requested = _resolve_session(conn, session_id)
    if requested is None:
        return None
    identity = build_identity_context(conn)
    topology = _session_topology(conn, identity)
    requested_id = str(requested["id"])
    requested_navigation_id = requested_id
    if requested_id in topology.hidden_ids:
        owner_id = logical_orchestrator_id(conn, requested_id, context=identity)
        if owner_id in topology.rows:
            requested_navigation_id = str(owner_id)
    root_id = topology.root_by_id.get(
        requested_navigation_id, requested_navigation_id
    )
    if root_id not in topology.rows or root_id in topology.hidden_ids:
        return None

    branch_ids: list[str] = []
    pending = [(root_id, 0)]
    while pending and len(branch_ids) < _TREE_MAX_NODES:
        current, depth = pending.pop()
        branch_ids.append(current)
        if depth < _TREE_MAX_DEPTH:
            pending.extend(
                (child_id, depth + 1)
                for child_id in reversed(topology.children_by_id[current])
            )
    emitted_ids = set(branch_ids)
    bases = _session_node_bases(
        conn, topology, identity, emitted_ids, root_id
    )

    nodes: dict[str, dict[str, Any]] = {}
    returned_subtree_counts: dict[str, int] = {}
    for node_id in reversed(branch_ids):
        emitted_children = [
            child_id
            for child_id in topology.children_by_id[node_id]
            if child_id in emitted_ids
        ]
        returned_descendant_count = sum(
            returned_subtree_counts[child_id] for child_id in emitted_children
        )
        omitted_descendant_count = max(
            0,
            topology.descendant_counts.get(node_id, 0)
            - returned_descendant_count,
        )
        returned_subtree_counts[node_id] = returned_descendant_count + 1
        nodes[node_id] = {
            **bases[node_id],
            "children_truncated": omitted_descendant_count > 0,
            "omitted_descendant_count": omitted_descendant_count,
            "children": [
                nodes[child_id]
                for child_id in emitted_children
            ],
        }
    tree = nodes[root_id]
    total_node_count = topology.descendant_counts.get(root_id, 0) + 1
    returned_node_count = len(branch_ids)
    return {
        "root_id": root_id,
        "requested_id": session_id,
        "requested_navigation_id": requested_navigation_id,
        "bounds": {
            "max_nodes": _TREE_MAX_NODES,
            "max_depth": _TREE_MAX_DEPTH,
            "returned_node_count": returned_node_count,
            "total_node_count": total_node_count,
            "truncated": returned_node_count < total_node_count,
            "omitted_node_count": total_node_count - returned_node_count,
        },
        "tree": tree,
        "note": (
            "Session tree from resolved physical parents and observed worker links. "
            "Provider transcript shadows are provenance, not duplicate nodes."
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
