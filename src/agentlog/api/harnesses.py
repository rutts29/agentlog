"""Harness registry API helpers: declared capabilities + live DB coverage."""

from __future__ import annotations

import sqlite3
from typing import Any

from agentlog.api.identity_aggregates import visible_logical_sessions
from agentlog.registry.harnesses import CAPABILITY_KEYS, list_harnesses


def _ratio(observed: int, total: int) -> float | None:
    if total <= 0:
        return None
    return observed / total


def _empty_coverage() -> dict[str, Any]:
    return {
        "sessions": 0,
        "messages": 0,
        "per_message_model": {"observed": 0, "total": 0, "coverage": None},
        "per_message_tokens": {"observed": 0, "total": 0, "coverage": None},
        "effort": {"observed": 0, "total": 0, "coverage": None},
        "branch": {"observed": 0, "total": 0, "coverage": None},
        "commit_sha": {"observed": 0, "total": 0, "coverage": None},
        "ended_at": {"observed": 0, "total": 0, "coverage": None},
        "tool_events": {"observed": 0, "total": 0, "coverage": None},
        "skill_exposures": {"observed": 0, "total": 0, "coverage": None},
        "subagent_links": {"observed": 0, "total": 0, "coverage": None},
    }


def _physical_live_coverage_by_harness(
    conn: sqlite3.Connection,
) -> dict[str, dict[str, Any]]:
    """Compute per-capability coverage for all harnesses in a few grouped queries."""
    session_rows = {
        str(r["harness"]): r
        for r in conn.execute(
            """
            SELECT
                harness,
                COUNT(*) AS sessions,
                SUM(
                    CASE WHEN branch IS NOT NULL AND TRIM(branch) != '' THEN 1 ELSE 0 END
                ) AS branch,
                SUM(
                    CASE
                        WHEN commit_sha IS NOT NULL AND TRIM(commit_sha) != '' THEN 1
                        ELSE 0
                    END
                ) AS commit_sha,
                SUM(
                    CASE
                        WHEN ended_at IS NOT NULL AND TRIM(ended_at) != '' THEN 1
                        ELSE 0
                    END
                ) AS ended_at,
                SUM(
                    CASE
                        WHEN parent_session_id IS NOT NULL
                             AND TRIM(parent_session_id) != '' THEN 1
                        ELSE 0
                    END
                ) AS parent
            FROM sessions
            GROUP BY harness
            """
        ).fetchall()
    }
    message_rows = {
        str(r["harness"]): r
        for r in conn.execute(
            """
            SELECT
                s.harness AS harness,
                COUNT(*) AS messages,
                SUM(
                    CASE
                        WHEN m.model IS NOT NULL AND TRIM(m.model) != '' THEN 1
                        ELSE 0
                    END
                ) AS msg_model,
                SUM(
                    CASE
                        WHEN m.effort IS NOT NULL AND TRIM(m.effort) != '' THEN 1
                        ELSE 0
                    END
                ) AS msg_effort
            FROM messages m
            JOIN sessions s ON s.id = m.session_id
            GROUP BY s.harness
            """
        ).fetchall()
    }
    msg_tokens = {
        str(r["harness"]): int(r["c"])
        for r in conn.execute(
            """
            SELECT s.harness AS harness, COUNT(DISTINCT u.message_id) AS c
            FROM token_usage u
            JOIN sessions s ON s.id = u.session_id
            WHERE u.granularity = 'message' AND u.message_id IS NOT NULL
            GROUP BY s.harness
            """
        ).fetchall()
    }
    tool_sessions = {
        str(r["harness"]): int(r["c"])
        for r in conn.execute(
            """
            SELECT s.harness AS harness, COUNT(DISTINCT t.session_id) AS c
            FROM tool_events t
            JOIN sessions s ON s.id = t.session_id
            GROUP BY s.harness
            """
        ).fetchall()
    }
    skill_sessions = {
        str(r["harness"]): int(r["c"])
        for r in conn.execute(
            """
            SELECT s.harness AS harness, COUNT(DISTINCT sk.session_id) AS c
            FROM skill_exposures sk
            JOIN sessions s ON s.id = sk.session_id
            GROUP BY s.harness
            """
        ).fetchall()
    }

    out: dict[str, dict[str, Any]] = {}
    for harness_id in set(session_rows) | set(message_rows):
        srow = session_rows.get(harness_id)
        mrow = message_rows.get(harness_id)
        n_sessions = int(srow["sessions"]) if srow else 0
        n_messages = int(mrow["messages"]) if mrow else 0
        branch_n = int(srow["branch"] or 0) if srow else 0
        commit_n = int(srow["commit_sha"] or 0) if srow else 0
        ended_n = int(srow["ended_at"] or 0) if srow else 0
        parent_n = int(srow["parent"] or 0) if srow else 0
        model_n = int(mrow["msg_model"] or 0) if mrow else 0
        effort_n = int(mrow["msg_effort"] or 0) if mrow else 0
        tokens_n = msg_tokens.get(harness_id, 0)
        tools_n = tool_sessions.get(harness_id, 0)
        skills_n = skill_sessions.get(harness_id, 0)
        out[harness_id] = {
            "sessions": n_sessions,
            "messages": n_messages,
            "per_message_model": {
                "observed": model_n,
                "total": n_messages,
                "coverage": _ratio(model_n, n_messages),
            },
            "per_message_tokens": {
                "observed": tokens_n,
                "total": n_messages,
                "coverage": _ratio(tokens_n, n_messages),
            },
            "effort": {
                "observed": effort_n,
                "total": n_messages,
                "coverage": _ratio(effort_n, n_messages),
            },
            "branch": {
                "observed": branch_n,
                "total": n_sessions,
                "coverage": _ratio(branch_n, n_sessions),
            },
            "commit_sha": {
                "observed": commit_n,
                "total": n_sessions,
                "coverage": _ratio(commit_n, n_sessions),
            },
            "ended_at": {
                "observed": ended_n,
                "total": n_sessions,
                "coverage": _ratio(ended_n, n_sessions),
            },
            "tool_events": {
                "observed": tools_n,
                "total": n_sessions,
                "coverage": _ratio(tools_n, n_sessions),
            },
            "skill_exposures": {
                "observed": skills_n,
                "total": n_sessions,
                "coverage": _ratio(skills_n, n_sessions),
            },
            "subagent_links": {
                "observed": parent_n,
                "total": n_sessions,
                "coverage": _ratio(parent_n, n_sessions),
            },
        }
    return out


