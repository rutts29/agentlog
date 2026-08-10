"""Constellation graph payload: sessions as stars, repo anchors as places.

Scheme A from the v2 redesign: every node is a real, clickable session;
orchestration edges are real parent_session_id links; membership edges pull
sessions toward their repo anchor. No transcript text ever ships here.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from agentlog.api.deps import get_conn
from agentlog.api.queries import _project_label
from agentlog.api.ranges import TimeRange, parse_range, range_params, session_time_clause

router = APIRouter(tags=["graph"])

# Above this many in-range sessions, degrade to last-90d + all supervisors
# with an honest truncation note — never silent.
NODE_CAP = 3000

# Stable harness order for composition rings / spatial sub-groups so the
# same harness occupies a consistent direction across projects.
_HARNESS_ORDER = {"claude": 0, "codex": 1, "cursor": 2, "warp": 3}


def _harness_rank(name: str) -> tuple[int, str]:
    key = (name or "").lower()
    return (_HARNESS_ORDER.get(key, 50), key)

_SESSION_SQL = """
SELECT
    s.id, s.harness, s.external_id, s.parent_session_id,
    s.started_at, s.ended_at, s.repo, s.cwd,
    COALESCE(NULLIF(s.model_canonical, ''), '(unknown)') AS model,
    CASE
      WHEN s.started_at IS NOT NULL AND s.ended_at IS NOT NULL
           AND julianday(s.ended_at) IS NOT NULL
           AND julianday(s.started_at) IS NOT NULL
      THEN CAST(
        (julianday(s.ended_at) - julianday(s.started_at)) * 86400 AS INTEGER
      )
      ELSE NULL
    END AS duration_seconds,
    (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id) AS message_count,
    (SELECT COUNT(*) FROM tool_events t WHERE t.session_id = s.id) AS tool_count
