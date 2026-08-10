from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlparse

from agentlog.analysis.performance.gates import (
    AggregateCell,
    evaluate_binary_rate,
    unavailable_cell,
)
from agentlog.api.identity_aggregates import (
    VisibleLogicalSession,
    visible_logical_sessions,
)
from agentlog.api.model_rollup import (
    collapse_by_model,
    strict_message_model_sql,
    unknown_breakdown,
)
from agentlog.api.ranges import TimeRange, session_time_clause as _session_time_clause
from agentlog.api.semantic import redirect_cell
from agentlog.normalize.model_identity import (
    UNKNOWN_MODEL_LABEL,
    display_model,
    sql_coalesce_model,
)


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


def _aggregate_sessions(
    conn: sqlite3.Connection, tr: TimeRange
) -> list[VisibleLogicalSession]:
    where, params = _session_time_clause(tr)
    rows = conn.execute(
        f"""
        SELECT s.id, s.harness, s.started_at, s.ended_at, s.repo, s.cwd,
               s.model_canonical, s.agent_profile, s.effort
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
        f"""
        SELECT id, model_canonical, agent_profile
        FROM sessions WHERE id IN ({placeholders})
        """,
        ids,
    ).fetchall()
    return {str(row["id"]): row for row in rows}


def ingest_freshness(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS sessions,
            MAX(COALESCE(ended_at, started_at)) AS last_at
        FROM sessions
        """
    ).fetchone()
    return {
        "sessions": int(row["sessions"]) if row else 0,
        "last_at": row["last_at"] if row else None,
    }


def count_sessions(conn: sqlite3.Connection, tr: TimeRange) -> int:
    return len(_aggregate_sessions(conn, tr))


