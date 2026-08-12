from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from agentlog.analysis.attention import AttentionState, attention_payload
from agentlog.api import (
    activity as activity_api,
    adjudication as adjudication_api,
    attribution as attribution_api,
    briefs as briefs_api,
    descriptive,
    events as events_api,
    graph as graph_api,
    harnesses,
    live as live_api,
    proposals as proposals_api,
    queries,
    skills as skills_api,
    tokens,
    usage as usage_api,
)
from agentlog.api.deps import WRITE_BUSY_TIMEOUT_MS, get_conn
from agentlog.api.local_token import inject_spa_token
from agentlog.api.ranges import TimeRange, parse_range, range_params
from agentlog.api.security import SecurityConfig, install_security
from agentlog.config import DEFAULT_DB_PATH
from agentlog.normalize.model_identity import repair_null_model_identity
from agentlog.source_reader import read_source_transcript

log = logging.getLogger("agentlog.api")


def _parse_range_dep(
    range: str = Query("30d", alias="range"),
    start: str | None = None,
    end: str | None = None,
) -> TimeRange:
    try:
        return parse_range(range, custom_start=start, custom_end=end)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _startup_repair_model_identity(db_path: Path) -> None:
    """Heal rows wiped by older writers that omitted model_canonical."""
    if not db_path.is_file():
        return
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(f"PRAGMA busy_timeout = {WRITE_BUSY_TIMEOUT_MS}")
        # Sessions only at startup — keeps the API responsive under WAL load.
        n = repair_null_model_identity(
            conn, tables=("sessions",), include_token_usage=False
        )
        if n:
            log.info("repaired model identity on %s session rows", n)
    except sqlite3.Error as exc:
        log.warning("model identity startup repair skipped: %s", exc)
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
    finally:
        conn.close()


