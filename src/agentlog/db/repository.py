from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

from agentlog.normalize.models import ParseResult


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _sid(harness: str, external_id: str) -> str:
    return f"{harness}:{external_id}"


def _mid(session_id: str, seq: int) -> str:
    return f"{session_id}:m:{seq}"


def _tid(session_id: str, seq: int) -> str:
    return f"{session_id}:t:{seq}"


def _kid(session_id: str, idx: int, skill_name: str) -> str:
    raw = f"{session_id}:k:{idx}:{skill_name}"
    return hashlib.sha1(raw.encode()).hexdigest()[:24]


def _eid(session_id: str, req: str, resp: str) -> str:
    raw = f"{session_id}:e:{req}:{resp}"
    return hashlib.sha1(raw.encode()).hexdigest()[:24]


@dataclass
class ArtifactRow:
    id: int
    harness: str
    path: str
    size: int
    mtime_ns: int
    content_hash: str
    parsed_offset: int
    parser_version: str


class Repository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def get_artifact_by_path(self, path: str) -> ArtifactRow | None:
        row = self.conn.execute(
            "SELECT * FROM artifacts WHERE path = ?", (path,)
        ).fetchone()
        if not row:
            return None
        return ArtifactRow(**dict(row))

    def upsert_artifact(
        self,
        *,
        harness: str,
        path: str,
        size: int,
        mtime_ns: int,
        content_hash: str,
        parsed_offset: int,
        parser_version: str,
    ) -> int:
        self.conn.execute(
            """
            INSERT INTO artifacts (harness, path, size, mtime_ns, content_hash, parsed_offset, parser_version)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                harness=excluded.harness,
                size=excluded.size,
                mtime_ns=excluded.mtime_ns,
                content_hash=excluded.content_hash,
                parsed_offset=excluded.parsed_offset,
                parser_version=excluded.parser_version
            """,
            (harness, path, size, mtime_ns, content_hash, parsed_offset, parser_version),
        )
        row = self.conn.execute(
            "SELECT id FROM artifacts WHERE path = ?", (path,)
        ).fetchone()
        assert row is not None
        return int(row["id"])

    def delete_sessions_for_artifact(self, artifact_id: int) -> None:
        self.conn.execute(
            "DELETE FROM sessions WHERE artifact_id = ?", (artifact_id,)
        )

    def save_parse_result(
        self,
        *,
        artifact_id: int,
        result: ParseResult,
        append: bool,
        base_seq: int = 0,
        base_tool_seq: int = 0,
    ) -> str:
        session = result.session
        session_id = _sid(session.harness.value, session.external_id)

        if not append:
            self.conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            self.conn.execute(
                """
                INSERT INTO sessions (
                    id, harness, external_id, parent_session_id, artifact_id,
                    started_at, ended_at, repo, cwd, branch, commit_sha, model, effort
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    session.harness.value,
                    session.external_id,
                    session.parent_session_id,
                    artifact_id,
                    _iso(session.started_at),
                    _iso(session.ended_at),
                    session.repo,
                    session.cwd,
                    session.branch,
                    session.commit_sha,
                    session.model,
                    session.effort,
                ),
            )
        else:
            self.conn.execute(
                """
                UPDATE sessions SET
                    ended_at = COALESCE(?, ended_at),
                    model = COALESCE(?, model),
                    effort = COALESCE(?, effort),
                    cwd = COALESCE(?, cwd),
                    branch = COALESCE(?, branch),
                    commit_sha = COALESCE(?, commit_sha),
                    repo = COALESCE(?, repo)
                WHERE id = ?
                """,
                (
                    _iso(session.ended_at),
                    session.model,
                    session.effort,
                    session.cwd,
                    session.branch,
                    session.commit_sha,
                    session.repo,
                    session_id,
                ),
            )

        msg_id_by_seq: dict[int, str] = {}
        for msg in result.messages:
            seq = base_seq + msg.seq
            mid = _mid(session_id, seq)
            msg_id_by_seq[msg.seq] = mid
            self.conn.execute(
                """
                INSERT OR REPLACE INTO messages (
                    id, session_id, seq, role, timestamp, model, effort,
                    text, content_hash, is_tool_plumbing
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    mid,
                    session_id,
                    seq,
                    msg.role,
                    _iso(msg.timestamp),
                    msg.model,
                    msg.effort,
                    msg.text,
                    msg.content_hash,
                    1 if msg.is_tool_plumbing else 0,
                ),
            )

        for te in result.tool_events:
            seq = base_tool_seq + te.seq
            mid = None
            if te.message_seq is not None and te.message_seq in msg_id_by_seq:
                mid = msg_id_by_seq[te.message_seq]
            elif te.message_seq is not None:
                mid = _mid(session_id, base_seq + te.message_seq)
            self.conn.execute(
                """
                INSERT OR REPLACE INTO tool_events (
                    id, session_id, message_id, seq, tool_name, action, success, duration_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _tid(session_id, seq),
                    session_id,
                    mid,
                    seq,
                    te.tool_name,
                    te.action,
                    None if te.success is None else int(te.success),
                    te.duration_ms,
                ),
            )

        for idx, sk in enumerate(result.skill_exposures):
            mid = None
            if sk.message_seq is not None:
                mid = msg_id_by_seq.get(sk.message_seq) or _mid(
                    session_id, base_seq + sk.message_seq
                )
            self.conn.execute(
                """
                INSERT OR REPLACE INTO skill_exposures (
                    id, session_id, message_id, skill_name, exposure_type
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    _kid(session_id, base_seq * 1000 + idx, sk.skill_name),
                    session_id,
                    mid,
                    sk.skill_name,
                    sk.exposure_type,
                ),
            )

        return session_id

    def max_message_seq(self, session_id: str) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS m FROM messages WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return int(row["m"]) if row else 0

    def max_tool_seq(self, session_id: str) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS m FROM tool_events WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return int(row["m"]) if row else 0

    def replace_exchange_windows(
        self, session_id: str, windows: Iterable[tuple[str, str, str]]
    ) -> None:
        self.conn.execute(
            "DELETE FROM exchange_windows WHERE session_id = ?", (session_id,)
        )
        for req_id, resp_id, input_hash in windows:
            self.conn.execute(
                """
                INSERT INTO exchange_windows (
                    id, session_id, request_message_id, response_message_id, input_hash
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (_eid(session_id, req_id, resp_id), session_id, req_id, resp_id, input_hash),
            )

    def list_messages(self, session_id: str) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """
                SELECT * FROM messages
                WHERE session_id = ?
                ORDER BY seq
                """,
                (session_id,),
            )
        )

    def stats(self) -> dict[str, Any]:
        by_harness = list(
            self.conn.execute(
                """
                SELECT harness, COUNT(*) AS sessions
                FROM sessions
                GROUP BY harness
                ORDER BY harness
                """
            )
        )
        messages = self.conn.execute("SELECT COUNT(*) AS c FROM messages").fetchone()
        tools = self.conn.execute("SELECT COUNT(*) AS c FROM tool_events").fetchone()
        skills = self.conn.execute("SELECT COUNT(*) AS c FROM skill_exposures").fetchone()
        artifacts = self.conn.execute("SELECT COUNT(*) AS c FROM artifacts").fetchone()
        windows = self.conn.execute("SELECT COUNT(*) AS c FROM exchange_windows").fetchone()
        by_model = list(
            self.conn.execute(
                """
                SELECT COALESCE(model, '(unknown)') AS model, COUNT(*) AS sessions
                FROM sessions
                GROUP BY model
                ORDER BY sessions DESC
                LIMIT 20
                """
            )
        )
        date_range = self.conn.execute(
            """
            SELECT MIN(started_at) AS first_at, MAX(COALESCE(ended_at, started_at)) AS last_at
            FROM sessions
            """
        ).fetchone()
        return {
            "by_harness": by_harness,
            "messages": int(messages["c"]) if messages else 0,
            "tool_events": int(tools["c"]) if tools else 0,
            "skill_exposures": int(skills["c"]) if skills else 0,
            "artifacts": int(artifacts["c"]) if artifacts else 0,
            "exchange_windows": int(windows["c"]) if windows else 0,
            "by_model": by_model,
            "first_at": date_range["first_at"] if date_range else None,
            "last_at": date_range["last_at"] if date_range else None,
        }

    def list_sessions(
        self,
        *,
        harness: str | None = None,
        since: str | None = None,
        limit: int = 50,
    ) -> list[sqlite3.Row]:
        clauses: list[str] = []
        params: list[Any] = []
        if harness:
            clauses.append("harness = ?")
            params.append(harness)
        if since:
            clauses.append("started_at >= ?")
            params.append(since)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        return list(
            self.conn.execute(
                f"""
                SELECT id, harness, external_id, started_at, ended_at, cwd, model, effort,
                       (SELECT COUNT(*) FROM messages m WHERE m.session_id = sessions.id) AS message_count
                FROM sessions
                {where}
                ORDER BY COALESCE(started_at, '') DESC
                LIMIT ?
                """,
                params,
            )
        )

    def get_session(self, session_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()

    def search_messages(self, query: str, limit: int = 30) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """
                SELECT m.id, m.session_id, m.seq, m.role, m.timestamp, m.model,
                       snippet(messages_fts, 0, '[', ']', '…', 16) AS snippet,
                       s.harness, s.cwd
                FROM messages_fts
                JOIN messages m ON m.rowid = messages_fts.rowid
                JOIN sessions s ON s.id = m.session_id
                WHERE messages_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (query, limit),
            )
        )
