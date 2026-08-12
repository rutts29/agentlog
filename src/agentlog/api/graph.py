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
from agentlog.api.identity_aggregates import visible_logical_sessions
from agentlog.api.queries import _project_label
from agentlog.api.ranges import TimeRange, parse_range, range_params, session_time_clause
from agentlog.session_identity import (
    build_identity_context,
    lineage_parent_ids,
    logical_projection,
    provider_root_shadow_ids,
)

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
        where = f"({where} AND COALESCE(s.started_at, '') >= datetime(:end, '-90 days'))"
        physical_rows = conn.execute(
            f"{_SESSION_SQL} WHERE {where}", params
        ).fetchall()
        shown_ids = {str(row["id"]) for row in physical_rows}
        supervisor_ids = set(lineage_parent_ids(conn).values()) - shown_ids
        if supervisor_ids:
            placeholders = ",".join("?" for _ in supervisor_ids)
            physical_rows.extend(
                conn.execute(
                    f"{_SESSION_SQL} WHERE s.id IN ({placeholders})",
                    sorted(supervisor_ids),
                ).fetchall()
            )
        hidden = max(0, total - len(physical_rows))
        truncated = {
            "shown": len(physical_rows),
            "hidden": hidden,
            "note": f"showing 90d · {hidden} older sessions hidden",
        }
    else:
        physical_rows = conn.execute(
            f"{_SESSION_SQL} WHERE {where}", params
        ).fetchall()

    identity = build_identity_context(conn)
    root_shadows = provider_root_shadow_ids(conn, context=identity)
    physical_ids = {str(row["id"]) for row in physical_rows}
    owner_ids: set[str] = set()
    for row in physical_rows:
        if str(row["id"]) not in root_shadows:
            continue
        projection = logical_projection(
            conn, str(row["id"]), str(row["harness"]), context=identity
        )
        owner_id = projection["orchestrator_session_id"]
        if owner_id and str(owner_id) not in physical_ids:
            owner_ids.add(str(owner_id))
    for source_id, backings in identity.backings_by_source.items():
        if source_id in physical_ids:
            continue
        for backing in backings:
            target_id = backing["target_session_id"]
            if (
                backing.get("link_role") == "worker"
                and target_id
                and str(target_id) in physical_ids
                and identity.owners_by_session.get(str(target_id), set())
                == {source_id}
            ):
                owner_ids.add(source_id)
                break
    if owner_ids:
        placeholders = ",".join("?" for _ in owner_ids)
        physical_rows.extend(
            conn.execute(
                f"{_SESSION_SQL} WHERE s.id IN ({placeholders})",
                sorted(owner_ids),
            ).fetchall()
        )

    visible = visible_logical_sessions(conn, physical_rows, context=identity)
    rows = [session.row for session in visible]
    visible_by_id = {session.session_id: session for session in visible}

    lineage_parents = lineage_parent_ids(conn)
    presentation_by_physical: dict[str, str] = {}
    for r in physical_rows:
        session_id = str(r["id"])
        projection = logical_projection(
            conn, session_id, str(r["harness"]), context=identity
        )
        presentation_by_physical[session_id] = (
            str(projection["orchestrator_session_id"])
            if session_id in root_shadows and projection["orchestrator_session_id"]
            else session_id
        )
    child_counts: dict[str, int] = {}
    parent_of: dict[str, str] = {}
    for r in rows:
        resolved = lineage_parents.get(str(r["id"]))
        if resolved is None or resolved == r["id"]:
            continue
        parent_id = presentation_by_physical.get(resolved, resolved)
        if parent_id == r["id"] or parent_id not in visible_by_id:
            continue
        parent_of[r["id"]] = parent_id
        child_counts[parent_id] = child_counts.get(parent_id, 0) + 1
    for source_id, backings in identity.backings_by_source.items():
        if source_id not in visible_by_id:
            continue
        for backing in backings:
            target_id = backing["target_session_id"]
            if (
                backing.get("link_role") != "worker"
                or not target_id
                or str(target_id) not in visible_by_id
            ):
                continue
            child_id = str(target_id)
            if child_id in parent_of or child_id == source_id:
                continue
            if identity.owners_by_session.get(child_id, set()) != {source_id}:
                continue
            parent_of[child_id] = source_id
            child_counts[source_id] = child_counts.get(source_id, 0) + 1

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    repo_counts: dict[str, int] = {}
    harness_by_id = {
        session.session_id: session.logical_harness for session in visible
    }
    metric_ids = sorted({session.metric_session_id for session in visible})
    session_by_metric = {
        session.metric_session_id: session for session in visible
    }
    metric_models: dict[str, str] = {}
    message_counts: dict[str, int] = {}
    tool_counts: dict[str, int] = {}
    if metric_ids:
        placeholders = ",".join("?" for _ in metric_ids)
        metric_models = {
            str(row["id"]): str(row["model"])
            for row in conn.execute(
                f"""
                SELECT id,
                       COALESCE(NULLIF(model_canonical, ''), '(unknown)') AS model
                FROM sessions
                WHERE id IN ({placeholders})
                """,
                metric_ids,
            ).fetchall()
        }
        message_counts = {
            str(row["session_id"]): int(row["c"])
            for row in conn.execute(
                f"""
                SELECT session_id, COUNT(*) AS c
                FROM messages
                WHERE session_id IN ({placeholders})
                GROUP BY session_id
                """,
                metric_ids,
            ).fetchall()
        }
        tool_counts = {
            str(row["session_id"]): int(row["c"])
            for row in conn.execute(
                f"""
                SELECT session_id, COUNT(*) AS c
                FROM tool_events
                WHERE session_id IN ({placeholders})
                GROUP BY session_id
                """,
                metric_ids,
            ).fetchall()
        }

    # Per-repo composition: descriptive counts only, aggregated in Python
    # because the repo label is derived (_project_label) rather than stored.
    repo_of: dict[str, str] = {
        r["id"]: _project_label(r["repo"], r["cwd"]) for r in rows
    }
    repo_agg: dict[str, dict[str, Any]] = {}

    for r in rows:
        session = visible_by_id[str(r["id"])]
        projection = logical_projection(
            conn, session.session_id, str(r["harness"]), context=identity
        )
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
        agg["harnesses"][session.logical_harness] = (
            agg["harnesses"].get(session.logical_harness, 0) + 1
        )
        agg["messages"] += message_counts.get(session.metric_session_id, 0)
        agg["tools"] += tool_counts.get(session.metric_session_id, 0)
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
                "logical_harness": session.logical_harness,
                "runtime_harness": session.runtime_harness,
                "orchestrator_session_id": session.orchestrator_session_id,
                "transcript_session_id": projection["transcript_session_id"],
                "model": metric_models.get(session.metric_session_id, r["model"]),
                "repo": repo,
                "started_at": r["started_at"],
                "ended_at": r["ended_at"],
                "duration_seconds": r["duration_seconds"],
                "messages": message_counts.get(session.metric_session_id, 0),
                "tools": tool_counts.get(session.metric_session_id, 0),
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
    comp_rows: list[sqlite3.Row] = []
    if metric_ids:
        placeholders = ",".join("?" for _ in metric_ids)
        comp_rows = conn.execute(
            f"""
            SELECT m.session_id AS sid,
                   COALESCE(NULLIF(m.model_canonical, ''), '(unknown)') AS model,
                   m.effort AS effort,
                   COUNT(*) AS n
            FROM messages m
            WHERE m.session_id IN ({placeholders})
            GROUP BY m.session_id,
                     COALESCE(NULLIF(m.model_canonical, ''), '(unknown)'),
                     m.effort
            """,
            metric_ids,
        ).fetchall()
    for c in comp_rows:
        session = session_by_metric.get(str(c["sid"]))
        repo = repo_of.get(session.session_id) if session is not None else None
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
