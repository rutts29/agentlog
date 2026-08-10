"""Token-usage aggregates. Counts only — never routed through abstention gates."""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from typing import Any
from urllib.parse import urlparse

from agentlog.api.identity_aggregates import (
    VisibleLogicalSession,
    visible_logical_sessions,
)
from agentlog.api.model_rollup import (
    GRAIN_DESCRIPTIONS,
    MESSAGE_MODEL,
    MESSAGE_MODEL_SQL,
    SESSION_START_MODEL,
    SESSION_START_MODEL_SQL,
    USAGE_MODEL_SQL,
)
from agentlog.api.ranges import TimeRange, session_time_clause as _session_time_clause
from agentlog.normalize.model_identity import display_model
from agentlog.pricing import estimate_cost, get_pricing
from agentlog.session_identity import build_identity_context, logical_projection

_USAGE_GROUP_BY = frozenset(
    {"harness", "model", "day", "repo", "agent_profile"}
)

# Rows used for additively comparable session totals:
# - Claude: per-message usage (sum)
# - Codex: final session_cumulative snapshot only (never sum cumulatives)
_ADDITIVE_GRANULARITIES = ("message",)
_SESSION_CUMULATIVE = "session_cumulative"


def _aggregate_sessions(
    conn: sqlite3.Connection, tr: TimeRange
) -> list[VisibleLogicalSession]:
    where, params = _session_time_clause(tr)
    rows = conn.execute(
        f"""
        SELECT s.id, s.harness, s.started_at
        FROM sessions s
        WHERE {where}
        """,
        params,
    ).fetchall()
    return visible_logical_sessions(conn, rows)


def _empty_totals() -> dict[str, Any]:
    return {
        "input_tokens": None,
        "output_tokens": None,
        "cache_creation_input_tokens": None,
        "cache_read_input_tokens": None,
        "cached_input_tokens": None,
        "cache_write_input_tokens": None,
        "reasoning_output_tokens": None,
        "total_tokens": None,
        "fields_present": {
            "input_tokens": False,
            "output_tokens": False,
            "cache_creation_input_tokens": False,
            "cache_read_input_tokens": False,
            "cached_input_tokens": False,
            "cache_write_input_tokens": False,
            "reasoning_output_tokens": False,
            "total_tokens": False,
        },
    }


def _sum_nullable(rows: list[sqlite3.Row], field: str) -> tuple[int | None, bool]:
    present = False
    total = 0
    for row in rows:
        value = row[field]
        if value is None:
            continue
        present = True
        total += int(value)
    return (total if present else None), present


def _totals_from_rows(rows: list[sqlite3.Row]) -> dict[str, Any]:
    out = _empty_totals()
    fields = (
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "cached_input_tokens",
        "cache_write_input_tokens",
        "reasoning_output_tokens",
        "total_tokens",
    )
    for field in fields:
        total, present = _sum_nullable(rows, field)
        out[field] = total
        out["fields_present"][field] = present
    # If some rows report input/output but omit total_tokens, a partial sum of
    # total_tokens would under-count — leave it null instead of a false total.
    if rows and out["fields_present"]["total_tokens"]:
        for row in rows:
            if row["total_tokens"] is None and (
                row["input_tokens"] is not None or row["output_tokens"] is not None
            ):
                out["total_tokens"] = None
                out["fields_present"]["total_tokens"] = False
                break
    return out


def _cache_ratios(totals: dict[str, Any]) -> dict[str, Any]:
    """Cache-hit ratios only where the harness fields support them."""
    out: dict[str, Any] = {
        "claude_cache_read_ratio": None,
        "codex_cached_input_ratio": None,
    }
    fp = totals["fields_present"]
    if fp["cache_read_input_tokens"] or fp["cache_creation_input_tokens"]:
        cache_read = totals["cache_read_input_tokens"] or 0
        cache_create = totals["cache_creation_input_tokens"] or 0
        input_tok = totals["input_tokens"] or 0
        denom = input_tok + cache_read + cache_create
        if denom > 0:
            out["claude_cache_read_ratio"] = cache_read / denom
    if fp["cached_input_tokens"] and fp["input_tokens"]:
        input_tok = totals["input_tokens"] or 0
        cached = totals["cached_input_tokens"] or 0
        if input_tok > 0:
            out["codex_cached_input_ratio"] = cached / input_tok
    return out


