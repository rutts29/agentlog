"""AI code attribution API — session↔git commit joins (descriptive only)."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException

from agentlog.analysis.attribution import (
    attribution_rollup,
    rebuild_attribution,
    session_attribution,
)
from agentlog.api.deps import get_conn, get_write_conn

router = APIRouter(tags=["attribution"])


@router.get("/api/attribution")
def attribution(
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    return attribution_rollup(conn)


@router.post("/api/attribution/rebuild")
def attribution_rebuild(
    conn: sqlite3.Connection = Depends(get_write_conn),
) -> dict:
    """Rebuild session_commits from local git history, then return the rollup."""
    stats = rebuild_attribution(conn)
    rollup = attribution_rollup(conn)
    return {
        "rebuild": {
            "repos_seen": stats.repos_seen,
            "repos_resolved": stats.repos_resolved,
            "repos_skipped": stats.repos_skipped,
            "sessions_considered": stats.sessions_considered,
            "explicit_joins": stats.explicit_joins,
            "time_window_joins": stats.time_window_joins,
            "sessions_with_join": stats.sessions_with_join,
            "sessions_no_joinable_commit": stats.sessions_no_joinable,
            "sessions_unresolved_repo": stats.sessions_unresolved,
            "sessions_failed": stats.sessions_failed,
            "sessions_published": stats.sessions_published,
            "published": stats.published,
            "errors": stats.errors[:20],
        },
        **rollup,
    }


@router.get("/api/attribution/session/{session_id:path}")
def attribution_session(
    session_id: str,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    detail = session_attribution(conn, session_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="session not found")
    return detail
