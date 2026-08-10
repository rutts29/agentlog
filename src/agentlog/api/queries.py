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
    """Descriptive assistant-message model shares — not a quality ranking.

    One row per model identity. The harness split rides along as a
    breakdown so the rendered label stays the grouping key.
    """
    where, params = _session_time_clause(tr)
    model_expr = strict_message_model_sql()
    rows = conn.execute(
        f"""
        SELECT {model_expr} AS model,
               s.harness AS harness,
               COUNT(*) AS messages,
               COUNT(DISTINCT s.id) AS sessions
        FROM sessions s
        JOIN messages m ON m.session_id = s.id
        WHERE {where}
          AND m.role = 'assistant'
        GROUP BY {model_expr}, harness
        ORDER BY messages DESC
        """,
        params,
    ).fetchall()
    raw_rows = [dict(r) for r in rows]
    collapsed = collapse_by_model(raw_rows, count_key="messages")
    session_rows = {
        row["model"]: row
        for row in collapse_by_model(raw_rows, count_key="sessions")
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
    where, params = _session_time_clause(tr)
    rows = conn.execute(
        f"""
        SELECT m.model AS model_raw,
               COUNT(*) AS messages,
               COUNT(DISTINCT s.id) AS sessions
        FROM sessions s
        JOIN messages m ON m.session_id = s.id
        WHERE {where}
          AND m.role = 'assistant'
          AND {strict_message_model_sql()} = :unknown_model
        GROUP BY m.model
        """,
        {**params, "unknown_model": UNKNOWN_MODEL_LABEL},
    ).fetchall()
    raw_rows = [dict(r) for r in rows]
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
    where, params = _session_time_clause(tr)
    rows = conn.execute(
        f"""
        SELECT COALESCE(NULLIF(s.agent_profile, ''), '(none)') AS agent_profile,
               s.harness AS harness,
               COUNT(*) AS sessions
        FROM sessions s
        WHERE {where}
          AND s.agent_profile IS NOT NULL
          AND TRIM(s.agent_profile) != ''
        GROUP BY agent_profile, harness
        ORDER BY sessions DESC
        """,
        params,
    ).fetchall()
    collapsed = collapse_by_model(
        [dict(r) for r in rows], model_key="agent_profile"
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
            s.model_canonical,
            s.model AS model_raw,
            s.provider,
            s.agent_profile,
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
                "model": display_model(r["model_canonical"]),
                "model_raw": r["model_raw"],
                "provider": r["provider"],
                "agent_profile": r["agent_profile"],
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


_INSIGHT_ACTIVE_CLAIM_STATUSES = frozenset({"candidate", "approved", "published"})
_INSIGHT_FACT_KINDS = frozenset(
    {
        "recurring_instruction",
        "harness_model_usage",
        "correction_theme",
        "session_fact",
    }
)
_INSIGHT_SUPPORT_OK = frozenset({"ok", "insufficient"})
_INSIGHTS_GROUP_CAP = 20


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


def _insights_in_range(iso: str | None, tr: TimeRange) -> tuple[bool, bool]:
    """Return (include, noted_unscoped). Prefer range filter when timestamps parse."""
    if tr.start is None:
        return True, False
    dt = _insights_parse_ts(iso)
    if dt is None:
        return True, True
    return tr.start <= dt <= tr.end, False


def _insights_clip(text: str | None, limit: int = 280) -> str:
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 1)] + "…"