def _session_additive_rows(
    conn: sqlite3.Connection, tr: TimeRange
) -> list[dict[str, Any]]:
    """One additive contribution row per session with usage."""
    sessions = _aggregate_sessions(conn, tr)
    by_metric = {session.metric_session_id: session for session in sessions}
    metric_ids = sorted(by_metric)
    if not metric_ids:
        return []
    placeholders = ",".join("?" for _ in metric_ids)
    message_rows = list(
        conn.execute(
            f"""
            SELECT
                s.id AS session_id,
                {SESSION_START_MODEL_SQL} AS model,
                SUM(u.input_tokens) AS input_tokens,
                SUM(u.output_tokens) AS output_tokens,
                SUM(u.cache_creation_input_tokens) AS cache_creation_input_tokens,
                SUM(u.cache_read_input_tokens) AS cache_read_input_tokens,
                SUM(u.cached_input_tokens) AS cached_input_tokens,
                SUM(u.cache_write_input_tokens) AS cache_write_input_tokens,
                SUM(u.reasoning_output_tokens) AS reasoning_output_tokens,
                SUM(u.total_tokens) AS total_tokens,
                COUNT(*) AS usage_rows
            FROM token_usage u
            JOIN sessions s ON s.id = u.session_id
            WHERE u.session_id IN ({placeholders})
              AND u.granularity IN ('message')
            GROUP BY s.id
            """,
            metric_ids,
        )
    )
    cumulative_rows = list(
        conn.execute(
            f"""
            SELECT
                s.id AS session_id,
                {SESSION_START_MODEL_SQL} AS model,
                u.input_tokens AS input_tokens,
                u.output_tokens AS output_tokens,
                u.cache_creation_input_tokens AS cache_creation_input_tokens,
                u.cache_read_input_tokens AS cache_read_input_tokens,
                u.cached_input_tokens AS cached_input_tokens,
                u.cache_write_input_tokens AS cache_write_input_tokens,
                u.reasoning_output_tokens AS reasoning_output_tokens,
                u.total_tokens AS total_tokens,
                1 AS usage_rows
            FROM token_usage u
            JOIN sessions s ON s.id = u.session_id
            JOIN (
                SELECT u2.session_id AS session_id, MAX(u2.seq) AS max_seq
                FROM token_usage u2
                WHERE u2.session_id IN ({placeholders})
                  AND u2.granularity = 'session_cumulative'
                GROUP BY u2.session_id
            ) latest ON latest.session_id = u.session_id AND latest.max_seq = u.seq
            WHERE u.session_id IN ({placeholders})
              AND u.granularity = 'session_cumulative'
            """,
            [*metric_ids, *metric_ids],
        )
    )
    out: list[dict[str, Any]] = []
    for row in [*message_rows, *cumulative_rows]:
        session = by_metric[str(row["session_id"])]
        item = dict(row)
        item["session_id"] = session.session_id
        item["harness"] = session.logical_harness
        item["started_at"] = session.row["started_at"]
        out.append(item)
    return out


