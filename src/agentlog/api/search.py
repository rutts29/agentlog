"""Dual-mode message search for materialized and source-backed sessions."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable, Mapping
from pathlib import Path
from threading import Event
from typing import Any, Protocol

from agentlog.api.identity_aggregates import (
    VisibleLogicalSession,
    visible_logical_sessions,
)
from agentlog.api.ranges import TimeRange, session_time_clause
from agentlog.ingest.base import is_sqlite_path
from agentlog.normalize.model_identity import display_model

_FTS_TOKEN_RE = re.compile(r"[A-Za-z0-9_./+-]+")
_JSONL_PREFILTER_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+\Z")
DEFAULT_SOURCE_SCAN_LIMIT = 200
MAX_SOURCE_SCAN_LIMIT = 1000
DEFAULT_SOURCE_SCAN_WORKERS = 1


class SourceReader(Protocol):
    """Read-only source contract; ``read_source_transcript`` is the canonical form."""

    def __call__(self, conn: sqlite3.Connection, session_id: str) -> Any: ...


def fts_match_query(raw: str) -> str | None:
    tokens = _FTS_TOKEN_RE.findall(raw or "")
    if not tokens:
        return None
    return " AND ".join(f'"{token}"' for token in tokens[:24])


def _value(row: sqlite3.Row, *names: str) -> Any:
    keys = set(row.keys())
    for name in names:
        if name in keys:
            return row[name]
    return None


def is_source_backed_session(row: sqlite3.Row) -> bool:
    """Classify only explicit source markers as source-backed.

    Older databases have no marker and remain materialized. This avoids
    silently changing their search behavior during the forward-only cutover.
    """
    return _value(row, "transcript_storage") == "source_backed"


def _project_label(repo: str | None, cwd: str | None) -> str:
    if repo:
        text = repo.strip()
        if "/" in text:
            return text.rstrip("/").split("/")[-1].removesuffix(".git") or text
        return text
    if cwd:
        return cwd.rstrip("/").split("/")[-1] or "(unknown)"
    return "(unknown)"


def _metadata_matches(row: sqlite3.Row, query: str) -> bool:
    needle = query.casefold()
    return any(
        needle in str(_value(row, name) or "").casefold()
        for name in ("id", "external_id", "repo", "cwd", "branch", "model", "model_canonical", "provider")
    )


def _filter_sessions(
    sessions: Iterable[VisibleLogicalSession],
    metrics: Mapping[str, sqlite3.Row],
    *,
    harness: list[str] | None,
    model: list[str] | None,
    project: list[str] | None,
) -> list[VisibleLogicalSession]:
    result: list[VisibleLogicalSession] = []
    for session in sessions:
        if harness and session.logical_harness not in harness:
            continue
        metric = metrics.get(session.metric_session_id)
        source_model = str(metric["model_canonical"] or "(unknown)") if metric else "(unknown)"
        if model and source_model not in model:
            continue
        label = _project_label(session.row["repo"], session.row["cwd"])
        if project and label not in project:
            continue
        result.append(session)
    return result


def _metric_rows(conn: sqlite3.Connection, sessions: Iterable[VisibleLogicalSession]) -> dict[str, sqlite3.Row]:
    ids = sorted({session.metric_session_id for session in sessions})
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT id, model_canonical, transcript_storage FROM sessions WHERE id IN ({placeholders})", ids
    ).fetchall()
    return {str(row["id"]): row for row in rows}


def _transcript_storage(
    session: VisibleLogicalSession, metrics: Mapping[str, sqlite3.Row]
) -> str:
    metric = metrics.get(session.metric_session_id)
    if metric is not None:
        return str(metric["transcript_storage"] or "legacy_materialized")
    return str(session.row["transcript_storage"] or "legacy_materialized")


def _item_key(item: Mapping[str, Any]) -> tuple[str, str]:
    logical = str(
        item.get("logical_session_id")
        or item.get("transcript_session_id")
        or item.get("session_id")
        or ""
    )
    locator = item.get("message_locator") or item.get("message_id")
    if locator is None:
        locator = f"seq:{item.get('seq')}"
    return logical, str(locator)


def _source_item(
    hit: Mapping[str, Any],
    sessions: Mapping[str, VisibleLogicalSession],
    metrics: Mapping[str, sqlite3.Row],
) -> dict[str, Any] | None:
    physical_id = str(hit.get("session_id") or hit.get("source_session_id") or "")
    session = sessions.get(physical_id)
    if session is None:
        return None
    metric = metrics.get(session.metric_session_id)
    locator = hit.get("message_locator") or hit.get("message_id")
    if locator is None and hit.get("seq") is not None:
        locator = f"seq:{hit['seq']}"
    item = {
        "message_id": hit.get("message_id") or hit.get("id"),
        "message_locator": locator,
        "session_id": session.session_id,
        "physical_session_id": physical_id,
        "seq": hit.get("seq"),
        "role": hit.get("role"),
        "timestamp": hit.get("timestamp"),
        "snippet": hit.get("snippet") or hit.get("text") or "",
        "harness": session.logical_harness,
        "runtime_harness": session.runtime_harness,
        "orchestrator_session_id": session.orchestrator_session_id,
        "transcript_session_id": session.metric_session_id,
        "model": display_model(str(metric["model_canonical"] or "(unknown)") if metric else "(unknown)"),
        "effort": session.row["effort"],
        "project": _project_label(session.row["repo"], session.row["cwd"]),
        "started_at": session.row["started_at"],
        "provenance": {
            "mode": "source_scan",
            "session_storage": "source_backed",
            "message_locator": locator,
        },
    }
    return item


def _reader_result(
    source_reader: SourceReader | Any,
    conn: sqlite3.Connection,
    session_id: str,
) -> tuple[list[Mapping[str, Any]], Mapping[str, Any]]:
    reader_method = getattr(source_reader, "read_source_transcript", None)
    if reader_method is not None:
        result = reader_method(conn, session_id)
    elif callable(source_reader):
        result = source_reader(conn, session_id)
    else:
        raise TypeError("source_reader must be read_source_transcript(conn, session_id)")
    if isinstance(result, Mapping):
        messages = result.get("messages") or []
        metadata = result
    else:
        messages = getattr(result, "messages", []) or []
        metadata = {
            "status": getattr(result, "status", None),
            "source_identity": getattr(result, "source_identity", None),
            "source_hash": getattr(result, "source_hash", None),
            "warning": getattr(result, "warning", None),
        }
    return [message for message in messages if isinstance(message, Mapping)], metadata


def _scan_source_session(
    source_reader: SourceReader | Any,
    conn: sqlite3.Connection,
    session_id: str,
    query_tokens: list[str],
    *,
    max_messages: int,
    cancelled: Event | None = None,
) -> tuple[list[Mapping[str, Any]], Mapping[str, Any], bool, bool]:
    if cancelled is not None and cancelled.is_set():
        return [], {}, False, True
    messages, metadata = _reader_result(source_reader, conn, session_id)
    if cancelled is not None and cancelled.is_set():
        return [], metadata, False, True
    if str(metadata.get("status") or "") != "ready":
        return [], metadata, False, False
    truncated = len(messages) > max_messages
    hits: list[Mapping[str, Any]] = []
    for message in messages[:max_messages]:
        if cancelled is not None and cancelled.is_set():
            return [], metadata, truncated, True
        text = str(message.get("text") or "")
        folded = text.casefold()
        if not all(token.casefold() in folded for token in query_tokens):
            continue
        match = re.search(
            "|".join(re.escape(token) for token in query_tokens), text, re.IGNORECASE
        )
        if match is None:
            continue
        start = max(0, match.start() - 120)
        end = min(len(text), match.end() + 200)
        snippet = text[start:end]
        snippet = re.sub(
            "|".join(re.escape(token) for token in query_tokens),
            lambda found: f"«{found.group(0)}»",
            snippet,
            flags=re.IGNORECASE,
        )
        if start:
            snippet = "…" + snippet
        if end < len(text):
            snippet += "…"
        hits.append(
            {
                **message,
                "session_id": session_id,
                "message_locator": message.get("id") or f"seq:{message.get('seq')}",
                "snippet": snippet,
                "source_status": metadata.get("status"),
                "source_identity": metadata.get("source_identity"),
                "source_hash": metadata.get("source_hash"),
            }
        )
    return hits, metadata, truncated, False


def _jsonl_might_contain_tokens(
    path: Path,
    query_tokens: list[str],
    *,
    cancelled: Event | None,
) -> bool | None:
    """Return False only when an ASCII query cannot occur in a stable JSONL file."""
    try:
        encoded = {token.lower().encode("ascii") for token in query_tokens}
    except UnicodeEncodeError:
        return True
    if any(_JSONL_PREFILTER_TOKEN_RE.fullmatch(token) is None for token in query_tokens):
        return True
    if not encoded or is_sqlite_path(path):
        return True
    try:
        before = path.stat()
        found: set[bytes] = set()
        carry = b""
        saw_unicode_escape = False
        overlap = max(map(len, encoded)) - 1
        carry_len = max(overlap, 1)
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                if cancelled is not None and cancelled.is_set():
                    return None
                folded = (carry + chunk).lower()
                saw_unicode_escape = saw_unicode_escape or b"\\u" in folded
                found.update(token for token in encoded - found if token in folded)
                if found == encoded:
                    return True
                carry = folded[-carry_len:]
        after = path.stat()
    except OSError:
        return True
    identity = lambda stat: (
        stat.st_dev,
        stat.st_ino,
        stat.st_size,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
    )
    if identity(before) != identity(after):
        return True
    if saw_unicode_escape:
        return True
    return False


def _source_candidate_ids(
    conn: sqlite3.Connection,
    sessions: list[VisibleLogicalSession],
    metrics: Mapping[str, sqlite3.Row],
    query_tokens: list[str],
    *,
    cancelled: Event | None,
) -> set[str] | None:
    source_ids = sorted({
        session.metric_session_id
        if metrics.get(session.metric_session_id) is not None
        and metrics[session.metric_session_id]["transcript_storage"] == "source_backed"
        else session.session_id
        for session in sessions
    })
    if not source_ids:
        return set()
    placeholders = ",".join("?" for _ in source_ids)
    rows = conn.execute(
        f"""
        SELECT s.id, a.path
        FROM sessions s
        LEFT JOIN artifacts a ON a.id = s.artifact_id
        WHERE s.id IN ({placeholders})
        """,
        source_ids,
    ).fetchall()
    candidates: set[str] = set()
    for row in rows:
        if cancelled is not None and cancelled.is_set():
            return None
        path = Path(str(row["path"])) if row["path"] else None
        if path is None or _jsonl_might_contain_tokens(
            path, query_tokens, cancelled=cancelled
        ) is not False:
            candidates.add(str(row["id"]))
    return candidates


def _scan_source_sessions(
    source_reader: SourceReader | Any,
    conn: sqlite3.Connection,
    sessions: list[VisibleLogicalSession],
    metrics: Mapping[str, sqlite3.Row],
    query_tokens: list[str],
    *,
    workers: int,
    cancelled: Event | None,
) -> list[tuple[VisibleLogicalSession, list[Mapping[str, Any]], Mapping[str, Any], bool]]:
    def scan(scan_conn: sqlite3.Connection, session: VisibleLogicalSession):
        metric = metrics.get(session.metric_session_id)
        source_id = (
            session.metric_session_id
            if metric is not None and metric["transcript_storage"] == "source_backed"
            else session.session_id
        )
        hits, metadata, message_truncated, scan_cancelled = _scan_source_session(
            source_reader,
            scan_conn,
            source_id,
            query_tokens,
            max_messages=5000,
            cancelled=cancelled,
        )
        if scan_cancelled:
            return None
        return session, hits, metadata, message_truncated

    if workers != 1:
        raise ValueError("source scan workers must be one to avoid competing with metadata requests")
    candidate_ids = _source_candidate_ids(
        conn, sessions, metrics, query_tokens, cancelled=cancelled
    )
    if candidate_ids is None:
        return []
    results = []
    for session in sessions:
        if cancelled is not None and cancelled.is_set():
            break
        metric = metrics.get(session.metric_session_id)
        source_id = (
            session.metric_session_id
            if metric is not None and metric["transcript_storage"] == "source_backed"
            else session.session_id
        )
        if source_id not in candidate_ids:
            continue
        scanned = scan(conn, session)
        if scanned is None:
            break
        results.append(scanned)
    return results


def search_messages(
    conn: sqlite3.Connection,
    tr: TimeRange,
    *,
    q: str,
    harness: list[str] | None = None,
    model: list[str] | None = None,
    project: list[str] | None = None,
    cursor: int = 0,
    limit: int = 40,
    source_reader: SourceReader | None = None,
    source_scan_limit: int = DEFAULT_SOURCE_SCAN_LIMIT,
    source_scan_workers: int = DEFAULT_SOURCE_SCAN_WORKERS,
    cancelled: Event | None = None,
) -> dict[str, Any]:
    match = fts_match_query(q)
    if match is None:
        return {
            "q": q,
            "total": 0,
            "cursor": 0,
            "next_cursor": None,
            "items": [],
            "note": "Enter a search term to query materialized messages.",
        }
    where, range_params = session_time_clause(tr)
    rows = conn.execute(f"SELECT s.* FROM sessions s WHERE {where}", range_params).fetchall()
    visible = visible_logical_sessions(conn, rows)
    metrics = _metric_rows(conn, visible)
    visible = _filter_sessions(visible, metrics, harness=harness, model=model, project=project)
    by_id = {session.session_id: session for session in visible}
    visible_by_metric: dict[str, VisibleLogicalSession] = {}
    for session in visible:
        visible_by_metric.setdefault(session.metric_session_id, session)
    source_sessions = [
        session
        for session in visible
        if _transcript_storage(session, metrics) == "source_backed"
    ]
    for session in source_sessions:
        by_id[session.metric_session_id] = session
    source_physical = {session.session_id for session in source_sessions}
    source_metric = {session.metric_session_id for session in source_sessions}
    materialized_metric_ids = sorted(
        {session.metric_session_id for session in visible} - source_physical - source_metric
    )

    items: list[dict[str, Any]] = []
    if materialized_metric_ids:
        params: dict[str, Any] = {"match": match, "fetch_n": min(2000, max(limit * 5, cursor + limit + 200))}
        metric_ph = ",".join(f":metric{i}" for i in range(len(materialized_metric_ids)))
        for index, session_id in enumerate(materialized_metric_ids):
            params[f"metric{index}"] = session_id
        rows = conn.execute(
            f"""
            SELECT m.id AS message_id, m.session_id, m.seq, m.role, m.timestamp,
                   snippet(messages_fts, 0, '«', '»', '…', 18) AS snippet,
                   bm25(messages_fts) AS rank
            FROM messages_fts
            JOIN messages m ON m.rowid = messages_fts.rowid
            WHERE messages_fts MATCH :match AND m.session_id IN ({metric_ph})
            ORDER BY rank LIMIT :fetch_n
            """,
            params,
        ).fetchall()
        for row in rows:
            session = visible_by_metric.get(str(row["session_id"]))
            if session is None:
                continue
            item = {
                "message_id": row["message_id"],
                "message_locator": row["message_id"],
                "session_id": session.session_id,
                "physical_session_id": row["session_id"],
                "seq": row["seq"],
                "role": row["role"],
                "timestamp": row["timestamp"],
                "snippet": row["snippet"],
                "harness": session.logical_harness,
                "runtime_harness": session.runtime_harness,
                "orchestrator_session_id": session.orchestrator_session_id,
                "transcript_session_id": session.metric_session_id,
                "model": display_model(str(metrics.get(session.metric_session_id)["model_canonical"] or "(unknown)")),
                "effort": session.row["effort"],
                "project": _project_label(session.row["repo"], session.row["cwd"]),
                "started_at": session.row["started_at"],
                "provenance": {
                    "mode": "materialized_fts",
                    "session_storage": "materialized",
                    "message_locator": row["message_id"],
                },
            }
            items.append(item)

    source_truncated = False
    source_warnings: list[str] = []
    if source_reader is None and source_sessions:
        from agentlog.source_reader import CachedSourceTranscriptReader

        source_reader = CachedSourceTranscriptReader()
    if source_reader is not None and source_sessions:
        scan_limit = max(1, min(int(source_scan_limit), MAX_SOURCE_SCAN_LIMIT))
        ordered = sorted(
            source_sessions,
            key=lambda session: str(session.row["started_at"] or ""),
            reverse=True,
        )
        ordered.sort(key=lambda session: not _metadata_matches(session.row, q))
        selected = ordered[:scan_limit]
        source_truncated = len(selected) < len(ordered)
        query_tokens = _FTS_TOKEN_RE.findall(q)[:24]
        scanned = _scan_source_sessions(
            source_reader,
            conn,
            selected,
            metrics,
            query_tokens,
            workers=source_scan_workers,
            cancelled=cancelled,
        )
        if cancelled is not None and cancelled.is_set():
            return {"cancelled": True}
        for session, hits, metadata, message_truncated in scanned:
            source_truncated = source_truncated or message_truncated
            if metadata.get("warning"):
                source_warnings.append(str(metadata["warning"]))
            for hit in hits:
                item = _source_item(hit, by_id, metrics)
                if item is not None:
                    item["provenance"].update(
                        {
                            "source_status": metadata.get("status"),
                            "source_identity": metadata.get("source_identity"),
                            "source_hash": metadata.get("source_hash"),
                        }
                    )
                    items.append(item)

    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        key = _item_key(item)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    total = len(deduped)
    offset = max(0, int(cursor))
    page = deduped[offset : offset + max(1, int(limit))]
    return {
        "q": q,
        "match": match,
        "total": total,
        "cursor": offset,
        "next_cursor": offset + limit if offset + limit < total else None,
        "items": page,
        "note": "Dual-mode search: materialized messages use SQLite FTS; source-backed messages use a bounded read-only source scan.",
        "truncated": source_truncated or total >= min(2000, max(limit * 5, cursor + limit + 200)),
        "source_warnings": list(dict.fromkeys(source_warnings)),
    }


__all__ = [
    "DEFAULT_SOURCE_SCAN_LIMIT",
    "DEFAULT_SOURCE_SCAN_WORKERS",
    "MAX_SOURCE_SCAN_LIMIT",
    "SourceReader",
    "fts_match_query",
    "is_source_backed_session",
    "search_messages",
]