def _claim_insight_card(
    claim: Any, *, range_note: bool = False
) -> dict[str, Any] | None:
    if claim.status not in _INSIGHT_ACTIVE_CLAIM_STATUSES:
        return None
    if claim.kind not in _INSIGHT_FACT_KINDS:
        return None
    if claim.kind != "correction_theme" and claim.support_status not in _INSIGHT_SUPPORT_OK:
        return None

    value = dict(claim.value or {})
    phrasing = value.get("phrasing") or ""
    body = _insights_clip(phrasing) or _insights_clip(
        f"{claim.kind}: {claim.subject} ({claim.predicate})"
    )
    if range_note:
        body = (
            f"{body} Observed across the full corpus (claim timestamp "
            "outside the selected range filter)."
            if body
            else "Observed across the full corpus (outside selected range)."
        )

    if claim.kind == "session_fact":
        card_kind = "fact"
        title = str(value.get("title") or "Session fact")
        dnp = claim.does_not_prove or "A single session does not establish a pattern."
        suggested = None
        theme = value.get("theme") or claim.subject
    elif claim.kind == "harness_model_usage":
        card_kind = "usage"
        title = f"Model mix · {claim.subject}"
        dnp = claim.does_not_prove or ""
        suggested = None
        theme = None
    elif claim.kind == "correction_theme":
        card_kind = "fact"
        title = "Correction label rate"
        dnp = claim.does_not_prove or (
            "Correction labels are descriptive frequency, not a quality score "
            "and not a config proposal."
        )
        suggested = None
        theme = None
    else:
        card_kind = "fact"
        title = f"Instruction theme · {claim.subject}"
        dnp = claim.does_not_prove or ""
        suggested = value.get("suggested_instruction") or None
        theme = value.get("theme") or claim.subject

    session_id = None
    for ev in getattr(claim, "evidence", None) or []:
        if getattr(ev, "session_id", None):
            session_id = ev.session_id
            break

    href = f"/sessions/{quote(session_id, safe='')}" if session_id else None

    return {
        "id": f"claim:{claim.id}",
        "kind": card_kind,
        "title": title,
        "body": body,
        "confidence": claim.support_status,
        "sample_size": int(claim.sample_size or 0),
        "does_not_prove": dnp,
        "theme": theme,
        "source": "claim",
        "source_id": claim.id,
        "origin": "session" if claim.kind == "session_fact" else "corpus",
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


def _proposal_insight_card(
    proposal: Any, *, range_note: bool = False
) -> dict[str, Any] | None:
    if not _is_llm_coach_proposal(proposal):
        return None
    body = _insights_clip(proposal.rationale)
    if range_note and body:
        body = (
            f"{body} (proposal timestamp outside selected range; "
            "shown corpus-wide.)"
        )
    elif range_note:
        body = "Coach suggestion (outside selected range; shown corpus-wide)."

    suggested = None
    theme = None
    confidence = "insufficient"
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
        if support == "ok":
            confidence = "ok"
        elif support == "insufficient" and confidence != "ok":
            confidence = "insufficient"

    return {
        "id": f"proposal:{proposal.id}",
        "kind": "coach",
        "title": proposal.title or "Harness suggestion",
        "body": body or "Pending coach suggestion — review on the proposals board.",
        "confidence": confidence,
        "sample_size": int(proposal.sample_size or 0),
        "does_not_prove": proposal.does_not_prove
        or (
            "A pending LLM proposal is a review candidate, not proof that the "
            "edit would improve outcomes."
        ),
        "theme": theme,
        "source": "proposal",
        "source_id": proposal.id,
        "origin": "proposal",
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
    proposals = list_proposals(conn, status="pending", include_claims=True, limit=100)

    claim_cards: list[dict[str, Any]] = []
    claim_cards_fallback: list[dict[str, Any]] = []
    for claim in claims:
        if claim.kind not in _INSIGHT_FACT_KINDS:
            continue
        if claim.kind == "correction_theme":
            if claim.status not in _INSIGHT_ACTIVE_CLAIM_STATUSES:
                continue
        elif claim.support_status not in _INSIGHT_SUPPORT_OK:
            continue
        elif claim.status not in _INSIGHT_ACTIVE_CLAIM_STATUSES:
            continue
        in_range, _unscoped = _insights_in_range(claim.observed_at, tr)
        if in_range:
            card = _claim_insight_card(claim, range_note=False)
            if card:
                claim_cards.append(card)
        else:
            card = _claim_insight_card(claim, range_note=True)
            if card:
                claim_cards_fallback.append(card)

    coach_cards: list[dict[str, Any]] = []
    coach_fallback: list[dict[str, Any]] = []
    for proposal in proposals:
        in_range, _unscoped = _insights_in_range(proposal.created_at, tr)
        if in_range:
            card = _proposal_insight_card(proposal, range_note=False)
            if card:
                coach_cards.append(card)
        else:
            card = _proposal_insight_card(proposal, range_note=True)
            if card:
                coach_fallback.append(card)

    # Prefer range-filtered cards; if that empties the feed, show corpus-wide
    # rather than inventing a filtered-zero empty state.
    if not coach_cards and not claim_cards:
        coach_cards = coach_fallback
        claim_cards = claim_cards_fallback

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
        "title": "No derived claims yet",
        "body": (
            "Insights hold factual claim cards and pending coach suggestions. "
            "They appear after claim extraction writes recurring-instruction / "
            "usage / correction facts, or after an LLM proposal run leaves "
            f"pending review items. UX observations in range: {ux_n}."
        ),
        "missing": ["active claims", "pending LLM proposals"],
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