def create_app(
    db_path: Path | None = None,
    *,
    security: SecurityConfig | None = None,
    dist_dir: Path | None = None,
    adjudication_window_ids: Iterable[str] | None = None,
) -> FastAPI:
    path = Path(db_path or DEFAULT_DB_PATH)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        _startup_repair_model_identity(Path(app.state.db_path))
        yield

    app = FastAPI(title="agentlog", version="0.1.0", lifespan=lifespan)
    app.state.db_path = path
    configured_adjudications = (
        (adjudication_window_ids,)
        if isinstance(adjudication_window_ids, str)
        else (adjudication_window_ids or ())
    )
    app.state.adjudication_window_ids = tuple(
        dict.fromkeys(
            window_id.strip()
            for value in configured_adjudications
            if (window_id := str(value)).strip()
        )
    )

    sec = security or SecurityConfig()
    # Security first (inner), CORS last (outer). Deny responses from the
    # trust boundary must still receive ACAO headers; otherwise a browser
    # on an allowed dashboard origin sees TypeError "Failed to fetch"
    # instead of the 401/403 body when the Bearer token is missing.
    install_security(app, sec)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=sorted(sec.allowed_origins),
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    @app.get("/api/health")
    def health() -> dict:
        from agentlog.service.health import build_health

        return build_health(app.state.db_path)

    @app.get("/api/harnesses")
    def harness_registry(
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict:
        return harnesses.harness_matrix(conn)

    @app.get("/api/attention")
    def attention_inbox(
        conn: sqlite3.Connection = Depends(get_conn),
        state: str | None = Query(None),
    ) -> dict:
        allowed: dict[str, AttentionState] = {
            "live_waiting": "live_waiting",
            "live_error": "live_error",
            "waiting_on_user": "waiting_on_user",
            "error_streak": "error_streak",
            "open_task": "open_task",
            "long_running": "long_running",
            "resumable": "resumable",
        }
        selected: AttentionState | None = None
        if state is not None:
            if state not in allowed:
                raise HTTPException(
                    status_code=400,
                    detail=f"state must be one of {sorted(allowed)}",
                )
            selected = allowed[state]
        return attention_payload(conn, state=selected)

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
        prev_ledger: dict | None = None
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
            prev_ledger = descriptive.ledger_counts(conn, prev_tr)

        lead = queries.semantic_lead_metric(conn, tr)
        streak = queries.streak_days(conn, tr)
        ledger = descriptive.ledger_counts(conn, tr)
        token_totals = tokens.corpus_totals(conn, tr)

        def _count_kpi(key: str, label: str) -> dict:
            value = ledger[key]
            previous = prev_ledger[key] if prev_ledger is not None else None
            ratio = (
                (value - previous) / previous
                if previous is not None and previous > 0
                else None
            )
            return {
                "value": value,
                "previous": previous,
                "delta_ratio": ratio,
                "kind": "count",
                "label": label,
            }
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
                "messages": _count_kpi("messages", "Messages"),
                "tool_events": _count_kpi("tool_events", "Tool calls"),
                "windows": _count_kpi("windows", "Exchange windows"),
                "auto_reviews": _count_kpi("auto_reviews", "Auto-reviews"),
                "tokens_est": token_totals,
                "cost_est": token_totals.get("cost"),
                "interaction_style": lead.to_dict(),
                "streak": {
                    "current_days": streak["current"],
                    "longest_days": streak["longest"],
                    "label": "Active days in range",
                    "note": "Calendar days with at least one session start.",
                },
            },
            "ledger": ledger,
            "flags": lead.flags,
        }

    @app.get("/api/timeseries/sessions")
    def timeseries_sessions(
        conn: sqlite3.Connection = Depends(get_conn),
        tr: TimeRange = Depends(_parse_range_dep),
        by: str = Query("harness"),
    ) -> dict:
        if by not in {"harness", "model"}:
            raise HTTPException(
                status_code=400, detail="by must be harness or model"
            )
        series = descriptive.sessions_daily_by(conn, tr, by=by)
        return {
            **range_params(tr),
            "by": by,
            "series": series,
            "note": f"Daily session counts by {by}. Descriptive usage only.",
        }

    @app.get("/api/models")
    def models(
        conn: sqlite3.Connection = Depends(get_conn),
        tr: TimeRange = Depends(_parse_range_dep),
    ) -> dict:
        return {**range_params(tr), **queries.models_profile(conn, tr)}

    @app.get("/api/models/monthly")
    def models_monthly(
        conn: sqlite3.Connection = Depends(get_conn),
        tr: TimeRange = Depends(_parse_range_dep),
    ) -> dict:
        return {**range_params(tr), **descriptive.model_monthly_mix(conn, tr)}

    @app.get("/api/tools")
    def tools(
        conn: sqlite3.Connection = Depends(get_conn),
        tr: TimeRange = Depends(_parse_range_dep),
        limit: int = Query(40, ge=1, le=200),
    ) -> dict:
        return {**range_params(tr), **descriptive.tool_usage(conn, tr, limit=limit)}

    @app.get("/api/request-kinds")
    def request_kinds(
        conn: sqlite3.Connection = Depends(get_conn),
        tr: TimeRange = Depends(_parse_range_dep),
    ) -> dict:
        return {
            **range_params(tr),
            **descriptive.request_kind_distribution(conn, tr),
        }

    @app.get("/api/distributions")
    def distributions(
        conn: sqlite3.Connection = Depends(get_conn),
        tr: TimeRange = Depends(_parse_range_dep),
    ) -> dict:
        return {
            **range_params(tr),
            **descriptive.duration_and_volume(conn, tr),
        }

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

    @app.get("/api/facets")
    def facets(
        conn: sqlite3.Connection = Depends(get_conn),
        tr: TimeRange = Depends(_parse_range_dep),
        view: str | None = Query(None),
    ) -> dict:
        if view not in {None, "roots"}:
            raise HTTPException(status_code=400, detail="view must be roots")
        return {
            **range_params(tr),
            **descriptive.session_facets(conn, tr, view=view),
        }

    @app.get("/api/search")
    def search(
        conn: sqlite3.Connection = Depends(get_conn),
        tr: TimeRange = Depends(_parse_range_dep),
        q: str = Query("", min_length=0),
        harness: list[str] | None = Query(None),
        model: list[str] | None = Query(None),
        project: list[str] | None = Query(None),
        cursor: int = 0,
        limit: int = Query(40, ge=1, le=100),
    ) -> dict:
        data = descriptive.search_messages(
            conn,
            tr,
            q=q,
            harness=harness,
            model=model,
            project=project,
            cursor=cursor,
            limit=limit,
            source_reader=read_source_transcript,
        )
        return {**range_params(tr), **data}

    @app.get("/api/sessions")
    def sessions(
        conn: sqlite3.Connection = Depends(get_conn),
        tr: TimeRange = Depends(_parse_range_dep),
        harness: list[str] | None = Query(None),
        model: list[str] | None = Query(None),
        effort: list[str] | None = Query(None),
        branch: list[str] | None = Query(None),
        project: list[str] | None = Query(None),
        q: str | None = None,
        sort: str = Query("started_at"),
        order: str = Query("desc"),
        cursor: int = 0,
        limit: int = Query(50, ge=1, le=200),
    ) -> dict:
        data = descriptive.list_sessions_v2(
            conn,
            tr,
            harness=harness,
            model=model,
            effort=effort,
            branch=branch,
            project=project,
            q=q,
            sort=sort,
            order=order,
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

    @app.get("/api/sessions/{session_id}/tree")
    def session_tree(
        session_id: str,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict:
        tree = descriptive.orchestration_tree(conn, session_id)
        if tree is None:
            raise HTTPException(status_code=404, detail="session not found")
        return tree

    @app.get("/api/sessions/{session_id}")
    def session(
        session_id: str,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict:
        detail = descriptive.session_detail_v2(conn, session_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="session not found")
        return detail

    @app.get("/api/orchestration")
    def orchestration(
        conn: sqlite3.Connection = Depends(get_conn),
        tr: TimeRange = Depends(_parse_range_dep),
        limit: int = Query(40, ge=1, le=100),
    ) -> dict:
        return {
            **range_params(tr),
            **descriptive.orchestration_overview(conn, tr, limit=limit),
        }

    @app.get("/api/auto-review")
    def auto_review(
        conn: sqlite3.Connection = Depends(get_conn),
        tr: TimeRange = Depends(_parse_range_dep),
        limit: int = Query(50, ge=1, le=200),
    ) -> dict:
        return {
            **range_params(tr),
            **descriptive.auto_review_surface(conn, tr, limit=limit),
        }

    app.include_router(skills_api.router)
    app.include_router(briefs_api.router)
    app.include_router(events_api.router)
    app.include_router(live_api.router)
    app.include_router(graph_api.router)
    app.include_router(adjudication_api.router)
    app.include_router(attribution_api.router)
    app.include_router(usage_api.router)
    app.include_router(activity_api.router)
    app.include_router(proposals_api.router)

    @app.get("/api/tokens")
    def tokens_summary(
        conn: sqlite3.Connection = Depends(get_conn),
        tr: TimeRange = Depends(_parse_range_dep),
    ) -> dict:
        return {**range_params(tr), **tokens.corpus_totals(conn, tr)}

    @app.get("/api/tokens/by-harness")
    def tokens_by_harness(
        conn: sqlite3.Connection = Depends(get_conn),
        tr: TimeRange = Depends(_parse_range_dep),
    ) -> dict:
        return {**range_params(tr), **tokens.by_harness(conn, tr)}

    @app.get("/api/tokens/by-model")
    def tokens_by_model(
        conn: sqlite3.Connection = Depends(get_conn),
        tr: TimeRange = Depends(_parse_range_dep),
    ) -> dict:
        return {**range_params(tr), **tokens.by_model(conn, tr)}

    @app.get("/api/tokens/timeseries")
    def tokens_timeseries(
        conn: sqlite3.Connection = Depends(get_conn),
        tr: TimeRange = Depends(_parse_range_dep),
    ) -> dict:
        return {**range_params(tr), **tokens.timeseries_daily(conn, tr)}

    @app.get("/api/sessions/{session_id}/tokens")
    def session_tokens(
        session_id: str,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict:
        detail = tokens.session_tokens(conn, session_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="session not found")
        return detail

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

    # Fallback routes for session ids that contain "/" (e.g. cursor ids like
    # "cursor:<project>/subagent:<uuid>"). The single-segment routes above
    # cannot match those; these path-converter twins are registered last so
    # they never shadow /recent, /tree, or /tokens for normal ids.
    @app.get("/api/sessions/{session_id:path}/tree")
    def session_tree_path(
        session_id: str,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict:
        tree = descriptive.orchestration_tree(conn, session_id)
        if tree is None:
            raise HTTPException(status_code=404, detail="session not found")
        return tree

    @app.get("/api/sessions/{session_id:path}")
    def session_path(
        session_id: str,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict:
        detail = descriptive.session_detail_v2(conn, session_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="session not found")
        return detail

    dist = Path(dist_dir) if dist_dir is not None else (
        Path(__file__).resolve().parents[3] / "web" / "dist"
    )
    if dist.is_dir():
        assets = dist / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/{full_path:path}")
        def spa(full_path: str, request: Request):
            if full_path.startswith("api/"):
                raise HTTPException(status_code=404)
            index = dist / "index.html"
            candidate = dist / full_path
            if (
                full_path
                and candidate.is_file()
                and candidate.resolve() != index.resolve()
            ):
                return FileResponse(candidate)
            html = index.read_text(encoding="utf-8")
            sec = getattr(request.app.state, "security", None)
            token_value = getattr(sec, "token", None) if sec is not None else None
            if token_value:
                html = inject_spa_token(html, token_value)
            return HTMLResponse(html)

    return app
