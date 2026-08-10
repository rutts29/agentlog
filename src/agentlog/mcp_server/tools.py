"""Read-only query tools for the agentlog MCP server.

All functions take an open ``sqlite3.Connection`` and return compact JSON-ready
dicts. They never write to the database or the filesystem.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from typing import Any, Literal
from urllib.parse import urlparse

from agentlog.analysis.attention import derive_attention
from agentlog.analysis.skills import list_skill_profiles
from agentlog.api.model_rollup import (
    GRAIN_DESCRIPTIONS,
    SESSION_START_MODEL,
    SESSION_START_MODEL_SQL,
)

DEFAULT_SESSION_LIMIT = 10
MAX_SESSION_LIMIT = 50
DEFAULT_MESSAGE_TRUNCATE = 200
DEFAULT_MESSAGE_LIMIT = 40
MAX_MESSAGE_LIMIT = 80
DEFAULT_SKILL_LIMIT = 40

GroupBy = Literal["harness", "model", "day", "repo", "agent_profile"]

_FTS_TOKEN_RE = re.compile(r"[A-Za-z0-9_./+-]+")


def _clip(text: str | None, limit: int) -> str:
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 1)] + "…"


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


def _fts_match(raw: str) -> str | None:
    tokens = _FTS_TOKEN_RE.findall(raw or "")
    if not tokens:
        return None
    return " AND ".join(f'"{t}"' for t in tokens[:24])


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


def _first_user_preview(conn: sqlite3.Connection, session_id: str) -> str | None:
    row = conn.execute(
        """
        SELECT text FROM messages
        WHERE session_id = ? AND role = 'user'
          AND COALESCE(is_tool_plumbing, 0) = 0
        ORDER BY seq ASC
        LIMIT 1
        """,
        (session_id,),
    ).fetchone()
    if row is None:
        return None
    preview = _clip(row["text"], 120)
    return preview or None


def _session_meta(conn: sqlite3.Connection, session_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        f"""
        SELECT
            s.id, s.harness, s.external_id, s.parent_session_id,
            s.started_at, s.ended_at, s.repo, s.cwd, s.branch,
            s.commit_sha, s.model_canonical, s.model AS model_raw,
            s.provider, s.agent_profile, s.effort,
            {_duration_seconds_sql()} AS duration_seconds,
            (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id)
                AS message_count,
            (SELECT COUNT(*) FROM tool_events t WHERE t.session_id = s.id)
                AS tool_count
        FROM sessions s
        WHERE s.id = ?
        """,
        (session_id,),
    ).fetchone()
    if row is None:
        return None
    dur = row["duration_seconds"]
    return {
        "id": row["id"],
        "harness": row["harness"],
        "external_id": row["external_id"],
        "parent_session_id": row["parent_session_id"],
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "repo": row["repo"],
        "cwd": row["cwd"],
        "project": _project_label(row["repo"], row["cwd"]),
        "branch": row["branch"],
        "commit_sha": row["commit_sha"],
        "model": row["model_canonical"] or "(unknown)",
        "model_raw": row["model_raw"],
        "provider": row["provider"],
        "agent_profile": row["agent_profile"],
        "effort": row["effort"],
        "duration_seconds": int(dur) if dur is not None and int(dur) >= 0 else None,
        "message_count": int(row["message_count"]),
        "tool_count": int(row["tool_count"]),
        "title": _first_user_preview(conn, session_id),
    }


def search_sessions(
    conn: sqlite3.Connection,
    query: str,
    *,
    harness: str | None = None,
    since: str | None = None,
    limit: int = DEFAULT_SESSION_LIMIT,
) -> dict[str, Any]:
    """Full-text-ish session search over messages, id, repo, cwd, and model."""
    limit = max(1, min(int(limit), MAX_SESSION_LIMIT))
    q = (query or "").strip()
    if not q:
        return {"query": query, "total": 0, "sessions": [], "note": "Empty query."}

    clauses: list[str] = []
    params: dict[str, Any] = {}
    if harness:
        clauses.append("s.harness = :harness")
        params["harness"] = harness
    if since:
        clauses.append("COALESCE(s.started_at, '') >= :since")
        params["since"] = since
    filter_sql = (" AND " + " AND ".join(clauses)) if clauses else ""

    session_ids: list[str] = []
    seen: set[str] = set()
    match = _fts_match(q)

    if match is not None:
        try:
            rows = conn.execute(
                f"""
                SELECT m.session_id, bm25(messages_fts) AS rank
                FROM messages_fts
                JOIN messages m ON m.rowid = messages_fts.rowid
                JOIN sessions s ON s.id = m.session_id
                WHERE messages_fts MATCH :match{filter_sql}
                ORDER BY rank
                LIMIT :lim
                """,
                {**params, "match": match, "lim": limit * 5},
            ).fetchall()
            for r in rows:
                sid = str(r["session_id"])
                if sid not in seen:
                    seen.add(sid)
                    session_ids.append(sid)
        except sqlite3.OperationalError:
            pass

    like = f"%{q}%"
    meta_rows = conn.execute(
        f"""
        SELECT s.id
        FROM sessions s
        WHERE (
            s.id LIKE :like
            OR COALESCE(s.repo, '') LIKE :like
            OR COALESCE(s.cwd, '') LIKE :like
            OR COALESCE(s.model, '') LIKE :like
            OR COALESCE(s.model_canonical, '') LIKE :like
            OR COALESCE(s.branch, '') LIKE :like
        ){filter_sql}
        ORDER BY COALESCE(s.started_at, '') DESC
        LIMIT :lim
        """,
        {**params, "like": like, "lim": limit * 3},
    ).fetchall()
    for r in meta_rows:
        sid = str(r["id"])
        if sid not in seen:
            seen.add(sid)
            session_ids.append(sid)

    sessions: list[dict[str, Any]] = []
    for sid in session_ids:
        if len(sessions) >= limit:
            break
        meta = _session_meta(conn, sid)
        if meta is not None:
            sessions.append(meta)

    return {
        "query": query,
        "total": len(sessions),
        "sessions": sessions,
    }


def get_session(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    include_messages: bool = True,
    message_truncate: int = DEFAULT_MESSAGE_TRUNCATE,
    message_limit: int = DEFAULT_MESSAGE_LIMIT,
) -> dict[str, Any]:
    """Return session detail; messages are truncated and capped."""
    meta = _session_meta(conn, session_id)
    if meta is None and ":" not in session_id:
        for prefix in ("codex:", "claude:", "cursor:", "warp:", "hermes:"):
            meta = _session_meta(conn, prefix + session_id)
            if meta is not None:
                session_id = prefix + session_id
                break
    if meta is None:
        return {"error": "not_found", "session_id": session_id}

    out: dict[str, Any] = {"session": meta}
    if not include_messages:
        return out

    truncate = max(40, min(int(message_truncate), 500))
    limit = max(1, min(int(message_limit), MAX_MESSAGE_LIMIT))
    rows = conn.execute(
        """
        SELECT id, seq, role, timestamp, model, text, is_tool_plumbing
        FROM messages
        WHERE session_id = ?
        ORDER BY seq ASC
        LIMIT ?
        """,
        (session_id, limit),
    ).fetchall()
    total = int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE session_id = ?",
            (session_id,),
        ).fetchone()["n"]
    )
    messages = [
        {
            "id": r["id"],
            "seq": r["seq"],
            "role": r["role"],
            "timestamp": r["timestamp"],
            "model": r["model"],
            "is_tool_plumbing": bool(r["is_tool_plumbing"]),
            "text": _clip(r["text"], truncate),
        }
        for r in rows
    ]
    out["messages"] = messages
    out["messages_returned"] = len(messages)
    out["messages_total"] = total
    out["message_truncate"] = truncate
    if total > len(messages):
        out["note"] = f"Truncated to {len(messages)} of {total} messages."
    return out


def usage_stats(
    conn: sqlite3.Connection,
    group_by: GroupBy,
    *,
    since: str | None = None,
) -> dict[str, Any]:
    """Counts and durations grouped by harness, model, day, or repo."""
    if group_by not in {
        "harness",
        "model",
        "day",
        "repo",
        "agent_profile",
    }:
        return {
            "error": "invalid_group_by",
            "allowed": [
                "harness",
                "model",
                "day",
                "repo",
                "agent_profile",
            ],
        }

    clauses: list[str] = ["1=1"]
    params: list[Any] = []
    if since:
        clauses.append("COALESCE(s.started_at, '') >= ?")
        params.append(since)
    where = " AND ".join(clauses)
    dur_sql = _duration_seconds_sql()

    if group_by == "harness":
        key_sql = "s.harness"
    elif group_by == "model":
        key_sql = SESSION_START_MODEL_SQL
    elif group_by == "agent_profile":
        key_sql = "COALESCE(NULLIF(s.agent_profile, ''), '(none)')"
    elif group_by == "day":
        key_sql = "substr(COALESCE(s.started_at, ''), 1, 10)"
    else:
        key_sql = "COALESCE(NULLIF(s.repo, ''), COALESCE(NULLIF(s.cwd, ''), '(unknown)'))"

    rows = conn.execute(
        f"""
        SELECT
            {key_sql} AS bucket,
            COUNT(*) AS session_count,
            SUM(CASE WHEN ({dur_sql}) IS NOT NULL THEN ({dur_sql}) ELSE 0 END)
                AS duration_seconds_sum,
            SUM(CASE WHEN ({dur_sql}) IS NOT NULL THEN 1 ELSE 0 END)
                AS with_duration,
            SUM(
              (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id)
            ) AS message_count
        FROM sessions s
        WHERE {where}
        GROUP BY bucket
        ORDER BY session_count DESC, bucket ASC
        LIMIT 100
        """,
        params,
    ).fetchall()

    groups: list[dict[str, Any]] = []
    for r in rows:
        raw = r["bucket"] or "(unknown)"
        if group_by == "repo" and raw != "(unknown)":
            key = _project_label(str(raw), None)
        else:
            key = str(raw)
        n_dur = int(r["with_duration"] or 0)
        dur_sum = int(r["duration_seconds_sum"] or 0)
        groups.append(
            {
                "key": key,
                "session_count": int(r["session_count"]),
                "message_count": int(r["message_count"] or 0),
                "duration_seconds_sum": dur_sum if n_dur else 0,
                "duration_seconds_avg": (
                    round(dur_sum / n_dur) if n_dur else None
                ),
                "with_duration": n_dur,
            }
        )

    total_sessions = sum(g["session_count"] for g in groups)
    out: dict[str, Any] = {
        "group_by": group_by,
        "since": since,
        "total_sessions": total_sessions,
        "groups": groups,
    }
    if group_by == "model":
        out["grain"] = SESSION_START_MODEL
        out["grain_note"] = (
            GRAIN_DESCRIPTIONS[SESSION_START_MODEL]
            + " message_count is the session's total, attributed to that start "
            "model even when the session switched models mid-way."
        )
    return out


def attention_inbox(conn: sqlite3.Connection) -> dict[str, Any]:
    """Attention Inbox items from ``analysis.attention.derive_attention``."""
    items = derive_attention(conn)
    payload = [item.to_dict() for item in items[:50]]
    return {
        "count": len(items),
        "returned": len(payload),
        "items": payload,
    }


def skill_inventory(
    conn: sqlite3.Connection,
    *,
    limit: int = DEFAULT_SKILL_LIMIT,
) -> dict[str, Any]:
    """Skills with exposure counts (via analysis.skills profiles, compact)."""
    limit = max(1, min(int(limit), 100))
    data = list_skill_profiles(conn, min_sessions=1)
    items = [
        {
            "id": it.get("id"),
            "name": it["name"],
            "source": it.get("source"),
            "indexed": bool(it.get("indexed")),
            "exposure_count": int(it["fires"]),
            "sessions": int(it["sessions"]),
            "last_fired": it.get("last_fired"),
            "description": _clip(it.get("description"), 120),
        }
        for it in data["items"][:limit]
    ]
    return {
        "indexed_count": int(data.get("indexed_count") or 0),
        "activations": int(data.get("activations") or 0),
        "distinct_fired": int(data.get("distinct_fired") or 0),
        "returned": len(items),
        "items": items,
    }


def agreement_and_extraction_status(conn: sqlite3.Connection) -> dict[str, Any]:
    """Extraction progress: ux_observations, windows, label distributions."""
    tables = {
        str(r[0])
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    windows_total = int(
        conn.execute("SELECT COUNT(*) AS n FROM exchange_windows").fetchone()["n"]
    )
    if "ux_observations" not in tables:
        return {
            "ux_observations": 0,
            "windows_total": windows_total,
            "coverage": 0.0,
            "note": "ux_observations table not present.",
        }

    ux_n = int(
        conn.execute("SELECT COUNT(*) AS n FROM ux_observations").fetchone()["n"]
    )
    covered_windows = int(
        conn.execute(
            "SELECT COUNT(DISTINCT window_id) AS n FROM ux_observations"
        ).fetchone()["n"]
    )
    coverage = (
        round(covered_windows / windows_total, 4) if windows_total else 0.0
    )

    def _dist(column: str) -> dict[str, int]:
        rows = conn.execute(
            f"""
            SELECT COALESCE({column}, '(null)') AS label, COUNT(*) AS n
            FROM ux_observations
            GROUP BY label
            ORDER BY n DESC
            LIMIT 30
            """
        ).fetchall()
        return {str(r["label"]): int(r["n"]) for r in rows}

    turn_kinds: Counter[str] = Counter()
    for row in conn.execute(
        "SELECT turn_kinds_json FROM ux_observations LIMIT 5000"
    ):
        try:
            kinds = json.loads(row["turn_kinds_json"] or "[]")
        except json.JSONDecodeError:
            continue
        if isinstance(kinds, list):
            for k in kinds:
                turn_kinds[str(k)] += 1

    runs: list[dict[str, Any]] = []
    if "derivation_runs" in tables:
        run_rows = conn.execute(
            """
            SELECT status, COUNT(*) AS n
            FROM derivation_runs
            GROUP BY status
            ORDER BY n DESC
            """
        ).fetchall()
        runs = [{"status": r["status"], "count": int(r["n"])} for r in run_rows]

    return {
        "ux_observations": ux_n,
        "windows_total": windows_total,
        "windows_with_observations": covered_windows,
        "coverage": coverage,
        "label_distribution": {
            "user_stance": _dist("user_stance"),
            "agent_stance": _dist("agent_stance"),
            "prior_outcome": _dist("prior_outcome"),
            "turn_kinds": dict(turn_kinds.most_common(30)),
        },
        "derivation_runs": runs,
    }
