"""Live agent presence API — the single source of truth for "what is running".

Presence is derived from two inputs that are merged into one authoritative set:

1. the watch daemon's ``presence.json`` (push, sub-second when it fires), and
2. a direct scan of transcript mtimes (pull, immune to a dead daemon, a
   coalesced FSEvent, or a worker that has been silent inside a long tool call).

Every panel in the dashboard reads this one payload.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request

from agentlog.api.deps import get_db_path
from agentlog.config import (
    PRESENCE_ACTIVE_SECONDS,
    PRESENCE_SCAN_WINDOW_SECONDS,
    PRESENCE_WORKING_GRACE_SECONDS,
    presence_path_for_db,
)
from agentlog.registry.harnesses import get_harness
from agentlog.session_identity import (
    build_identity_context,
    lineage_parent_ids,
    logical_orchestrator_id,
)
from agentlog.watch.presence import external_id_for_path, read_presence_file, session_id_for
from agentlog.watch.scan import (
    PEEK_CACHE,
    SCAN_CACHE,
    Peek,
    project_label,
    prompt_label,
    task_label,
)

router = APIRouter(tags=["live"])

_WORKING_STATES = frozenset({"streaming", "tool_running", "thinking", "orchestrating"})


@dataclass
class _Observation:
    harness: str
    external_id: str
    source_path: str
    seen_at: float
    mtime: float | None = None
    size: int | None = None
    rank: int = 0


def _utc_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _parse_iso(text: str | None) -> float | None:
    if not isinstance(text, str) or not text:
        return None
    value = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def _is_worker(source_path: str, external_id: str, parent_session_id: str | None) -> bool:
    if "/subagents/" in source_path or "\\subagents\\" in source_path:
        return True
    if "subagent" in external_id.lower():
        return True
    return bool(parent_session_id)


def _parent_external_id(source_path: str) -> str | None:
    path = Path(source_path)
    if path.parent.name != "subagents":
        return None
    return path.parent.parent.name or None


def _harness_display(harness: str) -> str:
    record = get_harness(harness)
    if record and record.get("display_name"):
        return str(record["display_name"])
    return harness


def _activity_text(state: str, peek: Peek, worker: bool) -> str:
    if state == "tool_running":
        return f"running {peek.tool}" if peek.tool else "running a tool"
    if state == "streaming":
        return "writing"
    if state == "thinking":
        return "picking up the task" if worker else "thinking"
    if state == "waiting":
        return "handed back" if worker else "waiting on you"
    return "no recent output"


def _load_db_meta(db_path: Path, ids: list[str]) -> dict[str, dict]:
    """Batch-resolve session rows + first user message for the scanned keys."""
    if not ids:
        return {}
    try:
        uri = Path(db_path).resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    except Exception:  # noqa: BLE001 - presence must survive a missing DB
        return {}
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout = 2000")
        identity = build_identity_context(conn)
        placeholders = ",".join("?" for _ in ids)
        rows = conn.execute(
            f"""
            SELECT s.id, s.repo, s.cwd, s.parent_session_id,
                   s.transcript_storage, s.source_sync_status,
                   a.path AS artifact_path
            FROM sessions s
            LEFT JOIN artifacts a ON a.id = s.artifact_id
            WHERE s.id IN ({placeholders})
            """,
            ids,
        ).fetchall()
        resolved_parents = lineage_parent_ids(conn)
        meta: dict[str, dict] = {
            str(row["id"]): {
                "session_id": str(row["id"]),
                "logical_session_id": logical_orchestrator_id(
                    conn, str(row["id"]), context=identity
                )
                or str(row["id"]),
                "logical_harness": (
                    "t3code"
                    if logical_orchestrator_id(
                        conn, str(row["id"]), context=identity
                    )
                    else None
                ),
                "repo": row["repo"] or row["cwd"],
                "parent_session_id": resolved_parents.get(str(row["id"])),
                "title": None,
                "transcript_storage": row["transcript_storage"],
                "source_sync_status": row["source_sync_status"],
                "artifact_path": row["artifact_path"],
            }
            for row in rows
        }
        logical_ids = sorted(
            {
                str(item["logical_session_id"])
                for item in meta.values()
                if item.get("logical_session_id")
            }
        )
        if logical_ids:
            logical_spots = ",".join("?" for _ in logical_ids)
            logical_rows = conn.execute(
                f"""
                SELECT s.id, s.transcript_storage, s.source_sync_status,
                       a.path AS artifact_path
                FROM sessions s
                LEFT JOIN artifacts a ON a.id = s.artifact_id
                WHERE s.id IN ({logical_spots})
                """,
                logical_ids,
            ).fetchall()
            readiness = {str(item["id"]): item for item in logical_rows}
            for item in meta.values():
                logical = readiness.get(str(item["logical_session_id"]))
                if logical is None:
                    continue
                item["readiness_storage"] = logical["transcript_storage"]
                item["readiness_sync_status"] = logical["source_sync_status"]
                item["readiness_artifact_path"] = logical["artifact_path"]
        if meta:
            found = list(meta)
            spots = ",".join("?" for _ in found)
            titles = conn.execute(
                f"""
                SELECT m.session_id AS sid, m.text AS text
                FROM messages m
                JOIN (
                    SELECT session_id, MIN(seq) AS seq
                    FROM messages
                    WHERE session_id IN ({spots}) AND role = 'user'
                      AND COALESCE(is_tool_plumbing, 0) = 0
                    GROUP BY session_id
                ) f ON f.session_id = m.session_id AND f.seq = m.seq
                """,
                found,
            ).fetchall()
            for row in titles:
                text = row["text"]
                if text:
                    meta[str(row["sid"])]["title"] = str(text).strip()[:400]
        return meta
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


def _source_snapshot_status(
    row: dict | None,
) -> str:
    """Expose whether a live source can be opened without serving a stale view."""
    if row is None:
        return "pending"
    storage = row.get("readiness_storage", row.get("transcript_storage"))
    if storage != "source_backed":
        return "stable"
    artifact_path = row.get("readiness_artifact_path", row.get("artifact_path"))
    if not row.get("logical_session_id") or not artifact_path:
        return "pending"
    sync_status = str(
        row.get("readiness_sync_status", row.get("source_sync_status")) or ""
    )
    if sync_status.startswith("frozen_") or sync_status in {"unavailable", "error"}:
        return "pending"
    try:
        source = Path(str(artifact_path))
        if not source.is_file():
            return "pending"
        with source.open("rb") as handle:
            handle.read(1)
    except OSError:
        return "pending"
    return "stable"


def live_payload(
    db_path: Path,
    *,
    presence_path: Path | None = None,
    now: float | None = None,
    scan: bool = True,
) -> dict:
    """Authoritative live-presence snapshot: daemon push merged with disk scan."""
    started = time.perf_counter()
    clock = now if now is not None else time.time()
    path = presence_path or presence_path_for_db(db_path)
    daemon = read_presence_file(path)
    daemon_ts = _parse_iso(daemon.get("ts"))

    observed: dict[str, _Observation] = {}

    for raw in daemon.get("sessions") or []:
        if not isinstance(raw, dict):
            continue
        harness = str(raw.get("harness") or "")
        external_id = str(raw.get("external_id") or "")
        source_path = str(raw.get("source_path") or "")
        if not harness or not external_id:
            continue
        seen_at = _parse_iso(raw.get("last_activity_at")) or clock
        observed[session_id_for(harness, external_id)] = _Observation(
            harness=harness,
            external_id=external_id,
            source_path=source_path,
            seen_at=seen_at,
        )

    if scan:
        rows = SCAN_CACHE.rows(now=clock, window_seconds=PRESENCE_SCAN_WINDOW_SECONDS)
        for harness, file_path, mtime, size in rows:
            external_id = external_id_for_path(harness, file_path)
            if not external_id:
                continue
            key = session_id_for(harness, external_id)
            prev = observed.get(key)
            rank = _path_rank(harness, file_path)
            if prev is None:
                observed[key] = _Observation(
                    harness=harness,
                    external_id=external_id,
                    source_path=str(file_path),
                    seen_at=mtime,
                    mtime=mtime,
                    size=size,
                    rank=rank,
                )
                continue
            # Cursor mirrors a stub of every chat under the empty-window
            # placeholder project; never let the stub shadow the real file.
            prev.seen_at = max(prev.seen_at, mtime)
            if (rank, mtime) >= (prev.rank, prev.mtime or 0.0):
                prev.source_path = str(file_path)
                prev.mtime = mtime
                prev.size = size
                prev.rank = rank

    meta = _load_db_meta(db_path, list(observed))

    candidates: dict[str, dict] = {}
    for key, obs in observed.items():
        harness = obs.harness
        external_id = obs.external_id
        source_path = obs.source_path
        seen_at = obs.seen_at
        age = max(0.0, clock - seen_at)
        if age > PRESENCE_SCAN_WINDOW_SECONDS:
            continue
        file_path = Path(source_path) if source_path else None
        if file_path is not None:
            try:
                st = file_path.stat()
                obs.size, obs.mtime = st.st_size, st.st_mtime
            except OSError:
                obs.size, obs.mtime = 0, seen_at
        peek = (
            PEEK_CACHE.get(file_path, obs.mtime or seen_at, obs.size or 0)
            if file_path is not None and obs.size
            else Peek()
        )
        state = peek.state
        fresh = age <= PRESENCE_ACTIVE_SECONDS
        # A mid-turn transcript that stopped growing means the agent is inside a
        # tool call the harness has not flushed yet — still working, just quiet.
        working_gap = (
            not fresh
            and peek.mid_turn
            and state in _WORKING_STATES
            and age <= PRESENCE_WORKING_GRACE_SECONDS
        )

        row = meta.get(key)
        worker = _is_worker(
            source_path, external_id, row["parent_session_id"] if row else None
        )
        # Path-derived slug beats the DB column: repo attribution can drift, the
        # transcript's own location cannot.
        repo = (external_id_repo(harness, file_path) if file_path else None) or (
            row["repo"] if row else None
        )
        project = project_label(repo)
        db_title = row["title"] if row else None
        task = task_label(db_title) or task_label(peek.brief)
        if worker:
            label = peek.step or task or f"{project or harness} worker"
        else:
            label = (
                prompt_label(db_title)
                or prompt_label(peek.brief)
                or peek.step
                or task
                or (project or external_id[-12:])
            )

        candidates[key] = {
            "live": fresh or working_gap,
            "harness": harness,
            "harness_display": _harness_display(harness),
            "external_id": external_id,
            "session_id": row["session_id"] if row else None,
            "logical_session_id": row["logical_session_id"] if row else None,
            "logical_harness": row["logical_harness"] if row else None,
            "parent_session_id": row["parent_session_id"] if row else None,
            "parent_external_id": _parent_external_id(source_path),
            "source_path": source_path,
            "state": state,
            "role": "worker" if worker else "session",
            "label": label,
            "task": task,
            "step": peek.step,
            "tool": peek.tool,
            "activity": _activity_text(state, peek, worker),
            "project": project,
            "working": state in _WORKING_STATES,
            "observed_gap_seconds": round(age, 1) if working_gap else 0.0,
            "last_activity_at": _utc_iso(seen_at),
            "age_seconds": round(age, 3),
            "pending_ingest": row is None,
            "source_snapshot_status": _source_snapshot_status(row),
            "title": db_title or task or label,
            "repo": repo,
        }

    sessions = _select_live(candidates)
    sessions.sort(key=lambda item: (item["role"] != "session", item["age_seconds"]))
    workers = sum(1 for item in sessions if item["role"] == "worker")
    return {
        "ts": _utc_iso(clock),
        "epoch": daemon.get("epoch"),
        "generation": daemon.get("generation", 0),
        "active_seconds": PRESENCE_ACTIVE_SECONDS,
        "working_grace_seconds": PRESENCE_WORKING_GRACE_SECONDS,
        "path": str(path),
        "watcher": {
            "presence_ts": daemon.get("ts"),
            "age_seconds": round(clock - daemon_ts, 1) if daemon_ts else None,
            "fresh": bool(daemon_ts and clock - daemon_ts <= 45),
        },
        "counts": {
            "total": len(sessions),
            "sessions": len(sessions) - workers,
            "workers": workers,
            "working": sum(1 for item in sessions if item["working"]),
        },
        "took_ms": round((time.perf_counter() - started) * 1000, 2),
        "sessions": sessions,
    }


def _select_live(candidates: dict[str, dict]) -> list[dict]:
    """Keep live rows, plus the parent chat of any live worker.

    A conversation whose last turn ended is idle on its own, but if it spawned
    workers that are still running it is orchestrating them and belongs on the
    rail — otherwise the rail shows workers with no owner.
    """
    live = {key: dict(item) for key, item in candidates.items() if item["live"]}
    parents: set[str] = set()
    for item in live.values():
        if item["role"] != "worker":
            continue
        parent_sid = item.get("parent_session_id")
        if isinstance(parent_sid, str) and parent_sid:
            # DB ids are already "harness:external"; path-derived ones are bare.
            parents.add(
                parent_sid
                if ":" in parent_sid
                else f"{item['harness']}:{parent_sid}"
            )
        parent_ext = item.get("parent_external_id")
        if isinstance(parent_ext, str) and parent_ext:
            parents.add(f"{item['harness']}:{parent_ext}")

    for key in parents:
        if key in live:
            continue
        parent = candidates.get(key)
        if parent is None:
            continue
        promoted = dict(parent)
        promoted["live"] = True
        promoted["state"] = "orchestrating"
        promoted["working"] = True
        promoted["observed_gap_seconds"] = 0.0
        live[key] = promoted

    for item in live.values():
        item["worker_count"] = (
            0
            if item["role"] == "worker"
            else sum(
                1
                for other in live.values()
                if other["role"] == "worker"
                and (
                    other.get("parent_external_id") == item["external_id"]
                    or other.get("parent_session_id")
                    in {item.get("session_id"), item["external_id"]}
                )
            )
        )
        if not item["worker_count"]:
            continue
        if item["state"] in {"waiting", "unknown", "orchestrating"}:
            item["state"] = "orchestrating"
            item["working"] = True
        plural = "" if item["worker_count"] == 1 else "s"
        item["activity"] = f"{item['worker_count']} worker{plural} running"
        # Prefer a readable ask over a leftover URL stub once workers own the turn.
        if item["label"] and item["label"].startswith(("http://", "https://")):
            item["label"] = item.get("project") or item["label"]
    return list(live.values())


_PLACEHOLDER_REPOS = frozenset({"", "unknown", "empty-window"})


def _path_rank(harness: str, path: Path) -> int:
    """1 for a transcript under a real project, 0 for a placeholder mirror."""
    return 0 if external_id_repo(harness, path) is None and harness == "cursor" else 1


def external_id_repo(harness: str, path: Path) -> str | None:
    """Repo slug straight from the transcript path, for un-ingested sessions."""
    if harness != "cursor":
        return None
    parts = path.parts
    if "projects" not in parts:
        return None
    index = parts.index("projects")
    slug = parts[index + 1] if len(parts) > index + 1 else None
    return None if slug in _PLACEHOLDER_REPOS else slug


@router.get("/api/live")
def live_sessions(request: Request) -> dict:
    """Currently-active agent sessions and workers."""
    db_path = get_db_path(request)
    return live_payload(db_path)