def count_ux_observations(conn: sqlite3.Connection, tr: TimeRange) -> int:
    where, params = _session_time_clause(tr)
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS c
        FROM ux_observations u
        JOIN exchange_windows w ON w.id = u.window_id
        JOIN sessions s ON s.id = w.session_id
        WHERE {where}
        """,
        params,
    ).fetchone()
    return int(row["c"]) if row else 0


def streak_days(conn: sqlite3.Connection, tr: TimeRange) -> dict[str, Any]:
    days = sorted(
        {
            str(session.row["started_at"])[:10]
            for session in _aggregate_sessions(conn, tr)
            if session.row["started_at"]
        },
        reverse=True,
    )
    if not days:
        return {"current": 0, "longest": 0}
    # Current streak from most recent day backward (calendar gaps break it).
    current = 1
    for i in range(1, len(days)):
        prev = datetime.fromisoformat(days[i - 1])
        cur = datetime.fromisoformat(days[i])
        if (prev - cur).days == 1:
            current += 1
        else:
            break
    longest = 1
    run = 1
    for i in range(1, len(days)):
        prev = datetime.fromisoformat(days[i - 1])
        cur = datetime.fromisoformat(days[i])
        if (prev - cur).days == 1:
            run += 1
            longest = max(longest, run)
        else:
            run = 1
    return {"current": current, "longest": max(longest, current)}


def sessions_by_harness_daily(
    conn: sqlite3.Connection, tr: TimeRange
) -> list[dict[str, Any]]:
    by_day: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    harnesses: set[str] = set()
    for session in _aggregate_sessions(conn, tr):
        started_at = session.row["started_at"]
        if not started_at:
            continue
        day = str(started_at)[:10]
        by_day[day][session.logical_harness] += 1
        harnesses.add(session.logical_harness)
    out: list[dict[str, Any]] = []
    for day in sorted(by_day):
        item: dict[str, Any] = {"day": day, "total": sum(by_day[day].values())}
        for h in sorted(harnesses):
            item[h] = by_day[day].get(h, 0)
        out.append(item)
    return out


def model_mix(conn: sqlite3.Connection, tr: TimeRange) -> list[dict[str, Any]]:
    """Descriptive assistant-message model shares — not a quality ranking.

    One row per model identity. The harness split rides along as a
    breakdown so the rendered label stays the grouping key.
    """
    sessions = _aggregate_sessions(conn, tr)
    by_metric = {session.metric_session_id: session for session in sessions}
    metric_ids = sorted(by_metric)
    if not metric_ids:
        return []
    placeholders = ",".join("?" for _ in metric_ids)
    model_expr = strict_message_model_sql()
    rows = conn.execute(
        f"""
        SELECT m.session_id, {model_expr} AS model
        FROM messages m
        JOIN sessions s ON s.id = m.session_id
        WHERE m.session_id IN ({placeholders})
          AND m.role = 'assistant'
        """,
        metric_ids,
    ).fetchall()
    messages: dict[tuple[str, str], int] = defaultdict(int)
    session_ids: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in rows:
        session = by_metric[str(row["session_id"])]
        key = (str(row["model"]), session.logical_harness)
        messages[key] += 1
        session_ids[key].add(session.session_id)
    raw_rows = [
        {"model": model, "harness": harness, "messages": count}
        for (model, harness), count in messages.items()
    ]
    collapsed = collapse_by_model(raw_rows, count_key="messages")
    session_rows = {
        row["model"]: row
        for row in collapse_by_model(
            [
                {
                    "model": model,
                    "harness": harness,
                    "sessions": len(ids),
                }
                for (model, harness), ids in session_ids.items()
            ],
            count_key="sessions",
        )
    }
    total = sum(int(r["messages"]) for r in collapsed) or 1
    return [
        {
            "model": r["model"],
            "messages": r["messages"],
            "sessions": session_rows[r["model"]]["sessions"],
            "share": r["messages"] / total,
            "harnesses": session_rows[r["model"]]["harnesses"],
        }
        for r in collapsed
    ]


def unknown_model_detail(
    conn: sqlite3.Connection, tr: TimeRange
) -> dict[str, Any]:
    """Assistant messages whose model could not be resolved, with the reason."""
    sessions = _aggregate_sessions(conn, tr)
    by_metric = {session.metric_session_id: session for session in sessions}
    metric_ids = sorted(by_metric)
    if not metric_ids:
        return unknown_breakdown([])
    placeholders = ",".join("?" for _ in metric_ids)
    rows = conn.execute(
        f"""
        SELECT m.session_id, m.model AS model_raw
        FROM messages m
        JOIN sessions s ON s.id = m.session_id
        WHERE m.session_id IN ({placeholders})
          AND m.role = 'assistant'
          AND {strict_message_model_sql()} = ?
        """,
        [*metric_ids, UNKNOWN_MODEL_LABEL],
    ).fetchall()
    by_raw: dict[str | None, dict[str, Any]] = {}
    for row in rows:
        raw = row["model_raw"]
        entry = by_raw.setdefault(raw, {"model_raw": raw, "messages": 0, "ids": set()})
        entry["messages"] += 1
        entry["ids"].add(by_metric[str(row["session_id"])].session_id)
    raw_rows = [
        {
            "model_raw": entry["model_raw"],
            "messages": entry["messages"],
            "sessions": len(entry["ids"]),
        }
        for entry in by_raw.values()
    ]
    by_session = unknown_breakdown(raw_rows)
    by_message = unknown_breakdown(raw_rows, count_key="messages")
    message_reasons = {r["reason"]: r for r in by_message["reasons"]}
    return {
        **by_session,
        "messages": by_message["messages"],
        "reasons": [
            {**row, "messages": message_reasons[row["reason"]]["messages"]}
            for row in by_session["reasons"]
        ],
    }


def agent_profile_mix(
    conn: sqlite3.Connection, tr: TimeRange
) -> list[dict[str, Any]]:
    """Session counts by agent/profile identity (not a model ranking)."""
    sessions = _aggregate_sessions(conn, tr)
    metrics = _metric_rows(conn, sessions)
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for session in sessions:
        metric = metrics.get(session.metric_session_id)
        profile = metric["agent_profile"] if metric is not None else None
        if profile is not None and str(profile).strip():
            counts[(str(profile), session.logical_harness)] += 1
    collapsed = collapse_by_model(
        [
            {
                "agent_profile": profile,
                "harness": harness,
                "sessions": count,
            }
            for (profile, harness), count in counts.items()
        ],
        model_key="agent_profile",
    )
    total = sum(int(r["sessions"]) for r in collapsed) or 1
    return [
        {
            "agent_profile": r["agent_profile"],
            "harnesses": r["harnesses"],
            "sessions": r["sessions"],
            "share": r["sessions"] / total,
        }
        for r in collapsed
    ]


def activity_heatmap(conn: sqlite3.Connection, tr: TimeRange) -> dict[str, Any]:
    grid = [[0 for _ in range(24)] for _ in range(7)]
    for session in _aggregate_sessions(conn, tr):
        started_at = session.row["started_at"]
        if not started_at:
            continue
        try:
            dt = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
        except ValueError:
            continue
        # Monday=0 … Sunday=6
        weekday = dt.weekday()
        grid[weekday][dt.hour] += 1
    return {
        "weekdays": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
        "hours": list(range(24)),
        "counts": grid,
        "note": "Session start counts by weekday and hour (UTC). Descriptive usage only.",
    }


def top_projects(
    conn: sqlite3.Connection, tr: TimeRange, *, limit: int = 8
) -> list[dict[str, Any]]:
    rows = _aggregate_sessions(conn, tr)
    counts: dict[str, int] = defaultdict(int)
    for session in rows:
        label = _project_label(session.row["repo"], session.row["cwd"])
        counts[label] += 1
    ranked = sorted(counts.items(), key=lambda x: (-x[1], x[0]))[:limit]
    # Weekly buckets for sparklines (relative to range end).
    week_keys: list[str] = []
    if tr.end:
        for i in range(7, -1, -1):
            # Approximate week labels as ISO week of end-i*7 days
            from datetime import timedelta

            d = tr.end - timedelta(days=7 * i)
            week_keys.append(d.strftime("%G-W%V"))
    by_project_week: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for session in rows:
        row = session.row
        label = _project_label(row["repo"], row["cwd"])
        if label not in {p for p, _ in ranked}:
            continue
        if not row["started_at"]:
            continue
        try:
            dt = datetime.fromisoformat(str(row["started_at"]).replace("Z", "+00:00"))
        except ValueError:
            continue
        key = dt.strftime("%G-W%V")
        by_project_week[label][key] += 1
    out = []
    for label, n in ranked:
        spark = [by_project_week[label].get(k, 0) for k in week_keys]
        out.append({"project": label, "sessions": n, "sparkline": spark})
    return out


def recent_sessions(
    conn: sqlite3.Connection, tr: TimeRange, *, limit: int = 8
) -> list[dict[str, Any]]:
    sessions = _aggregate_sessions(conn, tr)
    metrics = _metric_rows(conn, sessions)
    metric_ids = sorted({session.metric_session_id for session in sessions})
    message_counts: dict[str, int] = {}
    tool_counts: dict[str, int] = {}
    if metric_ids:
        placeholders = ",".join("?" for _ in metric_ids)
        message_counts = {
            str(row["session_id"]): int(row["c"])
            for row in conn.execute(
                f"SELECT session_id, COUNT(*) AS c FROM messages "
                f"WHERE session_id IN ({placeholders}) GROUP BY session_id",
                metric_ids,
            ).fetchall()
        }
        tool_counts = {
            str(row["session_id"]): int(row["c"])
            for row in conn.execute(
                f"SELECT session_id, COUNT(*) AS c FROM tool_events "
                f"WHERE session_id IN ({placeholders}) GROUP BY session_id",
                metric_ids,
            ).fetchall()
        }
    sessions.sort(key=lambda session: str(session.row["started_at"] or ""), reverse=True)
    out = []
    for session in sessions[:limit]:
        r = session.row
        metric = metrics.get(session.metric_session_id)
        duration_s = None
        if r["started_at"] and r["ended_at"]:
            try:
                a = datetime.fromisoformat(r["started_at"].replace("Z", "+00:00"))
                b = datetime.fromisoformat(r["ended_at"].replace("Z", "+00:00"))
                duration_s = max(0, int((b - a).total_seconds()))
            except ValueError:
                duration_s = None
        out.append(
            {
                "id": r["id"],
                "harness": session.logical_harness,
                "runtime_harness": session.runtime_harness,
                "orchestrator_session_id": session.orchestrator_session_id,
                "model": display_model(metric["model_canonical"] if metric else None),
                "model_raw": None,
                "provider": None,
                "agent_profile": metric["agent_profile"] if metric else None,
                "effort": r["effort"],
                "project": _project_label(r["repo"], r["cwd"]),
                "started_at": r["started_at"],
                "ended_at": r["ended_at"],
                "duration_seconds": duration_s,
                "message_count": message_counts.get(session.metric_session_id, 0),
                "tool_count": tool_counts.get(session.metric_session_id, 0),
                # Tokens/cost unsupported in ledger — never invent.
                "tokens": None,
                "status": "observed",
            }
        )
    return out


def list_sessions(
    conn: sqlite3.Connection,
    tr: TimeRange,
    *,
    harness: list[str] | None = None,
    model: list[str] | None = None,
    project: list[str] | None = None,
    q: str | None = None,
    cursor: int = 0,
    limit: int = 50,
) -> dict[str, Any]:
    where, params = _session_time_clause(tr)
    clauses = [where]
    if harness:
        placeholders = ",".join(f":h{i}" for i in range(len(harness)))
        clauses.append(f"s.harness IN ({placeholders})")
        for i, h in enumerate(harness):
            params[f"h{i}"] = h
    if model:
        placeholders = ",".join(f":m{i}" for i in range(len(model)))
        clauses.append(
            f"{sql_coalesce_model('s.model_canonical')} IN ({placeholders})"
        )
        for i, m in enumerate(model):
            params[f"m{i}"] = m
    if q:
        clauses.append(
            """(
            s.id LIKE :q
            OR COALESCE(s.repo, '') LIKE :q
            OR COALESCE(s.cwd, '') LIKE :q
            OR COALESCE(s.model, '') LIKE :q
            OR COALESCE(s.model_canonical, '') LIKE :q
            OR EXISTS (
                SELECT 1 FROM messages m
                WHERE m.session_id = s.id AND m.role = 'user' AND m.text LIKE :q
                LIMIT 1
            )
        )"""
        )
        params["q"] = f"%{q}%"
    where_sql = " AND ".join(clauses)
    total = conn.execute(
        f"SELECT COUNT(*) AS c FROM sessions s WHERE {where_sql}",
        params,
    ).fetchone()
    params_page = {**params, "limit": limit, "offset": max(0, cursor)}
    rows = conn.execute(
        f"""
        SELECT
            s.id, s.harness, s.model_canonical, s.model AS model_raw,
            s.provider, s.agent_profile, s.effort, s.repo, s.cwd,
            s.started_at, s.ended_at,
            (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id) AS message_count,
            (SELECT COUNT(*) FROM tool_events t WHERE t.session_id = s.id) AS tool_count
        FROM sessions s
        WHERE {where_sql}
        ORDER BY COALESCE(s.started_at, '') DESC
        LIMIT :limit OFFSET :offset
        """,
        params_page,
    ).fetchall()
    items = []
    for r in rows:
        label = _project_label(r["repo"], r["cwd"])
        if project and label not in project:
            continue
        items.append(
            {
                "id": r["id"],
                "harness": r["harness"],
                "model": display_model(r["model_canonical"]),
                "model_raw": r["model_raw"],
                "provider": r["provider"],
                "agent_profile": r["agent_profile"],
                "effort": r["effort"],
                "project": label,
                "started_at": r["started_at"],
                "ended_at": r["ended_at"],
                "message_count": int(r["message_count"]),
                "tool_count": int(r["tool_count"]),
                "tokens": None,
                "status": "observed",
            }
        )
    return {
        "total": int(total["c"]) if total else 0,
        "cursor": cursor,
        "next_cursor": cursor + limit if total and cursor + limit < int(total["c"]) else None,
        "items": items,
    }


def session_detail(conn: sqlite3.Connection, session_id: str) -> dict[str, Any] | None:
    s = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if s is None:
        return None
    messages = conn.execute(
        """
        SELECT id, seq, role, timestamp, model, model_canonical, provider,
               agent_profile, effort, text, is_tool_plumbing, authored_by_agent
        FROM messages WHERE session_id = ? ORDER BY seq
        """,
        (session_id,),
    ).fetchall()
    tools = conn.execute(
        """
        SELECT id, message_id, seq, tool_name, action, success, duration_ms,
               operation_kind
        FROM tool_events WHERE session_id = ? ORDER BY seq
        """,
        (session_id,),
    ).fetchall()
    skills = conn.execute(
        """
        SELECT skill_name, exposure_type, COUNT(*) AS c
        FROM skill_exposures WHERE session_id = ?
        GROUP BY skill_name, exposure_type
        ORDER BY c DESC
        """,
        (session_id,),
    ).fetchall()
    return {
        "session": {
            "id": s["id"],
            "harness": s["harness"],
            "model": display_model(s["model_canonical"]),
            "model_raw": s["model"],
            "provider": s["provider"],
            "agent_profile": s["agent_profile"],
            "effort": s["effort"],
            "project": _project_label(s["repo"], s["cwd"]),
            "repo": s["repo"],
            "cwd": s["cwd"],
            "started_at": s["started_at"],
            "ended_at": s["ended_at"],
            "branch": s["branch"],
        },
        "messages": [
            {
                **{
                    k: v
                    for k, v in dict(m).items()
                    if k != "model_canonical"
                },
                "model": display_model(m["model_canonical"]),
                "model_raw": m["model"],
                "is_tool_plumbing": bool(m["is_tool_plumbing"]),
                "authored_by_agent": bool(m["authored_by_agent"]),
            }
            for m in messages
        ],
        "tool_events": [dict(t) for t in tools],
        "skills": [dict(sk) for sk in skills],
        "anatomy": {
            "message_count": len(messages),
            "tool_count": len(tools),
            "tokens": None,
            "cost_est": None,
            "note": (
                "Token and cost fields are not yet normalized in the ledger. "
                "Semantic correction/redirect markers require ux_observations."
            ),
        },
    }


def skills_summary(conn: sqlite3.Connection, tr: TimeRange) -> dict[str, Any]:
    where, params = _session_time_clause(tr)
    rows = conn.execute(
        f"""
        SELECT se.skill_name, COUNT(*) AS fires,
               COUNT(DISTINCT se.session_id) AS sessions,
               MAX(s.started_at) AS last_fired
        FROM skill_exposures se
        JOIN sessions s ON s.id = se.session_id
        WHERE {where}
        GROUP BY se.skill_name
        ORDER BY fires DESC
        """,
        params,
    ).fetchall()
    # Weekly fire counts per skill for hand-rolled sparklines (8 buckets
    # anchored to the range end, matching top_projects).
    week_keys: list[str] = []
    if tr.end:
        from datetime import timedelta

        for i in range(7, -1, -1):
            week_keys.append((tr.end - timedelta(days=7 * i)).strftime("%G-W%V"))
    fire_rows = conn.execute(
        f"""
        SELECT se.skill_name, s.started_at
        FROM skill_exposures se
        JOIN sessions s ON s.id = se.session_id
        WHERE {where} AND s.started_at IS NOT NULL
        """,
        params,
    ).fetchall()
    by_skill_week: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in fire_rows:
        try:
            dt = datetime.fromisoformat(r["started_at"].replace("Z", "+00:00"))
        except ValueError:
            continue
        by_skill_week[r["skill_name"]][dt.strftime("%G-W%V")] += 1
    items = [
        {
            "skill": r["skill_name"],
            "fires": int(r["fires"]),
            "sessions": int(r["sessions"]),
            "last_fired": r["last_fired"],
            "sparkline": [
                by_skill_week[r["skill_name"]].get(k, 0) for k in week_keys
            ],
            # Effectiveness contrasts abstain until semantic labels + precision gates.
            "interaction_style_with": unavailable_cell(
                metric="redirects_brakes_per_10_exchange_windows",
                kind="continuous",
                message=(
                    "Skill interaction-style contrasts require populated "
                    "ux_observations and a passing §4.7 precision gate. "
                    "No with/without rate is shown."
                ),
                flags=["source_capability", "small_sample"],
            ).to_dict(),
            "interaction_style_without": unavailable_cell(
                metric="redirects_brakes_per_10_exchange_windows",
                kind="continuous",
                message=(
                    "Skill interaction-style contrasts require populated "
                    "ux_observations and a passing §4.7 precision gate."
                ),
                flags=["source_capability", "small_sample"],
            ).to_dict(),
        }
        for r in rows
    ]
    return {
        "activations": sum(i["fires"] for i in items),
        "distinct_fired": len(items),
        "items": items,
        "note": (
            "Activation counts are descriptive. Effectiveness and correction "
            "contrasts are withheld until semantic extraction clears precision gates. "
            "Never treat activation count as a quality score."
        ),
    }


_INSIGHT_ACTIVE_CLAIM_STATUSES = frozenset({"approved", "published"})
_INSIGHT_FACT_KINDS = frozenset(
    {
        "recurring_instruction",
        "harness_model_usage",
        "correction_theme",
        "session_fact",
        "coach_observed_instance",
        "coach_corpus_pattern",
    }
)
_INSIGHT_SUPPORT_OK = frozenset({"ok"})
_INSIGHTS_GROUP_CAP = 20
_INSIGHT_DEMO_RUN_IDS = frozenset({"insights-session-demo"})
_INSIGHT_DEMO_PACKET_SUFFIX = ".research/insights-session-demo/facts.json"


def _insights_parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _insights_in_range(iso: str | None, tr: TimeRange) -> bool:
    if tr.start is None:
        return True
    dt = _insights_parse_ts(iso)
    if dt is None:
        return False
    return tr.start <= dt < tr.end


def _insights_clip(text: str | None, limit: int = 280) -> str:
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 1)] + "…"


def _insight_provenance(
    *,
    derivation: str | None,
    extractor: str | None,
    extractor_version: str | None,
    basis: dict[str, Any],
) -> dict[str, Any]:
    replay = basis.get("run_replay") if isinstance(basis.get("run_replay"), dict) else {}
    synthesis = basis.get("terra_synthesis_producer") or replay.get("terra_synthesis_producer") or {}
    review = basis.get("terra_review_producer") or replay.get("terra_review_producer") or {}
    catalog_id = str(basis.get("catalog_id") or "") or None
    review_id = str(basis.get("review_id") or replay.get("terra_review_id") or "") or None
    synthesis_bound = isinstance(synthesis, dict) and bool(synthesis.get("model"))
    review_bound = isinstance(review, dict) and bool(review.get("model")) and bool(review_id)
    source_packet_ids = basis.get("source_packet_ids") or [
        item.get("packet_id")
        for item in replay.get("terra_synthesis_results", [])
        if isinstance(item, dict) and item.get("packet_id")
    ]
    source_result_ids = basis.get("terra_synthesis_result_ids") or [
        item.get("result_id")
        for item in replay.get("terra_synthesis_results", [])
        if isinstance(item, dict) and item.get("result_id")
    ]
    return {
        "derivation": derivation or None,
        "extractor": extractor or None,
        "extractor_version": extractor_version or None,
        "run_id": (
            str(basis.get("run_id") or basis.get("catalog_id") or "") or None
        ),
        "model": str(synthesis.get("model") or basis.get("model") or "") or None,
        "source": "terra_synthesis" if synthesis_bound else str(basis.get("source") or basis.get("provider") or "") or None,
        "catalog_id": catalog_id,
        "review_id": review_id,
        "synthesis_model": str(synthesis.get("model") or "") or None,
        "synthesis_provider": str(synthesis.get("provider") or "") or None,
        "synthesis_worker_id": str(synthesis.get("worker_id") or "") or None,
        "review_model": str(review.get("model") or "") or None,
        "review_provider": str(review.get("provider") or "") or None,
        "review_worker_id": str(review.get("worker_id") or "") or None,
        "materializer_version": str(basis.get("materializer_version") or "") or None,
        "source_packet_ids": [str(value) for value in source_packet_ids if value],
        "source_result_ids": [str(value) for value in source_result_ids if value],
        "review_state": (
            "Terra synthesis and second review bound"
            if synthesis_bound and review_bound
            else "legacy or unverified provenance"
            if basis.get("provider") or basis.get("model")
            else "deterministic ledger derivation"
        ),
    }


def _insight_population(
    *,
    sample_size: int,
    denominator: int | None,
    insight_type: str,
    value: dict[str, Any],
    basis: dict[str, Any],
) -> tuple[int | None, str | None]:
    population = basis.get("eligible_population")
    if not isinstance(population, dict):
        population = value.get("eligible_population")
    if not isinstance(population, dict):
        population = {}

    population_n = population.get("root_cluster_count")
    if denominator is None and isinstance(population_n, int):
        denominator = population_n

    materialized_denominator = basis.get("full_eligible_root_denominator")
    if denominator is None and isinstance(materialized_denominator, int):
        denominator = materialized_denominator

    explicit = value.get("coverage") or basis.get("coverage")
    if isinstance(explicit, str) and explicit.strip():
        coverage = _insights_clip(explicit, 180)
    elif population:
        roots = population.get("root_cluster_count")
        harnesses = population.get("harness_count")
        harness_distribution = population.get("harness_distribution")
        if harnesses is None and isinstance(harness_distribution, dict):
            harnesses = len(harness_distribution)
        parts = []
        if isinstance(roots, int):
            parts.append(f"eligible population: {roots} root clusters")
        if isinstance(harnesses, int):
            parts.append(f"{harnesses} harnesses")
        coverage = "; ".join(parts) or None
    elif insight_type == "observed_instance":
        coverage = "one transcript instance; no corpus-pattern inference"
    elif denominator is not None:
        coverage = f"support n={sample_size} of denominator={denominator}"
    else:
        coverage = None
    return denominator, coverage


def _insight_int(*values: Any) -> int | None:
    for value in values:
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None


def _coverage_entries(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, dict):
        return []
    if "processed_roots" in value or "eligible_roots" in value:
        return [value]
    return [dict(item) for item in value.values() if isinstance(item, dict)]


def _calibrated_sampling_gate(basis: dict[str, Any]) -> str | None:
    gate = basis.get("calibrated_sampling_gate")
    if not isinstance(gate, dict) or gate.get("passed") is not True:
        return None
    method = str(gate.get("method") or "").strip()
    calibration_id = str(gate.get("calibration_id") or "").strip()
    validator_version = str(gate.get("validator_version") or "").strip()
    if not method or not calibration_id or not validator_version:
        return None
    return f"{method} ({calibration_id}; {validator_version})"


def _proof_capability_coverage(
    basis: dict[str, Any], value: dict[str, Any]
) -> tuple[dict[str, dict[str, Any]] | None, str | None, bool]:
    raw = basis.get("proof_capability_by_harness") or value.get(
        "proof_capability_by_harness"
    )
    if not isinstance(raw, dict) or not raw:
        return (
            None,
            "Per-harness terminal-evidence proof capability was not recorded; "
            "coverage is unknown and cannot be treated as complete.",
            True,
        )

    normalized: dict[str, dict[str, Any]] = {}
    impaired: list[str] = []
    for harness in sorted(raw):
        item = raw[harness]
        if isinstance(item, str):
            level = item
            details: dict[str, Any] = {}
        elif isinstance(item, dict):
            details = item
            eligible = _insight_int(details.get("eligible_roots"))
            proof_capable = _insight_int(details.get("proof_capable_roots"))
            if (
                eligible is not None
                and proof_capable is not None
                and proof_capable <= eligible
            ):
                if proof_capable == eligible:
                    level = "supported"
                elif proof_capable == 0:
                    level = "absent"
                else:
                    level = "partial"
            else:
                level = str(item.get("level") or item.get("status") or "unknown")
        else:
            level = "unknown"
            details = {}
        if level not in {"supported", "partial", "absent", "unknown"}:
            level = "unknown"
        eligible = _insight_int(details.get("eligible_roots"))
        processed = _insight_int(details.get("processed_roots"))
        proof_capable = _insight_int(details.get("proof_capable_roots"))
        raw_levels = details.get("levels")
        levels = (
            {
                str(name): count
                for name, raw_count in raw_levels.items()
                if (count := _insight_int(raw_count)) is not None
            }
            if isinstance(raw_levels, dict)
            else None
        )
        capability_complete = details.get("capability_complete")
        reported_capability = str(details.get("capability") or "").strip()
        normalized[str(harness)] = {
            "level": level,
            "processed_roots": processed,
            "eligible_roots": eligible,
            "proof_capable_roots": proof_capable,
            "levels": levels,
            "capability": reported_capability or None,
            "capability_complete": (
                capability_complete if isinstance(capability_complete, bool) else None
            ),
        }
        if level != "supported":
            counts = (
                f" {proof_capable}/{eligible} proof-capable roots"
                if proof_capable is not None and eligible is not None
                else ""
            )
            impaired.append(f"{harness}={level}{counts}")
    if not impaired:
        return normalized, None, False
    caveat = (
        "Terminal-evidence proof capability is not fully supported for "
        + ", ".join(impaired)
        + "; those roots remain in the eligible denominator and reduce coverage."
    )
    return normalized, caveat, True


def _coach_coverage(
    *,
    sample_size: int,
    denominator: int | None,
    value: dict[str, Any],
    basis: dict[str, Any],
) -> dict[str, Any] | None:
    raw_coverage = basis.get("coverage") or value.get("coverage")
    entries = _coverage_entries(raw_coverage)

    processed = _insight_int(basis.get("processed_roots"), value.get("processed_roots"))
    eligible = _insight_int(
        basis.get("eligible_roots"),
        value.get("eligible_roots"),
        basis.get("full_eligible_root_denominator"),
        value.get("full_eligible_root_denominator"),
        denominator,
    )
    selected = _insight_int(basis.get("selected_roots"), value.get("selected_roots"))

    pairs = {
        (entry.get("processed_roots"), entry.get("eligible_roots"))
        for entry in entries
        if _insight_int(entry.get("processed_roots")) is not None
        and _insight_int(entry.get("eligible_roots")) is not None
    }
    if processed is None and len(pairs) == 1:
        processed = _insight_int(next(iter(pairs))[0])
    if eligible is None and len(pairs) == 1:
        eligible = _insight_int(next(iter(pairs))[1])
    if selected is None:
        selected_values = {
            int(entry["selected_roots"])
            for entry in entries
            if _insight_int(entry.get("selected_roots")) is not None
        }
        if len(selected_values) == 1:
            selected = next(iter(selected_values))

    if (
        processed is None
        or eligible is None
        or eligible < 1
        or sample_size > processed
        or processed > eligible
        or (denominator is not None and denominator != eligible)
    ):
        return None

    processing_state = "complete" if processed == eligible else "partial"
    declared_state = str(
        basis.get("coverage_state") or value.get("coverage_state") or ""
    ).strip()
    if declared_state and declared_state != processing_state:
        return None
    proof_capability, proof_caveat, proof_impaired = _proof_capability_coverage(
        basis, value
    )
    state = "partial" if proof_impaired else processing_state
    selection_method = str(
        basis.get("selection_method")
        or value.get("selection_method")
        or next(
            (
                entry.get("selection_method") or entry.get("selection")
                for entry in entries
                if entry.get("selection_method") or entry.get("selection")
            ),
            "",
        )
        or ("score_then_temporal_strata" if basis.get("provider") == "coach_pipeline" else "")
    ).strip() or None
    selection_caveat = str(
        basis.get("selection_caveat") or value.get("selection_caveat") or ""
    ).strip()
    if not selection_caveat:
        if processing_state == "partial":
            selection_caveat = (
                f"Only {processed} of {eligible} eligible root clusters were processed. "
                "The support count describes the sampled run, not corpus prevalence "
                "or recurrence."
            )
        else:
            selection_caveat = (
                "All eligible root clusters were processed, but within-root evidence "
                "selection remains descriptive rather than causal."
            )
    label = (
        f"{sample_size} supporting / {processed} processed / {eligible} eligible "
        f"root clusters; coverage={state}; processing={processing_state}"
    )
    if selected is not None:
        label = f"{label}; {selected} selected"
    return {
        "supporting_roots": sample_size,
        "processed_roots": processed,
        "eligible_roots": eligible,
        "coverage_state": state,
        "processing_coverage_state": processing_state,
        "coverage": label,
        "selection_method": selection_method,
        "selection_caveat": selection_caveat,
        "sampling_gate": _calibrated_sampling_gate(basis),
        "proof_capability_by_harness": proof_capability,
        "proof_capability_caveat": proof_caveat,
    }


def _is_demo_insight_claim(claim: Any) -> bool:
    if getattr(claim, "kind", None) != "session_fact":
        return False
    basis = dict(getattr(claim, "confidence_basis", None) or {})
    if str(basis.get("run_id") or "") in _INSIGHT_DEMO_RUN_IDS:
        return True
    for key in ("source_path", "packet_path", "artifact_path", "path"):
        path = str(basis.get(key) or "").replace("\\", "/")
        if path.endswith(_INSIGHT_DEMO_PACKET_SUFFIX):
            return True
    return False


def _claim_insight_card(claim: Any) -> dict[str, Any] | None:
    if claim.status not in _INSIGHT_ACTIVE_CLAIM_STATUSES:
        return None
    if claim.kind not in _INSIGHT_FACT_KINDS:
        return None
    if claim.support_status not in _INSIGHT_SUPPORT_OK:
        return None
    if _is_demo_insight_claim(claim):
        return None

    value = dict(claim.value or {})
    basis = dict(claim.confidence_basis or {})
    phrasing = value.get("summary") or value.get("phrasing") or ""
    body = _insights_clip(phrasing) or _insights_clip(
        f"{claim.kind}: {claim.subject} ({claim.predicate})"
    )

    if claim.kind in {"session_fact", "coach_observed_instance"}:
        card_kind = "fact"
        insight_type = "observed_instance"
        title = str(value.get("title") or "Observed instance")
        dnp = claim.does_not_prove or "A single session does not establish a pattern."
        suggested = None
        canonical = value.get("canonical")
        canonical = dict(canonical) if isinstance(canonical, dict) else {}
        theme = value.get("theme") or canonical.get("predicate") or claim.subject
    elif claim.kind == "coach_corpus_pattern":
        card_kind = "fact"
        insight_type = "corpus_pattern"
        title = str(value.get("title") or "Corpus pattern")
        dnp = claim.does_not_prove or "An observed pattern does not establish causality."
        suggested = None
        canonical = value.get("canonical")
        canonical = dict(canonical) if isinstance(canonical, dict) else {}
        theme = canonical.get("predicate") or claim.subject
    elif claim.kind == "harness_model_usage":
        card_kind = "usage"
        insight_type = "corpus_pattern"
        title = f"Model mix · {claim.subject}"
        dnp = claim.does_not_prove or ""
        suggested = None
        theme = None
    elif claim.kind == "correction_theme":
        card_kind = "fact"
        insight_type = "corpus_pattern"
        title = "Correction label rate"
        dnp = claim.does_not_prove or (
            "Correction labels are descriptive frequency, not a quality score "
            "and not a config proposal."
        )
        suggested = None
        theme = None
    else:
        card_kind = "fact"
        insight_type = "corpus_pattern"
        title = f"Instruction theme · {claim.subject}"
        dnp = claim.does_not_prove or ""
        suggested = value.get("suggested_instruction") or None
        theme = value.get("theme") or claim.subject

    evidence = list(getattr(claim, "evidence", None) or [])
    session_id = None
    message_id = None
    for ev in evidence:
        if getattr(ev, "session_id", None):
            session_id = ev.session_id
            message_id = getattr(ev, "message_id", None)
            break

    href = f"/sessions/{quote(session_id, safe='')}" if session_id else None
    if href and message_id:
        href = f"{href}?msg={quote(str(message_id), safe='')}"
    denominator, coverage = _insight_population(
        sample_size=int(claim.sample_size or 0),
        denominator=claim.denominator,
        insight_type=insight_type,
        value=value,
        basis=basis,
    )
    coverage_fields: dict[str, Any] = {
        "supporting_roots": None,
        "processed_roots": None,
        "eligible_roots": None,
        "coverage_state": None,
        "processing_coverage_state": None,
        "selection_method": None,
        "selection_caveat": None,
        "sampling_gate": None,
        "proof_capability_by_harness": None,
        "proof_capability_caveat": None,
    }
    if claim.kind in {"coach_observed_instance", "coach_corpus_pattern"}:
        coach_coverage = _coach_coverage(
            sample_size=int(claim.sample_size or 0),
            denominator=denominator,
            value=value,
            basis=basis,
        )
        if coach_coverage is None:
            return None
        coverage_fields = coach_coverage
        coverage = str(coach_coverage["coverage"])
        if coach_coverage["processing_coverage_state"] == "partial":
            title = f"Sampled run · {title}"
            body = _insights_clip(
                "Sampled-run finding — "
                f"{coach_coverage['supporting_roots']} supporting among "
                f"{coach_coverage['processed_roots']} processed of "
                f"{coach_coverage['eligible_roots']} eligible root clusters. {body}",
                420,
            )
        elif coach_coverage["proof_capability_caveat"]:
            title = f"Evidence-limited · {title}"
            body = _insights_clip(
                "Evidence-limited finding — all eligible root clusters were "
                f"processed, but terminal-proof capability was incomplete. {body}",
                420,
            )

    return {
        "id": f"claim:{claim.id}",
        "kind": card_kind,
        "title": title,
        "body": body,
        "confidence": claim.support_status,
        "sample_size": int(claim.sample_size or 0),
        "denominator": denominator,
        "coverage": coverage,
        **coverage_fields,
        "does_not_prove": dnp,
        "theme": theme,
        "source": "claim",
        "source_id": claim.id,
        "origin": "session" if insight_type == "observed_instance" else "corpus",
        "insight_type": insight_type,
        "review_state": claim.status,
        "provenance": _insight_provenance(
            derivation=claim.derivation,
            extractor=claim.extractor_name,
            extractor_version=claim.extractor_version,
            basis=basis,
        ),
        "evidence_count": len(evidence),
        "suggested_instruction": suggested,
        "href": href,
    }


def _is_llm_coach_proposal(proposal: Any) -> bool:
    if proposal.status != "pending":
        return False
    if proposal.action == "archive_skill":
        return False
    if proposal.model or proposal.run_id:
        return True
    for claim in getattr(proposal, "claims", None) or []:
        if getattr(claim, "kind", None) == "llm_instruction_proposal":
            return True
    summary = (proposal.derivation_summary or "").lower()
    return "llm" in summary or "instruction_proposal" in summary


def _coalesce_pending_coach_proposals(proposals: list[Any]) -> list[Any]:
    chosen: dict[str, Any] = {}
    without_identity: list[Any] = []
    for proposal in proposals:
        source = dict(getattr(proposal, "provenance", None) or {})
        identity = source.get("semantic_identity") or source.get("intent_key")
        if not identity:
            for claim in getattr(proposal, "claims", None) or []:
                value = dict(getattr(claim, "value", None) or {})
                identity = value.get("semantic_identity") or value.get("intent_key")
                if identity:
                    break
        if not identity:
            without_identity.append(proposal)
            continue
        key = str(identity)
        current = chosen.get(key)
        if current is None or (
            str(getattr(proposal, "updated_at", "")),
            str(getattr(proposal, "created_at", "")),
            str(getattr(proposal, "id", "")),
        ) > (
            str(getattr(current, "updated_at", "")),
            str(getattr(current, "created_at", "")),
            str(getattr(current, "id", "")),
        ):
            chosen[key] = proposal
    return [*without_identity, *chosen.values()]


def _proposal_insight_card(proposal: Any) -> dict[str, Any] | None:
    if not _is_llm_coach_proposal(proposal):
        return None
    body = _insights_clip(proposal.rationale)
    suggested = None
    theme = None
    confidence = "insufficient"
    denominator = None
    evidence_count = 0
    for claim in getattr(proposal, "claims", None) or []:
        value = dict(getattr(claim, "value", None) or {})
        if not suggested and value.get("suggested_instruction"):
            suggested = str(value["suggested_instruction"])
        if theme is None and (
            value.get("theme")
            or getattr(claim, "kind", None)
            in {"llm_instruction_proposal", "recurring_instruction"}
        ):
            theme = value.get("theme") or getattr(claim, "subject", None)
        support = getattr(claim, "support_status", None)
        evidence_count += len(getattr(claim, "evidence", None) or [])
        if denominator is None and getattr(claim, "denominator", None) is not None:
            denominator = int(claim.denominator)
        if support == "ok":
            confidence = "ok"
        elif support == "insufficient" and confidence != "ok":
            confidence = "insufficient"

    provenance = dict(proposal.provenance or {})
    replay = provenance.get("run_replay") if isinstance(provenance.get("run_replay"), dict) else {}
    synthesis = provenance.get("terra_synthesis_producer") or replay.get("terra_synthesis_producer") or {}
    review = provenance.get("terra_review_producer") or replay.get("terra_review_producer") or {}
    verified_lineage = bool(
        provenance.get("catalog_id")
        and (provenance.get("review_id") or replay.get("terra_review_id"))
        and provenance.get("materializer_version")
        and isinstance(synthesis, dict)
        and synthesis.get("model")
        and isinstance(review, dict)
        and review.get("model")
    )
    denominator, coverage = _insight_population(
        sample_size=int(proposal.sample_size or 0),
        denominator=denominator,
        insight_type="coach_proposal",
        value={},
        basis=provenance,
    )
    coach_coverage = _coach_coverage(
        sample_size=int(proposal.sample_size or 0),
        denominator=denominator,
        value={},
        basis=provenance,
    )
    if coach_coverage is None:
        return None
    if coach_coverage["proof_capability_caveat"] is not None:
        return None
    if (
        coach_coverage["coverage_state"] != "complete"
        and not coach_coverage["sampling_gate"]
    ):
        return None
    coverage = str(coach_coverage["coverage"])
    if coach_coverage["coverage_state"] == "partial":
        title = f"Calibrated sample · {proposal.title or 'Harness suggestion'}"
        body = _insights_clip(
            "Calibrated sampled-run proposal — "
            f"{coach_coverage['supporting_roots']} supporting among "
            f"{coach_coverage['processed_roots']} processed of "
            f"{coach_coverage['eligible_roots']} eligible root clusters. {body}",
            420,
        )

    return {
        "id": f"proposal:{proposal.id}",
        "kind": "coach",
        "title": (
            title
            if coach_coverage["coverage_state"] == "partial"
            else proposal.title or "Harness suggestion"
        ),
        "body": body or "Pending coach suggestion — review on the proposals board.",
        "confidence": confidence,
        "sample_size": int(proposal.sample_size or 0),
        "denominator": denominator,
        "coverage": coverage,
        **coach_coverage,
        "does_not_prove": proposal.does_not_prove
        or (
            "A packet-validated proposal is a review candidate, not proof that "
            "the edit would improve outcomes."
            if verified_lineage
            else "This legacy proposal has unverified model/review lineage; it "
            "is not proof that the edit would improve outcomes."
        ),
        "theme": theme,
        "source": "proposal",
        "source_id": proposal.id,
        "origin": "proposal",
        "insight_type": "coach_proposal",
        "review_state": proposal.status,
        "provenance": _insight_provenance(
            derivation="llm_derived",
            extractor="proposal_packets",
            extractor_version=None,
            basis={
                **provenance,
                "run_id": proposal.run_id or provenance.get("run_id"),
                "model": proposal.model or provenance.get("model"),
            },
        ),
        "evidence_count": evidence_count,
        "suggested_instruction": suggested,
        "href": "/proposals",
    }


def insights_feed(conn: sqlite3.Connection, tr: TimeRange) -> dict[str, Any]:
    """Fact + coach cards from live claims and pending LLM proposals.

    Honest insights surface: no causal rankings, no unused-skill spam, no sentiment.
    """
    from agentlog.analysis.claims import list_claims, list_proposals

    ux_n = count_ux_observations(conn, tr)

    claims = list_claims(conn, status=None, include_evidence=True, limit=200)
    proposals = _coalesce_pending_coach_proposals(
        list_proposals(conn, status="pending", include_claims=True, limit=100)
    )

    claim_cards: list[dict[str, Any]] = []
    for claim in claims:
        if claim.kind not in _INSIGHT_FACT_KINDS:
            continue
        if claim.status not in _INSIGHT_ACTIVE_CLAIM_STATUSES:
            continue
        if claim.support_status not in _INSIGHT_SUPPORT_OK:
            continue
        if not _insights_in_range(claim.observed_at, tr):
            continue
        card = _claim_insight_card(claim)
        if card:
            claim_cards.append(card)

    coach_cards: list[dict[str, Any]] = []
    for proposal in proposals:
        if not _insights_in_range(proposal.created_at, tr):
            continue
        card = _proposal_insight_card(proposal)
        if card:
            coach_cards.append(card)

    def _fact_sort_key(card: dict[str, Any]) -> tuple[int, int, str]:
        conf = str(card.get("confidence") or "")
        conf_rank = 0 if conf == "ok" else 1
        return (conf_rank, -int(card.get("sample_size") or 0), str(card.get("id")))

    claim_cards.sort(key=_fact_sort_key)
    session_cards = [card for card in claim_cards if card["origin"] == "session"]
    corpus_cards = [card for card in claim_cards if card["origin"] != "session"]
    other_cards = [*coach_cards, *corpus_cards]
    items = [
        *session_cards[:_INSIGHTS_GROUP_CAP],
        *other_cards[:_INSIGHTS_GROUP_CAP],
    ]

    empty = {
        "title": "No evidence-backed insights in range",
        "body": (
            "No approved observations, published corpus patterns, or pending coach "
            "proposals fall inside this range. Insights appear only after "
            f"evidence-backed review. UX observations in range: {ux_n}."
        ),
        "missing": ["observed instances", "corpus patterns", "coach proposals"],
    }
    return {"items": items, "empty": empty}


def models_profile(conn: sqlite3.Connection, tr: TimeRange) -> dict[str, Any]:
    mix = model_mix(conn, tr)
    profiles = agent_profile_mix(conn, tr)
    # Interaction-style cells per model — abstain without semantic data.
    ux_n = count_ux_observations(conn, tr)
    cells = []
    for row in mix[:20]:
        if row["model"] == UNKNOWN_MODEL_LABEL:
            cell = unavailable_cell(
                metric="redirects_brakes_per_10_exchange_windows",
                kind="continuous",
                message=(
                    "Model-conditioned aggregates abstain when the model is "
                    "unknown."
                ),
                flags=["source_capability", "structural_nestedness"],
            )
        elif ux_n == 0:
            cell = unavailable_cell(
                metric="redirects_brakes_per_10_exchange_windows",
                kind="continuous",
                message=(
                    "Redirect/brake interaction-style rates require ux_observations. "
                    "The extraction table is empty pending the hand-labeling audit. "
                    "Usage share below is descriptive only — not a quality ranking."
                ),
                flags=["source_capability"],
            )
        else:
            # Path for when data exists: still must pass gates (tested separately).
            cell = _model_redirect_cell(conn, tr, row["model"])
        cells.append(
            {
                "model": row["model"],
                "harnesses": row["harnesses"],
                "messages": row["messages"],
                "sessions": row["sessions"],
                "share": row["share"],
                "interaction_style": cell.to_dict(),
                "flags": _model_confounder_flags(row),
            }
        )
    return {
        "title": "Model usage and interaction-style profile",
        "subtitle": (
            "Shares describe assistant-message model attribution. "
            "Interaction-style rates appear only when precision gates pass. "
            "This is not a quality ranking."
        ),
        "cost": {
            "status": "unavailable",
            "message": (
                "Token and cost fields are not yet normalized in the ledger. "
                "No estimated spend is shown."
            ),
        },
        "items": cells,
        "unknown": unknown_model_detail(conn, tr),
        "unknown_note": (
            "(unknown) is a declared category, not a fallback bucket: every "
            "assistant message in it has a stated reason its model could not be "
            "resolved."
        ),
        "profiles": profiles,
        "profiles_note": (
            "Agent/profile identities (e.g. codex-auto-review, grok-4.5-build) "
            "are counted here — not in the model mix."
        ),
    }


def _model_confounder_flags(row: dict[str, Any]) -> list[str]:
    flags: list[str] = []
    harnesses = {h["harness"] for h in row.get("harnesses", [])}
    if len(harnesses) == 1:
        flags.append("harness_model_aliasing")
        flags.append("structural_nestedness")
    if row["messages"] < 30:
        flags.append("small_sample")
    return flags


def _model_redirect_cell(
    conn: sqlite3.Connection, tr: TimeRange, model: str
) -> AggregateCell:
    """Model-conditioned redirect/brake cell. Descendant windows roll up to roots."""
    return redirect_cell(
        conn, tr, model=model, extra_flags=["structural_nestedness"]
    )


def semantic_lead_metric(conn: sqlite3.Connection, tr: TimeRange) -> AggregateCell:
    """Overview lead interaction-style metric — abstains without published evidence."""
    return redirect_cell(conn, tr)


def binary_cell_for_tests(
    successes: int,
    n: int,
    session_ids: list[str] | None = None,
) -> AggregateCell:
    """Thin wrapper used by API tests for under-powered cells."""
    return evaluate_binary_rate(
        metric="had_redirect_brake",
        successes=successes,
        n_clusters=n,
        session_ids=session_ids or [f"s{i}" for i in range(n)],
        availability=1.0,
    )
