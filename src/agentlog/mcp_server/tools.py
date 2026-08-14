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
from agentlog.api.identity_aggregates import visible_logical_sessions
from agentlog.session_identity import (
    IdentityContext,
    build_identity_context,
    logical_projection,
    provider_root_shadow_ids,
)
from agentlog.api.model_rollup import (
    GRAIN_DESCRIPTIONS,
    SESSION_START_MODEL,
)
from agentlog.source_reader import (
    CachedSourceTranscriptReader,
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


def _first_user_preview(
    conn: sqlite3.Connection,
    session_id: str,
    source_reader: CachedSourceTranscriptReader | None = None,
) -> str | None:
    storage = conn.execute(
        "SELECT transcript_storage FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if storage is not None and storage["transcript_storage"] == "source_backed":
        source = (source_reader or CachedSourceTranscriptReader())(conn, session_id)
        if not source.ready:
            return None
        for message in source.messages:
            if message.get("role") == "user" and not message.get("is_tool_plumbing"):
                preview = _clip(str(message.get("text") or ""), 120)
                if preview:
                    return preview
        return None
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


def _session_meta(
    conn: sqlite3.Connection,
    session_id: str,
    source_reader: CachedSourceTranscriptReader | None = None,
    *,
    context: IdentityContext | None = None,
) -> dict[str, Any] | None:
    row = conn.execute(
        f"""
        SELECT
            s.id, s.harness, s.external_id, s.parent_session_id,
            s.started_at, s.ended_at, s.repo, s.cwd, s.branch,
            s.commit_sha, s.model_canonical, s.model AS model_raw,
            s.provider, s.agent_profile, s.effort, s.transcript_storage,
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
    projection = logical_projection(
        conn, str(row["id"]), str(row["harness"]), context=context
    )
    transcript_id = str(projection["transcript_session_id"] or row["id"])
    transcript_storage = row["transcript_storage"]
    if transcript_id != str(row["id"]):
        transcript_row = conn.execute(
            "SELECT transcript_storage FROM sessions WHERE id = ?", (transcript_id,)
        ).fetchone()
        if transcript_row is None:
            return None
        transcript_storage = transcript_row["transcript_storage"]
    source = None
    source_read = None
    if transcript_storage == "source_backed":
        source_read = (source_reader or CachedSourceTranscriptReader())(
            conn, transcript_id
        )
        source = {
            "status": source_read.status,
            "identity": source_read.source_identity,
            "unit_id": source_read.source_unit_id,
            "warning": source_read.warning,
        }
        title = next(
            (
                _clip(str(message.get("text") or ""), 120)
                for message in source_read.messages
                if message.get("role") == "user"
                and not message.get("is_tool_plumbing")
                and str(message.get("text") or "").strip()
            ),
            None,
        )
    else:
        title = _first_user_preview(conn, transcript_id, source_reader)
    message_count = int(row["message_count"])
    if source_read is not None and source_read.ready:
        message_count = len(source_read.messages)
    return {
        "id": row["id"],
        "harness": row["harness"],
        "logical_harness": projection["logical_harness"],
        "runtime_harness": projection["runtime_harness"],
        "orchestrator_session_id": projection["orchestrator_session_id"],
        "transcript_session_id": transcript_id,
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
        "message_count": message_count,
        "tool_count": int(row["tool_count"]),
        "title": title,
        "source": source,
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
    provenance: dict[str, str] = {}
    source_reader = CachedSourceTranscriptReader()
    identity = build_identity_context(conn)
    match = _fts_match(q)
    session_columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(sessions)").fetchall()
    }
    storage_filter = (
        " AND COALESCE(s.transcript_storage, 'legacy_materialized') = 'legacy_materialized'"
        if "transcript_storage" in session_columns
        else ""
    )

    if match is not None:
        try:
            rows = conn.execute(
                f"""
                SELECT m.session_id, bm25(messages_fts) AS rank
                FROM messages_fts
                JOIN messages m ON m.rowid = messages_fts.rowid
                JOIN sessions s ON s.id = m.session_id
                WHERE messages_fts MATCH :match{filter_sql}{storage_filter}
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
                    provenance[sid] = "materialized_fts"
        except sqlite3.OperationalError:
            pass

    if "transcript_storage" in session_columns and match is not None:
        source_clauses: list[str] = []
        source_params: dict[str, Any] = {}
        if since:
            source_clauses.append("COALESCE(s.started_at, '') >= :since")
            source_params["since"] = since
        source_where = (
            " WHERE " + " AND ".join(source_clauses) if source_clauses else ""
        )
        source_rows = conn.execute(
            f"""
            SELECT s.* FROM sessions s{source_where}
            ORDER BY COALESCE(s.started_at, '') DESC
            """,
            source_params,
        ).fetchall()
        source_visible = [
            session
            for session in visible_logical_sessions(conn, source_rows, context=identity)
            if harness is None or session.logical_harness == harness
        ][: min(MAX_SESSION_LIMIT * 5, 200)]
        query_tokens = _FTS_TOKEN_RE.findall(q)[:24]
        metric_ids = sorted({session.metric_session_id for session in source_visible})
        metric_storage: dict[str, str | None] = {}
        if metric_ids:
            placeholders = ",".join("?" for _ in metric_ids)
            metric_storage = {
                str(row["id"]): row["transcript_storage"]
                for row in conn.execute(
                    f"SELECT id, transcript_storage FROM sessions WHERE id IN ({placeholders})",
                    metric_ids,
                )
            }
        for source_session in source_visible:
            if metric_storage.get(source_session.metric_session_id) != "source_backed":
                continue
            result = source_reader(conn, source_session.metric_session_id)
            if not result.ready:
                continue
            if not any(
                all(token.casefold() in str(message.get("text") or "").casefold() for token in query_tokens)
                for message in result.messages
            ):
                continue
            sid = source_session.session_id
            if sid not in seen:
                seen.add(sid)
                session_ids.append(sid)
            provenance[sid] = "source_scan"

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

    if session_ids:
        candidate_rows = conn.execute("SELECT * FROM sessions").fetchall()
        candidate_ids = set(session_ids)
        canonical_ids: list[str] = []
        canonical_provenance: dict[str, str] = {}
        for candidate in visible_logical_sessions(conn, candidate_rows, context=identity):
            if candidate.session_id not in candidate_ids and candidate.metric_session_id not in candidate_ids:
                continue
            if candidate.session_id in canonical_ids:
                continue
            canonical_ids.append(candidate.session_id)
            source_ids = {candidate.session_id, candidate.metric_session_id}
            canonical_provenance[candidate.session_id] = next(
                (provenance[source_id] for source_id in source_ids if source_id in provenance),
                "metadata",
            )
        session_ids = canonical_ids
        provenance = canonical_provenance

    sessions: list[dict[str, Any]] = []
    for sid in session_ids:
        if len(sessions) >= limit:
            break
        meta = _session_meta(conn, sid, source_reader, context=identity)
        if meta is not None:
            meta["provenance"] = provenance.get(sid, "metadata")
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
    source_reader = CachedSourceTranscriptReader()
    identity = build_identity_context(conn)
    meta = _session_meta(conn, session_id, source_reader, context=identity)
    if meta is None and ":" not in session_id:
        for prefix in ("codex:", "claude:", "cursor:", "warp:", "hermes:", "grok:"):
            meta = _session_meta(
                conn, prefix + session_id, source_reader, context=identity
            )
            if meta is not None:
                session_id = prefix + session_id
                break
    if meta is None:
        return {"error": "not_found", "session_id": session_id}

    requested_projection = logical_projection(
        conn, str(meta["id"]), str(meta["harness"]), context=identity
    )
    owner_id = requested_projection["orchestrator_session_id"]
    if str(meta["id"]) in provider_root_shadow_ids(conn, context=identity) and owner_id:
        session_id = str(owner_id)
        meta = _session_meta(conn, session_id, source_reader, context=identity)
        if meta is None:
            return {"error": "not_found", "session_id": session_id}

    projection = logical_projection(
        conn, session_id, str(meta["harness"]), context=identity
    )
    transcript_id = str(projection["transcript_session_id"] or session_id)
    source_read = source_reader(conn, transcript_id)
    if source_read.status == "source_unavailable":
        return {
            "error": "source_unavailable",
            "session_id": session_id,
            "warning": source_read.warning,
        }
    if source_read.status == "source_changed":
        return {
            "error": "source_changed",
            "session_id": session_id,
            "warning": source_read.warning,
        }

    out: dict[str, Any] = {"session": meta}
    if not include_messages:
        return out

    truncate = max(40, min(int(message_truncate), 500))
    limit = max(1, min(int(message_limit), MAX_MESSAGE_LIMIT))
    if source_read.ready:
        rows = source_read.messages[:limit]
        total = len(source_read.messages)
    else:
        rows = conn.execute(
            """
            SELECT id, seq, role, timestamp, model, text, is_tool_plumbing
            FROM messages
            WHERE session_id = ?
            ORDER BY seq ASC
            LIMIT ?
            """,
            (transcript_id, limit),
        ).fetchall()
        total = int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE session_id = ?",
                (transcript_id,),
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

    clauses: list[str] = []
    params: list[Any] = []
    if since:
        clauses.append("COALESCE(s.started_at, '') >= ?")
        params.append(since)
    where = " AND ".join(clauses) or "1=1"
    identity = build_identity_context(conn)
    physical_rows = conn.execute(
        f"SELECT s.* FROM sessions s WHERE {where}", params
    ).fetchall()
    visible = visible_logical_sessions(conn, physical_rows, context=identity)
    metric_ids = sorted({session.metric_session_id for session in visible})
    metric_rows: dict[str, sqlite3.Row] = {}
    message_counts: dict[str, int] = {}
    if metric_ids:
        placeholders = ",".join("?" for _ in metric_ids)
        metric_rows = {
            str(row["id"]): row
            for row in conn.execute(
                f"""
                SELECT s.id, s.model_canonical, s.agent_profile,
                       {_duration_seconds_sql()} AS duration_seconds
                FROM sessions s WHERE s.id IN ({placeholders})
                """,
                metric_ids,
            )
        }
        message_counts = {
            str(row["session_id"]): int(row["n"])
            for row in conn.execute(
                f"""
                SELECT session_id, COUNT(*) AS n FROM messages
                WHERE session_id IN ({placeholders}) GROUP BY session_id
                """,
                metric_ids,
            )
        }

    buckets: dict[str, dict[str, int]] = {}
    for session in visible:
        metric = metric_rows.get(session.metric_session_id)
        if metric is None:
            continue
        display_row = session.row
        if group_by == "harness":
            key = session.logical_harness
        elif group_by == "model":
            key = str(metric["model_canonical"] or "(unknown)")
        elif group_by == "agent_profile":
            key = str(metric["agent_profile"] or "(none)")
        elif group_by == "day":
            key = str(display_row["started_at"] or "")[:10]
        else:
            key = _project_label(display_row["repo"], display_row["cwd"])
        bucket = buckets.setdefault(
            key,
            {
                "session_count": 0,
                "duration_seconds_sum": 0,
                "with_duration": 0,
                "message_count": 0,
            },
        )
        bucket["session_count"] += 1
        bucket["message_count"] += message_counts.get(session.metric_session_id, 0)
        duration = metric["duration_seconds"]
        if duration is not None:
            bucket["duration_seconds_sum"] += int(duration)
            bucket["with_duration"] += 1

    rows = sorted(
        buckets.items(), key=lambda item: (-item[1]["session_count"], item[0])
    )[:100]
    groups: list[dict[str, Any]] = []
    for key, bucket in rows:
        n_dur = bucket["with_duration"]
        dur_sum = bucket["duration_seconds_sum"]
        groups.append(
            {
                "key": key,
                "session_count": bucket["session_count"],
                "message_count": bucket["message_count"],
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