def coverage(conn: sqlite3.Connection, tr: TimeRange) -> dict[str, Any]:
    sessions = _aggregate_sessions(conn, tr)
    by_metric = {session.metric_session_id: session for session in sessions}
    metric_ids = sorted(by_metric)
    sessions_by: dict[str, int] = defaultdict(int)
    sessions_with_by: dict[str, int] = defaultdict(int)
    messages_by: dict[str, int] = defaultdict(int)
    messages_with_by: dict[str, int] = defaultdict(int)
    usage_rows_by_gran: dict[str, dict[str, int]] = {
        "message": defaultdict(int),
        "turn": defaultdict(int),
        "session_cumulative": defaultdict(int),
    }
    for session in sessions:
        sessions_by[session.logical_harness] += 1
    if metric_ids:
        placeholders = ",".join("?" for _ in metric_ids)
        message_counts = conn.execute(
            f"""
            SELECT session_id, COUNT(*) AS c
            FROM messages WHERE session_id IN ({placeholders})
            GROUP BY session_id
            """,
            metric_ids,
        ).fetchall()
        for row in message_counts:
            messages_by[by_metric[str(row["session_id"])].logical_harness] += int(row["c"])
        usage_rows = conn.execute(
            f"""
            SELECT session_id, granularity, COUNT(*) AS c
            FROM token_usage WHERE session_id IN ({placeholders})
            GROUP BY session_id, granularity
            """,
            metric_ids,
        ).fetchall()
        seen_usage_sessions: set[str] = set()
        for row in usage_rows:
            metric_id = str(row["session_id"])
            harness = by_metric[metric_id].logical_harness
            if metric_id not in seen_usage_sessions:
                sessions_with_by[harness] += 1
                seen_usage_sessions.add(metric_id)
            granularity = str(row["granularity"])
            if granularity in usage_rows_by_gran:
                usage_rows_by_gran[granularity][harness] += int(row["c"])
        message_usage = conn.execute(
            f"""
            SELECT session_id, COUNT(DISTINCT message_id) AS c
            FROM token_usage
            WHERE session_id IN ({placeholders}) AND message_id IS NOT NULL
            GROUP BY session_id
            """,
            metric_ids,
        ).fetchall()
        for row in message_usage:
            messages_with_by[
                by_metric[str(row["session_id"])].logical_harness
            ] += int(row["c"])

    st = len(sessions)
    sw = sum(sessions_with_by.values())
    mt = sum(messages_by.values())
    mw = sum(messages_with_by.values())
    by_harness = [
        {
            "harness": harness,
            "sessions": sessions_by[harness],
            "sessions_with_usage": sessions_with_by.get(harness, 0),
            "messages": messages_by.get(harness, 0),
            "messages_with_usage": messages_with_by.get(harness, 0),
            "message_usage_rows": usage_rows_by_gran["message"].get(harness, 0),
            "turn_usage_rows": usage_rows_by_gran["turn"].get(harness, 0),
            "cumulative_usage_rows": usage_rows_by_gran["session_cumulative"].get(
                harness, 0
            ),
        }
        for harness in sorted(sessions_by)
    ]
    return {
        "sessions_total": st,
        "sessions_with_usage": sw,
        "sessions_coverage": (sw / st) if st else 0.0,
        "messages_total": mt,
        "messages_with_usage": mw,
        "messages_coverage": (mw / mt) if mt else 0.0,
        "by_harness": by_harness,
    }


def corpus_totals(conn: sqlite3.Connection, tr: TimeRange) -> dict[str, Any]:
    rows = _session_additive_rows(conn, tr)
    totals = _totals_from_rows(rows)
    return {
        "totals": totals,
        "cache_ratios": _cache_ratios(totals),
        "coverage": coverage(conn, tr),
        "note": (
            "Totals sum additive session contributions only: Claude message "
            "usage sums and Codex final session_cumulative snapshots. "
            "Codex cumulative mid-session rows are not summed. "
            "Fields are not perfectly aligned across harnesses; null means "
            "not reported. Coverage shows how much of the corpus carries usage."
        ),
        "cost": _corpus_cost(rows),
    }


def _corpus_cost(rows: list[sqlite3.Row]) -> dict[str, Any]:
    table = get_pricing()
    if not table.models:
        return {
            "status": "unavailable",
            "pricing_table_version": table.version,
            "as_of": table.as_of,
            "message": (
                "pricing.toml has no model rates; cost stays unavailable "
                "until the owner fills verifiable per-token prices."
            ),
            "usd": None,
        }
    total = 0.0
    estimated_sessions = 0
    unavailable_sessions = 0
    for row in rows:
        result = estimate_cost(
            model=row["model"],
            input_tokens=row["input_tokens"],
            output_tokens=row["output_tokens"],
            cache_read_input_tokens=row["cache_read_input_tokens"],
            cache_creation_input_tokens=row["cache_creation_input_tokens"],
            cached_input_tokens=row["cached_input_tokens"],
            cache_write_input_tokens=row["cache_write_input_tokens"],
            pricing=table,
        )
        if result["status"] == "estimated" and result["usd"] is not None:
            total += float(result["usd"])
            estimated_sessions += 1
        else:
            unavailable_sessions += 1
    if estimated_sessions == 0:
        return {
            "status": "unavailable",
            "pricing_table_version": table.version,
            "as_of": table.as_of,
            "message": (
                "No session models match configured rates in pricing.toml."
            ),
            "usd": None,
            "estimated_sessions": 0,
            "unavailable_sessions": unavailable_sessions,
        }
    return {
        "status": "estimated",
        "pricing_table_version": table.version,
        "as_of": table.as_of,
        "usd": total,
        "estimated_sessions": estimated_sessions,
        "unavailable_sessions": unavailable_sessions,
    }