FROM sessions s
"""


def _parse_range_dep(
    range: str = Query("30d", alias="range"),
    start: str | None = None,
    end: str | None = None,
) -> TimeRange:
    try:
        return parse_range(range, custom_start=start, custom_end=end)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _time_clause(tr: TimeRange) -> tuple[str, dict[str, Any]]:
    return session_time_clause(tr)


def graph_payload(conn: sqlite3.Connection, tr: TimeRange) -> dict[str, Any]:
    where, params = _time_clause(tr)
    total = int(
        conn.execute(
            f"SELECT COUNT(*) AS c FROM sessions s WHERE {where}", params
        ).fetchone()["c"]
    )

    truncated: dict[str, Any] | None = None
    if total > NODE_CAP:
        # parent_session_id may be a bare external_id or harness:external_id,
        # so match supervisors on all three spellings.
        where = f"""({where} AND COALESCE(s.started_at, '') >= datetime(:end, '-90 days'))
        OR EXISTS (
            SELECT 1 FROM sessions c
            WHERE c.parent_session_id IN
                (s.id, s.external_id, s.harness || ':' || s.external_id)
        )"""
        rows = conn.execute(
            f"{_SESSION_SQL} WHERE {where}", params
        ).fetchall()
        truncated = {
            "shown": len(rows),
            "hidden": total - len(rows),
            "note": f"showing 90d · {total - len(rows)} older sessions hidden",
        }
    else:
        rows = conn.execute(
            f"{_SESSION_SQL} WHERE {where}", params
        ).fetchall()

    by_key: dict[str, str] = {}
    for r in rows:
        by_key[r["id"]] = r["id"]
        by_key.setdefault(str(r["external_id"]), r["id"])
        by_key.setdefault(f"{r['harness']}:{r['external_id']}", r["id"])

    child_counts: dict[str, int] = {}
    parent_of: dict[str, str] = {}
    for r in rows:
        raw_parent = r["parent_session_id"]
        if not raw_parent:
            continue
        resolved = by_key.get(str(raw_parent))
        if resolved is None or resolved == r["id"]:
            continue
        parent_of[r["id"]] = resolved
        child_counts[resolved] = child_counts.get(resolved, 0) + 1

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    repo_counts: dict[str, int] = {}
    harness_by_id = {r["id"]: r["harness"] for r in rows}

    # Per-repo composition: descriptive counts only, aggregated in Python
    # because the repo label is derived (_project_label) rather than stored.
    repo_of: dict[str, str] = {
        r["id"]: _project_label(r["repo"], r["cwd"]) for r in rows
    }
    repo_agg: dict[str, dict[str, Any]] = {}

    for r in rows:
        repo = repo_of[r["id"]]
        repo_counts[repo] = repo_counts.get(repo, 0) + 1
        agg = repo_agg.setdefault(
            repo,
            {
                "harnesses": {},
                "messages": 0,
                "tools": 0,
                "first_at": None,
                "last_at": None,
            },
        )
        agg["harnesses"][r["harness"]] = agg["harnesses"].get(r["harness"], 0) + 1
        agg["messages"] += int(r["message_count"])
        agg["tools"] += int(r["tool_count"])
        started = r["started_at"]
        ended = r["ended_at"] or r["started_at"]
        if started and (agg["first_at"] is None or started < agg["first_at"]):
            agg["first_at"] = started
        if ended and (agg["last_at"] is None or ended > agg["last_at"]):
            agg["last_at"] = ended
        parent_id = parent_of.get(r["id"])
        nodes.append(
            {
                "id": r["id"],
                "kind": "session",
                "harness": r["harness"],
                "model": r["model"],
                "repo": repo,
                "started_at": r["started_at"],
                "ended_at": r["ended_at"],
                "duration_seconds": r["duration_seconds"],
                "messages": int(r["message_count"]),
                "tools": int(r["tool_count"]),
                "parent_id": parent_id,
                "children": child_counts.get(r["id"], 0),
            }
        )
        edges.append(
            {"source": r["id"], "target": f"repo:{repo}", "kind": "membership"}
        )
        if parent_id is not None:
            edges.append(
                {
                    "source": parent_id,
                    "target": r["id"],
                    "kind": "orchestration",
                    "harness": harness_by_id.get(parent_id),
                }
            )

    # Per-message model/effort mix (captures mid-session model switches).
    models_by_repo: dict[str, dict[str, int]] = {}
    efforts_by_repo: dict[str, dict[str, int]] = {}
    comp_rows = conn.execute(
        f"""
        SELECT m.session_id AS sid,
               COALESCE(NULLIF(m.model_canonical, ''), '(unknown)') AS model,
               m.effort AS effort,
               COUNT(*) AS n
        FROM messages m JOIN sessions s ON s.id = m.session_id
        WHERE ({where})
        GROUP BY m.session_id,
                 COALESCE(NULLIF(m.model_canonical, ''), '(unknown)'),
                 m.effort
        """,
        params,
    ).fetchall()
    for c in comp_rows:
        repo = repo_of.get(c["sid"])
        if repo is None:
            continue
        n = int(c["n"])
        if c["model"]:
            bucket = models_by_repo.setdefault(repo, {})
            bucket[c["model"]] = bucket.get(c["model"], 0) + n
        if c["effort"]:
            bucket = efforts_by_repo.setdefault(repo, {})
            bucket[c["effort"]] = bucket.get(c["effort"], 0) + n

    def _ranked(bucket: dict[str, int], key: str) -> list[dict[str, Any]]:
        return [
            {key: name, "messages": n}
            for name, n in sorted(bucket.items(), key=lambda x: (-x[1], x[0]))
        ]

    for repo, count in sorted(repo_counts.items(), key=lambda x: (-x[1], x[0])):
        agg = repo_agg[repo]
        nodes.append(
            {
                "id": f"repo:{repo}",
                "kind": "repo",
                "label": repo,
                "sessions": count,
                "harnesses": [
                    {"harness": h, "sessions": n}
                    for h, n in sorted(
                        agg["harnesses"].items(),
                        key=lambda x: (*_harness_rank(x[0]), -x[1]),
                    )
                ],
                "models": _ranked(models_by_repo.get(repo, {}), "model"),
                "efforts": _ranked(efforts_by_repo.get(repo, {}), "effort"),
                "first_at": agg["first_at"],
                "last_at": agg["last_at"],
                "messages": agg["messages"],
                "tools": agg["tools"],
            }
        )

    return {
        "nodes": nodes,
        "edges": edges,
        "counts": {
            "sessions": len(rows),
            "repos": len(repo_counts),
            "orchestration_edges": len(parent_of),
        },
        "truncated": truncated,
        "note": (
            "Sessions and real parent_session_id links. Descriptive only; "
            "no transcript text."
        ),
    }


@router.get("/api/graph")
def graph(
    conn: sqlite3.Connection = Depends(get_conn),
    tr: TimeRange = Depends(_parse_range_dep),
) -> dict:
    return {**range_params(tr), **graph_payload(conn, tr)}
