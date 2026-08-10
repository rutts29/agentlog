"""Grouped token-usage metrics for the dashboard."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query

from agentlog.api import tokens
from agentlog.api.deps import get_conn
from agentlog.api.ranges import TimeRange, parse_range, range_params

router = APIRouter(tags=["tokens"])


def _parse_range_dep(
    range: str = Query("30d", alias="range"),
    start: str | None = None,
    end: str | None = None,
) -> TimeRange:
    try:
        return parse_range(range, custom_start=start, custom_end=end)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/tokens/usage")
def tokens_usage(
    conn: sqlite3.Connection = Depends(get_conn),
    tr: TimeRange = Depends(_parse_range_dep),
    group_by: str = Query("harness"),
) -> dict:
    try:
        data = tokens.usage(conn, tr, group_by=group_by)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {**range_params(tr), **data}