def by_harness(conn: sqlite3.Connection, tr: TimeRange) -> dict[str, Any]:
    rows = _session_additive_rows(conn, tr)
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(str(row["harness"]), []).append(row)
    items = []
    for harness in sorted(grouped):
        totals = _totals_from_rows(grouped[harness])
        items.append(
            {
                "harness": harness,
                "sessions_with_usage": len(grouped[harness]),
                "totals": totals,
                "cache_ratios": _cache_ratios(totals),
            }
        )
    return {"items": items, "coverage": coverage(conn, tr)}


def by_model(conn: sqlite3.Connection, tr: TimeRange) -> dict[str, Any]:
    """Session-grain token totals.

    Codex reports only per-session cumulative totals, so a session's tokens
    cannot be split across the models it switched between. Rows are therefore
    keyed by the session's start model and named so no reader mistakes them
    for per-message attribution.
    """
    rows = _session_additive_rows(conn, tr)
    grouped: dict[str, list[sqlite3.Row]] = {}
    harness_split: dict[str, dict[str, int]] = {}
    for row in rows:
        model = str(row["model"])
        grouped.setdefault(model, []).append(row)
        split = harness_split.setdefault(model, {})
        harness = str(row["harness"])
        split[harness] = split.get(harness, 0) + 1
    items = []
    for model in sorted(grouped):
        totals = _totals_from_rows(grouped[model])
        breakdown = sorted(
            harness_split[model].items(), key=lambda kv: (-kv[1], kv[0])
        )
        items.append(
            {
                "session_start_model": model,
                "harnesses": [
                    {"harness": h, "sessions_with_usage": n}
                    for h, n in breakdown
                ],
                "sessions_with_usage": len(grouped[model]),
                "totals": totals,
                "cache_ratios": _cache_ratios(totals),
            }
        )
    items.sort(
        key=lambda x: (
            -(x["totals"]["total_tokens"] or 0),
            -(x["totals"]["input_tokens"] or 0),
            x["session_start_model"],
        )
    )
    return {
        "items": items,
        "grain": SESSION_START_MODEL,
        "grain_note": GRAIN_DESCRIPTIONS[SESSION_START_MODEL],
        "note": (
            "Session-grain totals. For per-message model attribution use "
            "/api/tokens/usage?group_by=model, which groups on the message "
            "model."
        ),
        "coverage": coverage(conn, tr),
    }