def live_coverage_by_harness(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    rows = conn.execute("SELECT id, harness FROM sessions").fetchall()
    sessions = visible_logical_sessions(conn, rows)
    by_metric = {session.metric_session_id: session for session in sessions}
    if not by_metric:
        return {}
    metric_ids = sorted(by_metric)
    placeholders = ",".join("?" for _ in metric_ids)
    source_rows = {
        str(row["id"]): row
        for row in conn.execute(
            f"""
            SELECT id, branch, commit_sha, ended_at, parent_session_id
            FROM sessions WHERE id IN ({placeholders})
            """,
            metric_ids,
        ).fetchall()
    }
    stats: dict[str, dict[str, int]] = {}

    def bucket(harness: str) -> dict[str, int]:
        return stats.setdefault(
            harness,
            {
                "sessions": 0,
                "messages": 0,
                "message_model": 0,
                "message_effort": 0,
                "message_tokens": 0,
                "branch": 0,
                "commit_sha": 0,
                "ended_at": 0,
                "parent": 0,
                "tool_sessions": 0,
                "skill_sessions": 0,
            },
        )

    for metric_session_id, logical in by_metric.items():
        counts = bucket(logical.logical_harness)
        counts["sessions"] += 1
        source = source_rows.get(metric_session_id)
        if source is None:
            continue
        for key in ("branch", "commit_sha", "ended_at", "parent_session_id"):
            if str(source[key] or "").strip():
                counts["parent" if key == "parent_session_id" else key] += 1

    for row in conn.execute(
        f"""
        SELECT session_id, model, effort FROM messages
        WHERE session_id IN ({placeholders})
        """,
        metric_ids,
    ):
        counts = bucket(by_metric[str(row["session_id"])].logical_harness)
        counts["messages"] += 1
        if str(row["model"] or "").strip():
            counts["message_model"] += 1
        if str(row["effort"] or "").strip():
            counts["message_effort"] += 1

    for row in conn.execute(
        f"""
        SELECT DISTINCT session_id, message_id FROM token_usage
        WHERE granularity = 'message' AND message_id IS NOT NULL
          AND session_id IN ({placeholders})
        """,
        metric_ids,
    ):
        bucket(by_metric[str(row["session_id"])].logical_harness)[
            "message_tokens"
        ] += 1
    for table, field in (
        ("tool_events", "tool_sessions"),
        ("skill_exposures", "skill_sessions"),
    ):
        rows = conn.execute(
            f"SELECT DISTINCT session_id FROM {table} WHERE session_id IN ({placeholders})",
            metric_ids,
        ).fetchall()
        for row in rows:
            bucket(by_metric[str(row["session_id"])].logical_harness)[field] += 1

    out: dict[str, dict[str, Any]] = {}
    for harness, counts in stats.items():
        n_sessions = counts["sessions"]
        n_messages = counts["messages"]
        out[harness] = {
            "sessions": n_sessions,
            "messages": n_messages,
            "per_message_model": {
                "observed": counts["message_model"],
                "total": n_messages,
                "coverage": _ratio(counts["message_model"], n_messages),
            },
            "per_message_tokens": {
                "observed": counts["message_tokens"],
                "total": n_messages,
                "coverage": _ratio(counts["message_tokens"], n_messages),
            },
            "effort": {
                "observed": counts["message_effort"],
                "total": n_messages,
                "coverage": _ratio(counts["message_effort"], n_messages),
            },
            "branch": {
                "observed": counts["branch"],
                "total": n_sessions,
                "coverage": _ratio(counts["branch"], n_sessions),
            },
            "commit_sha": {
                "observed": counts["commit_sha"],
                "total": n_sessions,
                "coverage": _ratio(counts["commit_sha"], n_sessions),
            },
            "ended_at": {
                "observed": counts["ended_at"],
                "total": n_sessions,
                "coverage": _ratio(counts["ended_at"], n_sessions),
            },
            "tool_events": {
                "observed": counts["tool_sessions"],
                "total": n_sessions,
                "coverage": _ratio(counts["tool_sessions"], n_sessions),
            },
            "skill_exposures": {
                "observed": counts["skill_sessions"],
                "total": n_sessions,
                "coverage": _ratio(counts["skill_sessions"], n_sessions),
            },
            "subagent_links": {
                "observed": counts["parent"],
                "total": n_sessions,
                "coverage": _ratio(counts["parent"], n_sessions),
            },
        }
    return out


def live_coverage(conn: sqlite3.Connection, harness_id: str) -> dict[str, Any]:
    """Compute per-capability coverage fractions for one harness from the DB."""
    return live_coverage_by_harness(conn).get(harness_id, _empty_coverage())


def harness_matrix(conn: sqlite3.Connection) -> dict[str, Any]:
    coverage_by = live_coverage_by_harness(conn)
    items: list[dict[str, Any]] = []
    for record in list_harnesses():
        harness_id = str(record["id"])
        caps_out: dict[str, Any] = {}
        declared = record.get("capabilities") or {}
        notes = record.get("notes") or {}
        coverage = (
            coverage_by.get(harness_id, _empty_coverage())
            if record.get("ingest_status") == "active"
            else None
        )
        for key in CAPABILITY_KEYS:
            entry: dict[str, Any] = {
                "level": declared.get(key, "unknown"),
            }
            note = notes.get(key)
            if note:
                entry["note"] = note
            if coverage is not None and key in coverage:
                live = coverage[key]
                entry["coverage"] = live["coverage"]
                entry["observed"] = live["observed"]
                entry["total"] = live["total"]
            caps_out[key] = entry
        items.append(
            {
                "id": harness_id,
                "display_name": record["display_name"],
                "vendor": record["vendor"],
                "ingest_status": record["ingest_status"],
                "transcript_locations": list(record.get("transcript_locations") or []),
                "capabilities": caps_out,
                "sessions": coverage["sessions"] if coverage else 0,
                "messages": coverage["messages"] if coverage else 0,
            }
        )
    return {
        "items": items,
        "capability_keys": list(CAPABILITY_KEYS),
        "identity_grain": "logical_sessions",
    }
