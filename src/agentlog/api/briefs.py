"""Session brief API (JSON + Markdown)."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse

from agentlog.analysis.briefs import (
    build_session_brief,
    render_brief_markdown,
)
from agentlog.api.deps import get_conn

router = APIRouter(tags=["briefs"])


def _brief_or_404(conn: sqlite3.Connection, session_id: str) -> dict:
    brief = build_session_brief(conn, session_id)
    if brief is None:
        raise HTTPException(status_code=404, detail="session not found")
    return brief


@router.get("/api/sessions/{session_id}/brief")
def session_brief(
    session_id: str,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    return _brief_or_404(conn, session_id)


@router.get("/api/sessions/{session_id}/brief.md")
def session_brief_md(
    session_id: str,
    conn: sqlite3.Connection = Depends(get_conn),
) -> PlainTextResponse:
    brief = _brief_or_404(conn, session_id)
    return PlainTextResponse(
        render_brief_markdown(brief),
        media_type="text/markdown; charset=utf-8",
    )


@router.get("/api/sessions/{session_id:path}/brief")
def session_brief_path(
    session_id: str,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    return _brief_or_404(conn, session_id)


@router.get("/api/sessions/{session_id:path}/brief.md")
def session_brief_md_path(
    session_id: str,
    conn: sqlite3.Connection = Depends(get_conn),
) -> PlainTextResponse:
    brief = _brief_or_404(conn, session_id)
    return PlainTextResponse(
        render_brief_markdown(brief),
        media_type="text/markdown; charset=utf-8",
    )
