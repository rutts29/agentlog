from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from agentlog.analysis.performance.gates import (
    AggregateCell,
    evaluate_binary_rate,
    evaluate_continuous_rate,
    unavailable_cell,
)
from agentlog.api.ranges import TimeRange


def _session_time_clause(
    tr: TimeRange,
    *,
    start_param: str = "start",
    end_param: str = "end",
    alias: str = "s",
) -> tuple[str, dict[str, Any]]:
    parts = [f"COALESCE({alias}.started_at, '') < :{end_param}"]
    params: dict[str, Any] = {end_param: tr.end_iso}
    if tr.start is not None:
        parts.append(f"COALESCE({alias}.started_at, '') >= :{start_param}")
        params[start_param] = tr.start_iso
    return " AND ".join(parts), params


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
    where, params = _session_time_clause(tr)
    row = conn.execute(
        f"SELECT COUNT(*) AS c FROM sessions s WHERE {where}",
        params,
    ).fetchone()
    return int(row["c"]) if row else 0


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
    where, params = _session_time_clause(tr)
    rows = conn.execute(
        f"""
        SELECT DISTINCT substr(started_at, 1, 10) AS day
        FROM sessions s
        WHERE started_at IS NOT NULL AND {where}
        ORDER BY day DESC
        """,
        params,
    ).fetchall()
    days = [r["day"] for r in rows if r["day"]]
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
    where, params = _session_time_clause(tr)
    rows = conn.execute(
        f"""
        SELECT substr(started_at, 1, 10) AS day, harness, COUNT(*) AS sessions
        FROM sessions s
        WHERE started_at IS NOT NULL AND {where}
        GROUP BY day, harness
        ORDER BY day, harness
        """,
        params,
    ).fetchall()
    by_day: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    harnesses: set[str] = set()
    for r in rows:
        by_day[r["day"]][r["harness"]] = int(r["sessions"])
        harnesses.add(r["harness"])
    out: list[dict[str, Any]] = []
    for day in sorted(by_day):
        item: dict[str, Any] = {"day": day, "total": sum(by_day[day].values())}
        for h in sorted(harnesses):
            item[h] = by_day[day].get(h, 0)
        out.append(item)
    return out


def model_mix(conn: sqlite3.Connection, tr: TimeRange) -> list[dict[str, Any]]:
    """Descriptive model-selection shares — not a quality ranking."""
    where, params = _session_time_clause(tr)
    rows = conn.execute(
        f"""
        SELECT COALESCE(NULLIF(model, ''), '(unknown)') AS model,
               harness,
               COUNT(*) AS sessions
        FROM sessions s
        WHERE {where}
        GROUP BY model, harness
        ORDER BY sessions DESC
        """,
        params,
    ).fetchall()
    total = sum(int(r["sessions"]) for r in rows) or 1
    return [
        {
            "model": r["model"],
            "harness": r["harness"],
            "sessions": int(r["sessions"]),
            "share": int(r["sessions"]) / total,
        }
        for r in rows
    ]


