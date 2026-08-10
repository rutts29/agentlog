"""Ingest change feed for the dashboard (poll + SSE)."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from agentlog.api.deps import get_conn, get_db_path
from agentlog.api.live import live_payload
from agentlog.config import presence_path_for_db
from agentlog.watch.events import list_ingest_events

router = APIRouter(tags=["events"])


def _parse_since(since: str | None) -> str | None:
    if since is None or since == "":
        return None
    text = since.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        datetime.fromisoformat(text)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail="since must be an ISO-8601 timestamp"
        ) from exc
    return since.strip()


def _open_ro(db_path: Path) -> sqlite3.Connection:
    uri = db_path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _presence_fingerprint(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


def _presence_keys(sessions: list[dict]) -> set[str]:
    keys: set[str] = set()
    for s in sessions:
        sid = s.get("session_id")
        if sid:
            keys.add(str(sid))
            continue
        harness = s.get("harness")
        external_id = s.get("external_id")
        if harness and external_id:
            keys.add(f"{harness}:{external_id}")
    return keys


def iter_event_sse(
    db_path: Path,
    *,
    since: str | None = None,
    poll_seconds: float = 1.0,
    max_cycles: int | None = None,
    presence_path: Path | None = None,
) -> Iterator[str]:
    """Yield SSE frames for new ingest_events rows and presence transitions."""
    last_id = 0
    bootstrap = since is not None
    cycles = 0
    pres_path = presence_path or presence_path_for_db(db_path)
    last_pres = _presence_fingerprint(pres_path)
    prev_keys: set[str] = set()
    # Seed previous keys without emitting so the first change is a real transition.
    try:
        seed = live_payload(db_path, presence_path=pres_path)
        prev_keys = _presence_keys(list(seed.get("sessions") or []))
    except Exception:  # noqa: BLE001
        prev_keys = set()
    yield ": connected\n\n"
    while True:
        try:
            conn = _open_ro(db_path)
            try:
                if bootstrap:
                    rows = list_ingest_events(conn, since=since, limit=200)
                    bootstrap = False
                else:
                    rows = list_ingest_events(conn, after_id=last_id, limit=200)
            finally:
                conn.close()
        except sqlite3.Error as exc:
            yield f"event: error\ndata: {json.dumps({'error': str(exc)})}\n\n"
            rows = []
        for event in rows:
            last_id = max(last_id, event.id)
            payload = json.dumps(event.to_dict(), separators=(",", ":"))
            yield f"event: ingest\ndata: {payload}\n\n"

        fp = _presence_fingerprint(pres_path)
        if fp != last_pres:
            last_pres = fp
            try:
                live = live_payload(db_path, presence_path=pres_path)
                sessions = list(live.get("sessions") or [])
                keys = _presence_keys(sessions)
                transitions = [
                    {"action": "active", "key": k} for k in sorted(keys - prev_keys)
                ] + [
                    {"action": "idle", "key": k} for k in sorted(prev_keys - keys)
                ]
                prev_keys = keys
                body = {
                    "ts": live.get("ts"),
                    "generation": live.get("generation", 0),
                    "sessions": sessions,
                    "transitions": transitions,
                }
                yield (
                    "event: presence\n"
                    f"data: {json.dumps(body, separators=(',', ':'))}\n\n"
                )
            except Exception as exc:  # noqa: BLE001
                yield (
                    "event: error\n"
                    f"data: {json.dumps({'error': str(exc)})}\n\n"
                )

        cycles += 1
        if max_cycles is not None and cycles >= max_cycles:
            break
        time.sleep(poll_seconds)


@router.get("/api/events")
def events_list(
    conn: sqlite3.Connection = Depends(get_conn),
    since: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
) -> dict:
    since_iso = _parse_since(since)
    items = list_ingest_events(conn, since=since_iso, limit=limit)
    return {"items": [e.to_dict() for e in items]}


@router.get("/api/events/stream")
def events_stream(
    request: Request,
    since: str | None = Query(None),
) -> StreamingResponse:
    since_iso = _parse_since(since)
    db_path = get_db_path(request)
    return StreamingResponse(
        iter_event_sse(db_path, since=since_iso),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
