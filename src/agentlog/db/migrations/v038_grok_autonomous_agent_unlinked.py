"""Classify source-verified unlinked Grok autonomous runs."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from agentlog.ingest.grok import GrokAdapter
from agentlog.session_identity import GROK_AUTONOMOUS_AGENT_UNLINKED_THREAD_SOURCE


def _matching_result(
    adapter: GrokAdapter, row: sqlite3.Row
):
    path = Path(str(row["path"]))
    try:
        snapshot = adapter.capture_source(path)
    except (OSError, UnicodeError, ValueError):
        return None
    if (
        snapshot.revision != (int(row["size"]), int(row["mtime_ns"]))
        or snapshot.content_hash != str(row["content_hash"])
    ):
        return None
    try:
        results = adapter.parse_source_snapshot(path, snapshot)
        if not adapter.composite_snapshot_matches(
            path,
            revision=snapshot.revision,
            content_hash=snapshot.content_hash,
        ):
            return None
    except Exception:
        return None
    if len(results) != 1:
        return None
    result = results[0]
    if (
        result.session.thread_source
        != GROK_AUTONOMOUS_AGENT_UNLINKED_THREAD_SOURCE
        or result.session.parent_session_id is not None
    ):
        return None
    return result


def apply(conn: sqlite3.Connection) -> None:
    """Update only unchanged metadata rows whose current source still matches."""
    conn.execute("SAVEPOINT grok_autonomous_agent_unlinked")
    try:
        adapter = GrokAdapter()
        candidates = conn.execute(
            """
            SELECT s.id, s.artifact_id, a.path, a.size, a.mtime_ns, a.content_hash
            FROM sessions s
            JOIN artifacts a ON a.id = s.artifact_id
            WHERE s.harness = 'grok'
              AND s.parent_session_id IS NULL
              AND s.thread_source IS NULL
              AND s.transcript_storage = 'source_backed'
              AND a.transcript_storage = 'source_backed'
            ORDER BY s.id
            """
        ).fetchall()
        for candidate in candidates:
            result = _matching_result(adapter, candidate)
            if result is None:
                continue
            expected = [
                (int(message.seq), str(message.role), str(message.content_hash))
                for message in result.messages
            ]
            actual = [
                (int(message["seq"]), str(message["role"]), str(message["content_hash"]))
                for message in conn.execute(
                    """
                    SELECT seq, role, content_hash
                    FROM messages
                    WHERE session_id = ?
                    ORDER BY seq
                    """,
                    (candidate["id"],),
                )
            ]
            if actual != expected:
                continue
            prompt = next(
                (
                    message
                    for message in result.messages
                    if message.role == "user"
                    and not message.is_tool_plumbing
                    and message.authored_by_agent
                ),
                None,
            )
            if prompt is None:
                continue
            updated = conn.execute(
                """
                UPDATE sessions
                SET thread_source = ?
                WHERE id = ? AND thread_source IS NULL AND parent_session_id IS NULL
                """,
                (GROK_AUTONOMOUS_AGENT_UNLINKED_THREAD_SOURCE, candidate["id"]),
            )
            if updated.rowcount != 1:
                continue
            conn.execute(
                """
                UPDATE messages
                SET authored_by_agent = 1
                WHERE session_id = ? AND seq = ?
                """,
                (candidate["id"], int(prompt.seq)),
            )
    except Exception:
        conn.execute("ROLLBACK TO grok_autonomous_agent_unlinked")
        conn.execute("RELEASE grok_autonomous_agent_unlinked")
        raise
    conn.execute("RELEASE grok_autonomous_agent_unlinked")
