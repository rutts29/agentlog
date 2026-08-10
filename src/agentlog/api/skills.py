"""Skills inventory and descriptive effectiveness profiles API."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query

from agentlog.analysis.skills import (
    DEFAULT_MIN_SESSIONS,
    list_skill_profiles,
    skill_detail,
    skill_inventory_report,
)
from agentlog.api.deps import get_conn
from agentlog.api.ranges import TimeRange, parse_range, range_params

router = APIRouter(tags=["skills"])


def _parse_range_dep(
    range: str = Query("30d", alias="range"),
    start: str | None = None,
    end: str | None = None,
) -> TimeRange:
    try:
        return parse_range(range, custom_start=start, custom_end=end)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/skills")
def skills_list(
    conn: sqlite3.Connection = Depends(get_conn),
    tr: TimeRange = Depends(_parse_range_dep),
    min_sessions: int = Query(DEFAULT_MIN_SESSIONS, ge=1, le=100),
) -> dict:
    data = list_skill_profiles(
        conn,
        min_sessions=min_sessions,
        start_iso=tr.start_iso,
        end_iso=tr.end_iso,
    )
    return {**range_params(tr), **data}


@router.get("/api/skills/duplicates")
def skills_duplicates(
    conn: sqlite3.Connection = Depends(get_conn),
    include_groups: bool = Query(True),
) -> dict:
    report = skill_inventory_report(conn)
    if not include_groups:
        for key in ("exact_duplicates", "normalized_duplicates", "name_conflicts"):
            report[key] = []
    return report


@router.get("/api/skills/{skill_id}")
def skills_get(
    skill_id: str,
    conn: sqlite3.Connection = Depends(get_conn),
    tr: TimeRange = Depends(_parse_range_dep),
    min_sessions: int = Query(DEFAULT_MIN_SESSIONS, ge=1, le=100),
) -> dict:
    detail = skill_detail(
        conn,
        skill_id,
        min_sessions=min_sessions,
        start_iso=tr.start_iso,
        end_iso=tr.end_iso,
    )
    if detail is None:
        raise HTTPException(status_code=404, detail="skill not found")
    return {**range_params(tr), **detail}
