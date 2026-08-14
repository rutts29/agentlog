"""Persist source-evidenced, text-free Grok CLI launch observations."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from agentlog.db.repository import Repository
from agentlog.ingest.base import file_stat, hash_bytes
from agentlog.ingest.codex import CodexAdapter


SQL = """
CREATE TABLE IF NOT EXISTS grok_launch_observations (
    id INTEGER PRIMARY KEY,
    caller_session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    caller_artifact_id INTEGER NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
    caller_artifact_hash TEXT NOT NULL,
    call_id TEXT NOT NULL,
    call_timestamp TEXT NOT NULL,
    cwd TEXT,
    requested_model TEXT,
    prompt_hash TEXT NOT NULL,
    UNIQUE(caller_session_id, caller_artifact_hash, call_id)
);
CREATE INDEX IF NOT EXISTS idx_grok_launch_match
ON grok_launch_observations(prompt_hash, cwd, requested_model, call_timestamp);
"""


def _unchanged_result(adapter: CodexAdapter, row: sqlite3.Row):
    path = Path(str(row["path"]))
    try:
        before = file_stat(path)
        if before != (int(row["size"]), int(row["mtime_ns"])):
            return None
        data = path.read_bytes()
        after = file_stat(path)
        if before != after or len(data) != before[0]:
            return None
        parsed_offset = int(row["parsed_offset"])
        if parsed_offset < 0 or parsed_offset > len(data):
            return None
        if hash_bytes(data[:parsed_offset]) != str(row["content_hash"]):
            return None
        result = adapter.parse_chunk(path, data, start_offset=0)
        if file_stat(path) != after:
            return None
        if f"codex:{result.session.external_id}" != str(row["id"]):
            return None
        return result
    except (OSError, UnicodeError, ValueError):
        return None


def apply(conn: sqlite3.Connection) -> None:
    conn.executescript(SQL)
    repo = Repository(conn)
    adapter = CodexAdapter()
    targets = conn.execute(
        """
        SELECT cwd, started_at
        FROM sessions
        WHERE harness = 'grok'
          AND parent_session_id IS NULL
          AND thread_source = 'autonomous_agent_unlinked'
          AND transcript_storage = 'source_backed'
          AND cwd IS NOT NULL AND started_at IS NOT NULL
        """
    ).fetchall()
    windows: dict[str, tuple[datetime, datetime]] = {}
    for target in targets:
        try:
            started = datetime.fromisoformat(str(target["started_at"]).replace("Z", "+00:00"))
        except ValueError:
            continue
        cwd = str(target["cwd"])
        begin, end = windows.get(cwd, (started, started))
        windows[cwd] = (min(begin, started), max(end, started))
    if not windows:
        return
    predicates: list[str] = []
    params: list[str] = []
    for cwd, (begin, end) in sorted(windows.items()):
        predicates.append("(s.cwd = ? AND s.started_at <= ? AND COALESCE(s.ended_at, s.started_at) >= ?)")
        params.extend([cwd, end.isoformat(), (begin - timedelta(minutes=5)).isoformat()])
    rows = conn.execute(
        """
        SELECT s.id, s.artifact_id, a.path, a.size, a.mtime_ns,
               a.content_hash, a.parsed_offset
        FROM sessions s
        JOIN artifacts a ON a.id = s.artifact_id
        WHERE s.harness = 'codex'
          AND s.transcript_storage = 'source_backed'
          AND a.transcript_storage = 'source_backed'
          AND (""" + " OR ".join(predicates) + ") ORDER BY s.id",
        params,
    ).fetchall()
    for row in rows:
        result = _unchanged_result(adapter, row)
        if result is None or result.session.external_id is None:
            continue
        expected = [
            (int(message.seq), str(message.role), str(message.content_hash))
            for message in result.messages
        ]
        actual = [
            (int(message["seq"]), str(message["role"]), str(message["content_hash"]))
            for message in conn.execute(
                "SELECT seq, role, content_hash FROM messages WHERE session_id = ? ORDER BY seq",
                (row["id"],),
            )
        ]
        if expected != actual:
            continue
        repo.replace_grok_launch_observations(
            str(row["id"]), int(row["artifact_id"]), result.extras.get("grok_launches", [])
        )