def activity_heatmap(conn: sqlite3.Connection, tr: TimeRange) -> dict[str, Any]:
    where, params = _session_time_clause(tr)
    rows = conn.execute(
        f"""
        SELECT started_at
        FROM sessions s
        WHERE started_at IS NOT NULL AND {where}
        """,
        params,
    ).fetchall()
    grid = [[0 for _ in range(24)] for _ in range(7)]
    for r in rows:
        try:
            dt = datetime.fromisoformat(r["started_at"].replace("Z", "+00:00"))
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
    where, params = _session_time_clause(tr)
    rows = conn.execute(
        f"""
        SELECT id, repo, cwd, started_at
        FROM sessions s
        WHERE {where}
        ORDER BY COALESCE(started_at, '') DESC
        """,
        params,
    ).fetchall()
    counts: dict[str, int] = defaultdict(int)
    for r in rows:
        label = _project_label(r["repo"], r["cwd"])
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
    for r in rows:
        label = _project_label(r["repo"], r["cwd"])
        if label not in {p for p, _ in ranked}:
            continue
        if not r["started_at"]:
            continue
        try:
            dt = datetime.fromisoformat(r["started_at"].replace("Z", "+00:00"))
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
    where, params = _session_time_clause(tr)
    params = {**params, "limit": limit}
    rows = conn.execute(
        f"""
        SELECT
            s.id,
            s.harness,
            s.model,
            s.effort,
            s.repo,
            s.cwd,
            s.started_at,
            s.ended_at,
            (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id) AS message_count,
            (SELECT COUNT(*) FROM tool_events t WHERE t.session_id = s.id) AS tool_count
        FROM sessions s
        WHERE {where}
        ORDER BY COALESCE(s.started_at, '') DESC
        LIMIT :limit
        """,
        params,
    ).fetchall()
    out = []
    for r in rows:
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
                "harness": r["harness"],
                "model": r["model"] or "(unknown)",
                "effort": r["effort"],
                "project": _project_label(r["repo"], r["cwd"]),
                "started_at": r["started_at"],
                "ended_at": r["ended_at"],
                "duration_seconds": duration_s,
                "message_count": int(r["message_count"]),
                "tool_count": int(r["tool_count"]),
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
        clauses.append(f"COALESCE(s.model, '(unknown)') IN ({placeholders})")
        for i, m in enumerate(model):
            params[f"m{i}"] = m
    if q:
        clauses.append(
            """(
            s.id LIKE :q
            OR COALESCE(s.repo, '') LIKE :q
            OR COALESCE(s.cwd, '') LIKE :q
            OR COALESCE(s.model, '') LIKE :q
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
            s.id, s.harness, s.model, s.effort, s.repo, s.cwd,
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
                "model": r["model"] or "(unknown)",
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
        SELECT id, seq, role, timestamp, model, effort, text, is_tool_plumbing
        FROM messages WHERE session_id = ? ORDER BY seq
        """,
        (session_id,),
    ).fetchall()
    tools = conn.execute(
        """
        SELECT id, message_id, seq, tool_name, action, success, duration_ms
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
            "model": s["model"],
            "effort": s["effort"],
            "project": _project_label(s["repo"], s["cwd"]),
            "repo": s["repo"],
            "cwd": s["cwd"],
            "started_at": s["started_at"],
            "ended_at": s["ended_at"],
            "branch": s["branch"],
        },
        "messages": [dict(m) for m in messages],
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
    items = [
        {
            "skill": r["skill_name"],
            "fires": int(r["fires"]),
            "sessions": int(r["sessions"]),
            "last_fired": r["last_fired"],
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


def insights_feed(conn: sqlite3.Connection, tr: TimeRange) -> dict[str, Any]:
    ux_n = count_ux_observations(conn, tr)
    return {
        "items": [],
        "empty": {
            "title": "No derived claims yet",
            "body": (
                "Insights hold precomputed findings with confidence tags. "
                "They appear after semantic UX extraction populates ux_observations "
                f"(currently {ux_n} in range) and a claim clears its precision gate "
                "and confounder checks. Until then this feed stays empty rather than "
                "showing speculative patterns."
            ),
            "missing": ["ux_observations", "publish-qualified task labels"],
        },
    }


def models_profile(conn: sqlite3.Connection, tr: TimeRange) -> dict[str, Any]:
    mix = model_mix(conn, tr)
    # Interaction-style cells per model — abstain without semantic data.
    ux_n = count_ux_observations(conn, tr)
    cells = []
    for row in mix[:20]:
        if row["model"] in {"(unknown)", "<synthetic>"}:
            cell = unavailable_cell(
                metric="redirects_brakes_per_10_exchange_windows",
                kind="continuous",
                message=(
                    "Model-conditioned aggregates abstain when the model is "
                    "unknown or synthetic."
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
                "harness": row["harness"],
                "sessions": row["sessions"],
                "share": row["share"],
                "interaction_style": cell.to_dict(),
                "flags": _model_confounder_flags(row, mix),
            }
        )
    return {
        "title": "Model usage and interaction-style profile",
        "subtitle": (
            "Shares describe your model-selection pattern. "
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
    }


def _model_confounder_flags(
    row: dict[str, Any], mix: list[dict[str, Any]]
) -> list[str]:
    flags: list[str] = []
    model = row["model"]
    harnesses = {r["harness"] for r in mix if r["model"] == model}
    if len(harnesses) == 1:
        flags.append("harness_model_aliasing")
        flags.append("structural_nestedness")
    if row["sessions"] < 30:
        flags.append("small_sample")
    return flags


def _model_redirect_cell(
    conn: sqlite3.Connection, tr: TimeRange, model: str
) -> AggregateCell:
    """Build a redirect/brake cell when ux_observations exist (gate-enforced)."""
    where, params = _session_time_clause(tr)
    params = {**params, "model": model}
    rows = conn.execute(
        f"""
        SELECT
            COALESCE(s.parent_session_id, s.id) AS root_id,
            s.id AS session_id,
            u.flags_json
        FROM ux_observations u
        JOIN exchange_windows w ON w.id = u.window_id
        JOIN sessions s ON s.id = w.session_id
        WHERE {where}
          AND s.model = :model
          AND s.parent_session_id IS NULL
        """,
        params,
    ).fetchall()
    if not rows:
        return unavailable_cell(
            metric="redirects_brakes_per_10_exchange_windows",
            kind="continuous",
            message="No UX observations for this model in range.",
            flags=["source_capability"],
        )
    by_root: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_root[r["root_id"]].append(dict(r))
    rates: list[float] = []
    session_ids: list[str] = []
    for root_id, windows in by_root.items():
        n = len(windows)
        hits = 0
        for w in windows:
            try:
                flags = json.loads(w["flags_json"] or "{}")
            except json.JSONDecodeError:
                flags = {}
            if flags.get("redirect_brake") or flags.get("had_redirect_brake"):
                hits += 1
        rates.append((hits / n) * 10.0 if n else 0.0)
        session_ids.append(root_id)
    return evaluate_continuous_rate(
        metric="redirects_brakes_per_10_exchange_windows",
        per_cluster_values=rates,
        session_ids=session_ids,
        availability=1.0,
    )


def semantic_lead_metric(conn: sqlite3.Connection, tr: TimeRange) -> AggregateCell:
    """Overview lead interaction-style metric — abstains without UX data."""
    ux_n = count_ux_observations(conn, tr)
    if ux_n == 0:
        where, params = _session_time_clause(tr)
        ids = [
            r["id"]
            for r in conn.execute(
                f"""
                SELECT s.id FROM sessions s
                WHERE {where} AND s.parent_session_id IS NULL
                ORDER BY COALESCE(s.started_at, '') DESC
                LIMIT 40
                """,
                params,
            ).fetchall()
        ]
        return unavailable_cell(
            metric="redirects_brakes_per_10_exchange_windows",
            kind="continuous",
            message=(
                "Redirect/brake rate (descriptive steering frequency, not a quality "
                "score) requires ux_observations. That table is empty until the "
                "hand-labeling audit unlocks full extraction. No rate, sparkline, "
                "or delta is shown."
            ),
            flags=["source_capability"],
            session_ids=ids,
        )
    where, params = _session_time_clause(tr)
    rows = conn.execute(
        f"""
        SELECT
            COALESCE(s.parent_session_id, s.id) AS root_id,
            u.flags_json
        FROM ux_observations u
        JOIN exchange_windows w ON w.id = u.window_id
        JOIN sessions s ON s.id = w.session_id
        WHERE {where}
        """,
        params,
    ).fetchall()
    by_root: dict[str, list[Any]] = defaultdict(list)
    for r in rows:
        by_root[r["root_id"]].append(r["flags_json"])
    rates: list[float] = []
    session_ids: list[str] = []
    for root_id, flag_blobs in by_root.items():
        n = len(flag_blobs)
        hits = 0
        for blob in flag_blobs:
            try:
                flags = json.loads(blob or "{}")
            except json.JSONDecodeError:
                flags = {}
            if flags.get("redirect_brake") or flags.get("had_redirect_brake"):
                hits += 1
        rates.append((hits / n) * 10.0 if n else 0.0)
        session_ids.append(root_id)
    return evaluate_continuous_rate(
        metric="redirects_brakes_per_10_exchange_windows",
        per_cluster_values=rates,
        session_ids=session_ids,
        availability=1.0,
    )


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
