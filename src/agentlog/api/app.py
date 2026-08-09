from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from agentlog.api import queries
from agentlog.api.deps import get_conn
from agentlog.api.ranges import TimeRange, parse_range, range_params
from agentlog.config import DEFAULT_DB_PATH


def _parse_range_dep(
    range: str = Query("30d", alias="range"),
    start: str | None = None,
    end: str | None = None,
) -> TimeRange:
    try:
        return parse_range(range, custom_start=start, custom_end=end)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def create_app(db_path: Path | None = None) -> FastAPI:
    app = FastAPI(title="agentlog", version="0.1.0")
    app.state.db_path = Path(db_path or DEFAULT_DB_PATH)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1:8722",
            "http://localhost:8722",
            "http://127.0.0.1:5173",
            "http://localhost:5173",
        ],
        allow_methods=["GET"],
        allow_headers=["*"],
    )

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True, "db": str(app.state.db_path)}

    @app.get("/api/meta")
    def meta(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        return {
            "freshness": queries.ingest_freshness(conn),
            "language_contract": {
                "resting_surface": "descriptive usage and interaction-style profile",
                "forbidden": [
                    "best model",
                    "improved",
                    "caused",
                    "success rate for proxy metrics",
                    "redirect/brake as quality score",
                ],
            },
        }

    @app.get("/api/summary")
    def summary(
        conn: sqlite3.Connection = Depends(get_conn),
        tr: TimeRange = Depends(_parse_range_dep),
    ) -> dict:
        current = queries.count_sessions(conn, tr)
        prev = None
        delta = None
        if tr.prev_start is not None and tr.prev_end is not None:
            prev_tr = TimeRange(
                key="prev",
                start=tr.prev_start,
                end=tr.prev_end,
                prev_start=None,
                prev_end=None,
            )
            prev = queries.count_sessions(conn, prev_tr)
            if prev > 0:
                delta = (current - prev) / prev

        lead = queries.semantic_lead_metric(conn, tr)
        streak = queries.streak_days(conn, tr)
        return {
            **range_params(tr),
            "kpis": {
                "sessions": {
                    "value": current,
                    "previous": prev,
                    "delta_ratio": delta,
                    "kind": "count",
                    "label": "Sessions",
                },
                "tokens_est": {
                    "status": "unavailable",
                    "message": (
                        "Token usage is not yet normalized in the ledger. "
                        "No estimate is shown."
                    ),
                },
                "cost_est": {
                    "status": "unavailable",
                    "message": (
                        "Cost estimates require a versioned pricing table and "
                        "normalized token fields. Neither is present yet."
                    ),
                },
                "interaction_style": lead.to_dict(),
                "streak": {
                    "current_days": streak["current"],
                    "longest_days": streak["longest"],
                    "label": "Active days in range",
                    "note": "Calendar days with at least one session start.",
                },
            },
            "flags": lead.flags,
        }

    @app.get("/api/timeseries/sessions")
    def timeseries_sessions(
        conn: sqlite3.Connection = Depends(get_conn),
        tr: TimeRange = Depends(_parse_range_dep),
        by: str = Query("harness"),
    ) -> dict:
        if by != "harness":
            raise HTTPException(status_code=400, detail="only by=harness is supported")
        return {
            **range_params(tr),
            "by": by,
            "series": queries.sessions_by_harness_daily(conn, tr),
            "note": "Daily session counts by harness. Descriptive usage only.",
        }

    @app.get("/api/models")
    def models(
        conn: sqlite3.Connection = Depends(get_conn),
        tr: TimeRange = Depends(_parse_range_dep),
    ) -> dict:
        return {**range_params(tr), **queries.models_profile(conn, tr)}

    @app.get("/api/heatmap")
    def heatmap(
        conn: sqlite3.Connection = Depends(get_conn),
        tr: TimeRange = Depends(_parse_range_dep),
    ) -> dict:
        return {**range_params(tr), **queries.activity_heatmap(conn, tr)}

    @app.get("/api/projects")
    def projects(
        conn: sqlite3.Connection = Depends(get_conn),
        tr: TimeRange = Depends(_parse_range_dep),
    ) -> dict:
        return {
            **range_params(tr),
            "items": queries.top_projects(conn, tr),
        }

    @app.get("/api/sessions")
    def sessions(
        conn: sqlite3.Connection = Depends(get_conn),
        tr: TimeRange = Depends(_parse_range_dep),
        harness: list[str] | None = Query(None),
        model: list[str] | None = Query(None),
        project: list[str] | None = Query(None),
        q: str | None = None,
        cursor: int = 0,
        limit: int = Query(50, ge=1, le=200),
    ) -> dict:
        data = queries.list_sessions(
            conn,
            tr,
            harness=harness,
            model=model,
            project=project,
            q=q,
            cursor=cursor,
            limit=limit,
        )
        return {**range_params(tr), **data}

    @app.get("/api/sessions/recent")
    def sessions_recent(
        conn: sqlite3.Connection = Depends(get_conn),
        tr: TimeRange = Depends(_parse_range_dep),
        limit: int = Query(8, ge=1, le=40),
    ) -> dict:
        return {
            **range_params(tr),
            "items": queries.recent_sessions(conn, tr, limit=limit),
        }

    @app.get("/api/sessions/{session_id}")
    def session(
        session_id: str,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict:
        detail = queries.session_detail(conn, session_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="session not found")
        return detail

    @app.get("/api/skills")
    def skills(
        conn: sqlite3.Connection = Depends(get_conn),
        tr: TimeRange = Depends(_parse_range_dep),
    ) -> dict:
        return {**range_params(tr), **queries.skills_summary(conn, tr)}

    @app.get("/api/insights")
    def insights(
        conn: sqlite3.Connection = Depends(get_conn),
        tr: TimeRange = Depends(_parse_range_dep),
    ) -> dict:
        return {**range_params(tr), **queries.insights_feed(conn, tr)}

    @app.get("/api/aggregates/binary")
    def binary_aggregate(
        successes: int = Query(..., ge=0),
        n: int = Query(..., ge=0),
    ) -> dict:
        """Evaluate a binary cell through the precision gate (test/debug)."""
        return queries.binary_cell_for_tests(successes, n).to_dict()

    dist = Path(__file__).resolve().parents[3] / "web" / "dist"
    if dist.is_dir():
        assets = dist / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/{full_path:path}")
        def spa(full_path: str, request: Request):
            del request
            if full_path.startswith("api/"):
                raise HTTPException(status_code=404)
            index = dist / "index.html"
            candidate = dist / full_path
            if full_path and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(index)

    return app
