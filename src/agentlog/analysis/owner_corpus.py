"""Canonical local corpus for manual owner Insight review.

Unlike Coach packets this exporter starts from every visible logical session.
It is deliberately local-only and returns redacted messages in memory; only a
user-chosen export path receives transcript text.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from agentlog.api.identity_aggregates import visible_logical_sessions
from agentlog.safety.redaction import REDACTION_VERSION, RedactionReport, redact_text
from agentlog.source_reader import CachedSourceTranscriptReader, SourceReadResult


@dataclass(frozen=True)
class OwnerCorpus:
    messages: tuple[dict[str, Any], ...]
    session_ids: tuple[str, ...]
    visible_sessions: int
    source_backed_sessions: int
    redaction: dict[str, Any]
    corpus_hash: str


def owner_corpus_since(range_name: str) -> str | None:
    """Return an ISO cutoff for the shared dashboard ranges."""
    normalized = range_name.strip().lower()
    if normalized == "all":
        return None
    hours = {"24h": 24, "7d": 7 * 24, "30d": 30 * 24}.get(normalized)
    if hours is None:
        raise ValueError("range must be one of: 24h, 7d, 30d, all")
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def _within_range(value: object, since: str | None) -> bool:
    if since is None:
        return True
    if not isinstance(value, str) or not value:
        return False
    try:
        return datetime.fromisoformat(value).astimezone(timezone.utc) >= datetime.fromisoformat(since).astimezone(timezone.utc)
    except ValueError:
        return value >= since


def _tool_context(conn: sqlite3.Connection, session_ids: Iterable[str]) -> dict[tuple[str, str], list[str]]:
    ids = tuple(sorted(set(session_ids)))
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    result: dict[tuple[str, str], list[str]] = defaultdict(list)
    for row in conn.execute(
        f"""
        SELECT session_id, message_id, tool_name, action, success
        FROM tool_events
        WHERE session_id IN ({placeholders}) AND message_id IS NOT NULL
        ORDER BY session_id, seq, id
        """,
        ids,
    ):
        outcome = "unknown" if row["success"] is None else ("success" if row["success"] else "failed")
        result[(str(row["session_id"]), str(row["message_id"]))].append(
            f"Tool context: {row['tool_name']} {row['action']}; outcome={outcome}."
        )
    for row in conn.execute(
        f"""
        SELECT session_id, message_id, skill_name, exposure_type
        FROM skill_exposures
        WHERE session_id IN ({placeholders}) AND message_id IS NOT NULL
        ORDER BY session_id, id
        """,
        ids,
    ):
        result[(str(row["session_id"]), str(row["message_id"]))].append(
            f"Skill context: {row['skill_name']} was {row['exposure_type']}."
        )
    return result


def _metadata_messages(conn: sqlite3.Connection, session_id: str) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT id, seq, role, timestamp, text, content_hash, is_tool_plumbing,
                   authored_by_agent
            FROM messages WHERE session_id=? ORDER BY seq, id
            """,
            (session_id,),
        )
    ]