def timeseries_daily(conn: sqlite3.Connection, tr: TimeRange) -> dict[str, Any]:
    rows = _session_additive_rows(conn, tr)
    by_day: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        day = str(row["started_at"] or "")[:10]
        if not day:
            continue
        by_day.setdefault(day, []).append(row)
    series = []
    for day in sorted(by_day):
        totals = _totals_from_rows(by_day[day])
        series.append(
            {
                "day": day,
                "sessions_with_usage": len(by_day[day]),
                "totals": totals,
                "cache_ratios": _cache_ratios(totals),
            }
        )
    return {
        "series": series,
        "note": "Daily sums of additive session token contributions.",
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


def _cost_unavailable(reason: str, message: str) -> dict[str, Any]:
    table = get_pricing()
    return {
        "status": "unavailable",
        "reason": reason,
        "message": message,
        "usd": None,
        "pricing_table_version": table.version,
        "as_of": table.as_of,
    }


def _group_cost_payload() -> dict[str, Any]:
    table = get_pricing()
    if not table.models or not any(r.has_any_rate() for r in table.models.values()):
        return _cost_unavailable(
            "pricing_rates_unconfigured",
            (
                "pricing.toml has no model rates; cost stays unavailable "
                "until the owner fills verifiable per-token prices."
            ),
        )
    return _cost_unavailable(
        "pricing_partial_or_unmatched",
        "Rates exist but grouped cost estimation is not applied without "
        "full model×rate coverage for every token row in the group.",
    )


def _coverage_block(
    *,
    messages_with_usage: int,
    messages_total: int,
    sessions_with_usage: int,
    sessions_total: int,
    aggregation: str,
) -> dict[str, Any]:
    msg_ratio = (
        (messages_with_usage / messages_total) if messages_total else 0.0
    )
    sess_ratio = (
        (sessions_with_usage / sessions_total) if sessions_total else 0.0
    )
    # Message-linked coverage is the honest default; session-cumulative
    # sources (Codex) never mark complete via messages alone.
    complete = (
        messages_total > 0
        and messages_with_usage == messages_total
        and aggregation == "sum_of_message_usage"
    )
    if aggregation == "final_session_cumulative":
        complete = (
            sessions_total > 0 and sessions_with_usage == sessions_total
        )
    partial = not complete and (messages_with_usage > 0 or sessions_with_usage > 0)
    return {
        "messages_with_usage": messages_with_usage,
        "messages_total": messages_total,
        "messages_coverage": msg_ratio,
        "sessions_with_usage": sessions_with_usage,
        "sessions_total": sessions_total,
        "sessions_coverage": sess_ratio,
        "complete": complete,
        "partial": partial,
        "aggregation": aggregation,
        "note": (
            "Token sums cover only rows with usage; incomplete coverage means "
            "the sum is not a corpus total for this group."
            if partial or not complete
            else "Every unit in this group's denominator carries token usage."
        ),
    }


def usage(
    conn: sqlite3.Connection, tr: TimeRange, *, group_by: str = "harness"
) -> dict[str, Any]:
    """Grouped token usage with explicit per-group coverage and cost status."""
    if group_by not in _USAGE_GROUP_BY:
        raise ValueError(
            f"group_by must be one of {sorted(_USAGE_GROUP_BY)}"
        )
    time_sql, params = _session_time_clause(tr)

    # Additive message-level rows (Claude / Hermes): one contribution per usage row.
    message_rows = list(
        conn.execute(
            f"""
            SELECT
                s.id AS session_id,
                s.harness AS harness,
                {USAGE_MODEL_SQL} AS model,
                COALESCE(NULLIF(s.agent_profile, ''), '(none)') AS agent_profile,
                substr(COALESCE(s.started_at, ''), 1, 10) AS day,
                s.repo AS repo,
                s.cwd AS cwd,
                u.input_tokens AS input_tokens,
                u.output_tokens AS output_tokens,
                u.cache_creation_input_tokens AS cache_creation_input_tokens,
                u.cache_read_input_tokens AS cache_read_input_tokens,
                u.cached_input_tokens AS cached_input_tokens,
                u.cache_write_input_tokens AS cache_write_input_tokens,
                u.reasoning_output_tokens AS reasoning_output_tokens,
                u.total_tokens AS total_tokens,
                u.message_id AS message_id,
                'sum_of_message_usage' AS aggregation
            FROM token_usage u
            JOIN sessions s ON s.id = u.session_id
            LEFT JOIN messages m ON m.id = u.message_id
            WHERE u.granularity = 'message'
              AND {time_sql}
            """,
            params,
        )
    )
    # Codex: final session_cumulative only (never sum mid-session cumulatives).
    cumulative_rows = list(
        conn.execute(
            f"""
            SELECT
                s.id AS session_id,
                s.harness AS harness,
                {USAGE_MODEL_SQL} AS model,
                COALESCE(NULLIF(s.agent_profile, ''), '(none)') AS agent_profile,
                substr(COALESCE(s.started_at, ''), 1, 10) AS day,
                s.repo AS repo,
                s.cwd AS cwd,
                u.input_tokens AS input_tokens,
                u.output_tokens AS output_tokens,
                u.cache_creation_input_tokens AS cache_creation_input_tokens,
                u.cache_read_input_tokens AS cache_read_input_tokens,
                u.cached_input_tokens AS cached_input_tokens,
                u.cache_write_input_tokens AS cache_write_input_tokens,
                u.reasoning_output_tokens AS reasoning_output_tokens,
                u.total_tokens AS total_tokens,
                u.message_id AS message_id,
                'final_session_cumulative' AS aggregation
            FROM token_usage u
            JOIN sessions s ON s.id = u.session_id
            LEFT JOIN messages m ON m.id = u.message_id
            JOIN (
                SELECT u2.session_id AS session_id, MAX(u2.seq) AS max_seq
                FROM token_usage u2
                JOIN sessions s2 ON s2.id = u2.session_id
                WHERE u2.granularity = 'session_cumulative'
                  AND {_session_time_clause(tr, alias="s2")[0]}
                GROUP BY u2.session_id
            ) latest
              ON latest.session_id = u.session_id AND latest.max_seq = u.seq
            WHERE u.granularity = 'session_cumulative'
              AND {time_sql}
            """,
            params,
        )
    )

    def _dim(row: sqlite3.Row) -> str:
        if group_by == "harness":
            return str(row["harness"])
        if group_by == "model":
            return str(row["model"])
        if group_by == "agent_profile":
            return str(row["agent_profile"])
        if group_by == "day":
            return str(row["day"] or "")
        return _project_label(row["repo"], row["cwd"])

    contrib: dict[str, list[sqlite3.Row]] = defaultdict(list)
    agg_by_dim: dict[str, set[str]] = defaultdict(set)
    sessions_with: dict[str, set[str]] = defaultdict(set)
    messages_with: dict[str, set[str]] = defaultdict(set)
    for row in message_rows + cumulative_rows:
        key = _dim(row)
        if group_by == "day" and not key:
            continue
        contrib[key].append(row)
        agg_by_dim[key].add(str(row["aggregation"]))
        sessions_with[key].add(str(row["session_id"]))
        mid = row["message_id"]
        if mid:
            messages_with[key].add(str(mid))

    # Denominators must share the numerator's grain. For group_by="model" the
    # numerator is message-level, so the session denominator counts sessions
    # observed at that message model — not sessions whose start model matched.
    session_denoms: dict[str, int] = defaultdict(int)
    if group_by == "model":
        sessions_seen: dict[str, set[str]] = defaultdict(set)
        for row in conn.execute(
            f"""
            SELECT
                m.session_id AS session_id,
                {MESSAGE_MODEL_SQL} AS model
            FROM messages m
            JOIN sessions s ON s.id = m.session_id
            WHERE {time_sql}
            """,
            params,
        ):
            sessions_seen[str(row["model"])].add(str(row["session_id"]))
        for key, rows_for_key in contrib.items():
            for row in rows_for_key:
                sessions_seen[key].add(str(row["session_id"]))
        for key, ids in sessions_seen.items():
            session_denoms[key] = len(ids)
    else:
        for row in conn.execute(
            f"""
            SELECT
                s.id AS session_id,
                s.harness AS harness,
                COALESCE(NULLIF(s.agent_profile, ''), '(none)') AS agent_profile,
                substr(COALESCE(s.started_at, ''), 1, 10) AS day,
                s.repo AS repo,
                s.cwd AS cwd
            FROM sessions s
            WHERE {time_sql}
            """,
            params,
        ):
            if group_by == "harness":
                key = str(row["harness"])
            elif group_by == "agent_profile":
                key = str(row["agent_profile"])
            elif group_by == "day":
                key = str(row["day"] or "")
                if not key:
                    continue
            else:
                key = _project_label(row["repo"], row["cwd"])
            session_denoms[key] += 1

    message_denoms: dict[str, int] = defaultdict(int)
    for row in conn.execute(
        f"""
        SELECT
            s.harness AS harness,
            {MESSAGE_MODEL_SQL} AS model,
            COALESCE(NULLIF(s.agent_profile, ''), '(none)') AS agent_profile,
            substr(COALESCE(s.started_at, ''), 1, 10) AS day,
            s.repo AS repo,
            s.cwd AS cwd
        FROM messages m
        JOIN sessions s ON s.id = m.session_id
        WHERE {time_sql}
        """,
        params,
    ):
        if group_by == "harness":
            key = str(row["harness"])
        elif group_by == "model":
            key = str(row["model"])
        elif group_by == "agent_profile":
            key = str(row["agent_profile"])
        elif group_by == "day":
            key = str(row["day"] or "")
            if not key:
                continue
        else:
            key = _project_label(row["repo"], row["cwd"])
        message_denoms[key] += 1

    grain = MESSAGE_MODEL if group_by == "model" else "session_attribute"
    cost = _group_cost_payload()
    keys = sorted(
        set(session_denoms) | set(message_denoms) | set(contrib),
        key=lambda k: (
            -(
                (_totals_from_rows(contrib[k])["total_tokens"] or 0)
                if k in contrib
                else 0
            ),
            -(
                (_totals_from_rows(contrib[k])["input_tokens"] or 0)
                if k in contrib
                else 0
            ),
            k,
        ),
    )
    groups: list[dict[str, Any]] = []
    for key in keys:
        rows = contrib.get(key, [])
        totals = _totals_from_rows(rows) if rows else _empty_totals()
        aggs = agg_by_dim.get(key, set())
        if aggs == {"sum_of_message_usage"}:
            aggregation = "sum_of_message_usage"
        elif aggs == {"final_session_cumulative"}:
            aggregation = "final_session_cumulative"
        elif not aggs:
            aggregation = "none"
        else:
            aggregation = "mixed"
        cov = _coverage_block(
            messages_with_usage=len(messages_with.get(key, set())),
            messages_total=message_denoms.get(key, 0),
            sessions_with_usage=len(sessions_with.get(key, set())),
            sessions_total=session_denoms.get(key, 0),
            aggregation=aggregation,
        )
        groups.append(
            {
                "key": key,
                "group_by": group_by,
                "grain": grain,
                "input_tokens": totals["input_tokens"],
                "output_tokens": totals["output_tokens"],
                "total_tokens": totals["total_tokens"],
                "cache_read_input_tokens": totals["cache_read_input_tokens"],
                "cache_creation_input_tokens": totals[
                    "cache_creation_input_tokens"
                ],
                "cached_input_tokens": totals["cached_input_tokens"],
                "cache_write_input_tokens": totals["cache_write_input_tokens"],
                "reasoning_output_tokens": totals["reasoning_output_tokens"],
                "fields_present": totals["fields_present"],
                "usage_rows": len(rows),
                "coverage": cov,
                "cost": cost,
            }
        )

    return {
        "group_by": group_by,
        "grain": grain,
        "identity_grain": "physical_sessions",
        "grain_note": (
            GRAIN_DESCRIPTIONS[MESSAGE_MODEL]
            if group_by == "model"
            else (
                "Groups are session attributes; sessions_total counts sessions "
                "carrying the attribute and messages_total counts their messages."
            )
        ),
        "sessions_total_basis": (
            "sessions containing at least one message or usage row resolved to "
            "this model"
            if group_by == "model"
            else "sessions carrying this attribute"
        ),
        "groups": groups,
        "cost": cost,
        "harness_reality": {
            "claude": (
                "Per-assistant-message usage (input/output + cache read/write "
                "when present). Summing message rows is correct."
            ),
            "codex": (
                "Turn and session_cumulative snapshots; aggregates use the "
                "final session_cumulative only — never sum cumulatives."
            ),
            "cursor": "No token_usage rows in the ledger; tokens unavailable.",
            "warp": "No token_usage rows in the ledger; tokens unavailable.",
            "t3code": (
                "state.sqlite records no token counts; tokens unavailable."
            ),
        },
        "note": (
            "Descriptive token sums at physical-session grain with explicit coverage. Partial coverage "
            "means the sum must not be treated as complete for the group. "
            "Cost is unavailable while pricing.toml has no rates."
        ),
    }


def session_tokens(conn: sqlite3.Connection, session_id: str) -> dict[str, Any] | None:
    session = conn.execute(
        "SELECT * FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if session is None:
        return None
    identity = build_identity_context(conn)
    projection = logical_projection(
        conn, session_id, str(session["harness"]), context=identity
    )
    metric_session_id = str(projection["transcript_session_id"] or session_id)
    metric_session = session
    if metric_session_id != session_id:
        metric_session = conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (metric_session_id,)
        ).fetchone() or session
    rows = list(
        conn.execute(
            """
            SELECT *
            FROM token_usage
            WHERE session_id = ?
            ORDER BY seq, usage_source
            """,
            (metric_session_id,),
        )
    )
    additive = [
        r for r in rows if r["granularity"] in _ADDITIVE_GRANULARITIES
    ]
    if any(r["granularity"] == _SESSION_CUMULATIVE for r in rows):
        latest = max(
            (r for r in rows if r["granularity"] == _SESSION_CUMULATIVE),
            key=lambda r: int(r["seq"]),
        )
        additive = [latest]
    totals = _totals_from_rows(additive) if additive else _empty_totals()
    return {
        "session_id": session_id,
        "harness": projection["logical_harness"],
        "runtime_harness": projection["runtime_harness"],
        "orchestrator_session_id": projection["orchestrator_session_id"],
        "transcript_session_id": metric_session_id,
        "model": display_model(metric_session["model_canonical"]),
        "model_raw": metric_session["model"],
        "model_grain": SESSION_START_MODEL,
        "totals": totals,
        "cache_ratios": _cache_ratios(totals),
        "records": [dict(r) for r in rows],
        "aggregation": (
            "final session_cumulative"
            if any(r["granularity"] == _SESSION_CUMULATIVE for r in rows)
            else "sum of message usage"
        ),
    }
