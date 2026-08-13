"""Watcher / ingest health snapshot for API and CLI."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agentlog.config import (
    DEFAULT_DB_PATH,
    PRESENCE_HEARTBEAT_SECONDS,
    WATCHER_PRESENCE_STALE_SECONDS,
    presence_path_for_db,
)
from agentlog.watch.presence import read_presence_file


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _age_seconds(when: datetime | None, *, now: datetime | None = None) -> float | None:
    if when is None:
        return None
    clock = now or datetime.now(timezone.utc)
    return max(0.0, (clock - when).total_seconds())


def latest_ingest_by_harness(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT harness, MAX(ts) AS last_ts,
               SUM(sessions_added) AS sessions_added,
               SUM(sessions_updated) AS sessions_updated,
               SUM(messages_added) AS messages_added
        FROM ingest_events
        GROUP BY harness
        ORDER BY harness
        """
    ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        out[str(row["harness"])] = {
            "last_ts": row["last_ts"],
            "sessions_added": int(row["sessions_added"] or 0),
            "sessions_updated": int(row["sessions_updated"] or 0),
            "messages_added": int(row["messages_added"] or 0),
        }
    return out


def build_health(
    db_path: Path | None = None,
    *,
    conn: sqlite3.Connection | None = None,
    now: datetime | None = None,
    presence_stale_seconds: float | None = None,
    include_derived: bool = True,
) -> dict[str, Any]:
    """Return API health payload with watcher liveness and ingest freshness."""
    path = Path(db_path or DEFAULT_DB_PATH).expanduser()
    clock = now or datetime.now(timezone.utc)
    stale_after = (
        presence_stale_seconds
        if presence_stale_seconds is not None
        else WATCHER_PRESENCE_STALE_SECONDS
    )

    presence_path = presence_path_for_db(path)
    presence_exists = presence_path.is_file()
    presence = read_presence_file(presence_path) if presence_exists else {}
    presence_ts = _parse_ts(presence.get("ts")) if presence_exists else None
    # Prefer file mtime as heartbeat signal (daemon rewrites on heartbeat).
    mtime_ts: datetime | None = None
    if presence_exists:
        try:
            mtime_ts = datetime.fromtimestamp(
                presence_path.stat().st_mtime, tz=timezone.utc
            )
        except OSError:
            mtime_ts = None
    heartbeat_ts = mtime_ts or presence_ts
    presence_age = _age_seconds(heartbeat_ts, now=clock)
    presence_fresh = presence_age is not None and presence_age <= stale_after

    owns_conn = conn is None
    if conn is None:
        try:
            uri = path.resolve().as_uri() + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True)
            conn.row_factory = sqlite3.Row
        except (OSError, sqlite3.Error):
            conn = None

    last_by_harness: dict[str, dict[str, Any]] = {}
    last_ingest_at: str | None = None
    derived: dict[str, Any] | None = None
    db_ok = False
    if conn is not None:
        try:
            last_by_harness = latest_ingest_by_harness(conn)
            db_ok = True
            times = [
                _parse_ts(v.get("last_ts"))
                for v in last_by_harness.values()
                if v.get("last_ts")
            ]
            times = [t for t in times if t is not None]
            if times:
                last_ingest_at = max(times).isoformat()
            if include_derived:
                from agentlog.analysis.derive import derived_freshness

                derived = derived_freshness(conn)
        except sqlite3.Error:
            db_ok = False
        finally:
            if owns_conn:
                conn.close()

    watcher_alive = bool(presence_exists and presence_fresh)
    reasons: list[str] = []
    if not presence_exists:
        reasons.append("presence file missing (watcher not running or never started)")
    elif not presence_fresh:
        age_txt = f"{presence_age:.0f}s" if presence_age is not None else "unknown"
        reasons.append(
            f"presence stale ({age_txt} since last heartbeat; "
            f"threshold {stale_after:.0f}s, heartbeat every "
            f"{PRESENCE_HEARTBEAT_SECONDS:.0f}s)"
        )
    if not db_ok:
        reasons.append("database unreachable")
    elif not last_by_harness and not watcher_alive:
        # Catch-up may still be running when the watcher is freshly alive.
        reasons.append("no ingest_events recorded yet")

    degraded = bool(reasons)

    return {
        "ok": True,
        "db": str(path),
        "degraded": degraded,
        "reason": "; ".join(reasons) if reasons else None,
        "watcher": {
            "alive": watcher_alive,
            "presence_path": str(presence_path),
            "presence_exists": presence_exists,
            "presence_ts": presence_ts.isoformat() if presence_ts else None,
            "presence_age_seconds": (
                round(presence_age, 3) if presence_age is not None else None
            ),
            "presence_fresh": presence_fresh,
            "stale_after_seconds": stale_after,
        },
        "last_ingest_at": last_ingest_at,
        "last_ingest_by_harness": last_by_harness,
        "derived": derived,
    }