def _source_messages(
    conn: sqlite3.Connection,
    session_id: str,
    reader: CachedSourceTranscriptReader | Callable[[sqlite3.Connection, str], SourceReadResult],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    source = reader(conn, session_id)
    if not source.ready:
        raise ValueError(
            f"owner source transcript unavailable for {session_id}: "
            f"{source.warning or source.status}"
        )
    metadata = _metadata_messages(conn, session_id)
    by_id = {str(row["id"]): row for row in metadata}
    live = [dict(message) for message in source.messages]
    if len(live) != len(metadata):
        raise ValueError(f"owner source transcript is ahead of or behind metadata for {session_id}")
    for message in live:
        stored = by_id.get(str(message["id"]))
        if (
            stored is None
            or int(stored["seq"]) != int(message["seq"])
            or str(stored["role"]) != str(message["role"])
            or str(stored["content_hash"]) != str(message["content_hash"])
        ):
            raise ValueError(f"owner source transcript diverges from metadata for {session_id}")
        message["timestamp"] = message.get("timestamp") or stored.get("timestamp")
        message["is_tool_plumbing"] = bool(message.get("is_tool_plumbing"))
        message["authored_by_agent"] = bool(message.get("authored_by_agent"))
    return live, {
        "source_identity": str(source.source_identity or ""),
        "source_hash": str(source.source_hash or ""),
    }


def _legacy_messages(conn: sqlite3.Connection, session_id: str) -> tuple[list[dict[str, Any]], dict[str, str]]:
    messages = _metadata_messages(conn, session_id)
    return messages, {"metadata_session_id": session_id}


def collect_owner_corpus(
    conn: sqlite3.Connection,
    *,
    since: str | None = None,
    session_ids: Iterable[str] | None = None,
    source_reader: CachedSourceTranscriptReader | Callable[[sqlite3.Connection, str], SourceReadResult] | None = None,
) -> OwnerCorpus:
    """Read the complete selected visible logical corpus with source verification."""
    requested = {value for value in (session_ids or ()) if value}
    rows = conn.execute("SELECT * FROM sessions ORDER BY COALESCE(ended_at, started_at), id").fetchall()
    visible = visible_logical_sessions(conn, rows)
    selected = []
    for item in visible:
        row = item.row
        if requested and item.session_id not in requested and item.metric_session_id not in requested:
            continue
        activity = str(row["ended_at"] or row["started_at"] or "")
        if not _within_range(activity, since):
            continue
        selected.append(item)
    # The visible projection already handles provider shadows and guardians.
    # A second guard makes the corpus invariant clear even if a caller supplies
    # rows from a future projection implementation.
    canonical: dict[str, Any] = {}
    for item in selected:
        canonical.setdefault(item.metric_session_id, item)

    reader = source_reader or CachedSourceTranscriptReader()
    tool_context = _tool_context(conn, canonical)
    report = RedactionReport()
    output: list[dict[str, Any]] = []
    sourced = 0
    for session_id, item in sorted(canonical.items()):
        row = item.row
        if str(row["transcript_storage"]) == "source_backed":
            messages, provenance = _source_messages(conn, session_id, reader)
            sourced += 1
        else:
            messages, provenance = _legacy_messages(conn, session_id)
        packet_id = "owner_corpus:" + session_id
        for message in messages:
            text = str(message.get("text") or "")
            if not text.strip():
                continue
            output.append(
                {
                    "packet_id": packet_id,
                    "session_id": session_id,
                    "message_id": str(message["id"]),
                    "seq": int(message["seq"]),
                    "role": str(message["role"]),
                    "content_hash": str(message["content_hash"]),
                    "text": redact_text(text, report),
                    "context_facts": [redact_text(value, report) for value in tool_context.get((session_id, str(message["id"])), [])],
                    "source_snapshot": {
                        "corpus": "visible_logical_sessions_v1",
                        "logical_session_id": item.session_id,
                        "metric_session_id": session_id,
                        "logical_harness": item.logical_harness,
                        "runtime_harness": item.runtime_harness,
                        "source_provenance": provenance,
                    },
                }
            )
    if isinstance(reader, CachedSourceTranscriptReader) and not reader.verify_current():
        raise ValueError("owner source transcripts changed during corpus export")
    output.sort(key=lambda message: (message["session_id"], message["seq"], message["message_id"]))
    corpus_hash = hashlib.sha256(
        json.dumps(
            [(message["session_id"], message["message_id"], message["content_hash"]) for message in output],
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return OwnerCorpus(
        messages=tuple(output),
        session_ids=tuple(sorted(canonical)),
        visible_sessions=len(canonical),
        source_backed_sessions=sourced,
        redaction=report.to_dict(),
        corpus_hash=corpus_hash,
    )
