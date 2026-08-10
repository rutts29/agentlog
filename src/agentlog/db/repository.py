from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Iterator, TypeVar

from agentlog.normalize.model_identity import ModelIdentity, resolve_model_identity
from agentlog.normalize.models import ParseResult

T = TypeVar("T")

_BUSY_RETRIES = 8
_BUSY_RETRY_BASE_S = 0.05


def _is_busy(exc: BaseException) -> bool:
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


def _with_busy_retry(
    fn: Callable[[], T],
    *,
    attempts: int = _BUSY_RETRIES,
    base_delay_s: float = _BUSY_RETRY_BASE_S,
) -> T:
    last: BaseException | None = None
    for i in range(max(1, attempts)):
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            last = exc
            if not _is_busy(exc) or i >= attempts - 1:
                raise
            time.sleep(base_delay_s * (2**i))
    assert last is not None
    raise last


@contextmanager
def _savepoint(conn: sqlite3.Connection, name: str) -> Iterator[None]:
    """Atomic sub-batch that nests inside any caller-owned transaction.

    A savepoint (not BEGIN) is required because callers such as the ingest
    pipeline already hold an open transaction; rolling back to it undoes a
    partially applied batch so a busy-retry re-runs from a clean state.
    """
    conn.execute(f"SAVEPOINT {name}")
    try:
        yield
    except BaseException:
        try:
            conn.execute(f"ROLLBACK TO {name}")
        finally:
            conn.execute(f"RELEASE {name}")
        raise
    conn.execute(f"RELEASE {name}")


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _identity_for(
    raw: str | None,
    *,
    provider_hint: str | None = None,
    agent_profile_hint: str | None = None,
) -> ModelIdentity:
    return resolve_model_identity(
        raw,
        provider_hint=provider_hint,
        agent_profile_hint=agent_profile_hint,
    )


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
    """Legacy message-id-based window id. Prefer content_hash from windows.py."""
    raw = f"{session_id}:e:{req}:{resp}"
    return hashlib.sha1(raw.encode()).hexdigest()[:24]


def _uid(session_id: str, usage_source: str, seq: int) -> str:
    raw = f"{session_id}:u:{usage_source}:{seq}"
    return hashlib.sha1(raw.encode()).hexdigest()[:24]


_SESSION_IDENTITY_COLUMNS = (
    "parent_session_id",
    "started_at",
    "ended_at",
    "repo",
    "cwd",
    "branch",
    "commit_sha",
    "model",
    "provider",
    "agent_profile",
    "effort",
    "effort_source",
)


def _incoming_quality(result: ParseResult) -> dict[str, int]:
    session = result.session
    identity = sum(
        1
        for col in _SESSION_IDENTITY_COLUMNS
        if getattr(session, col, None) not in (None, "")
    )
    return {
        "messages": len(result.messages),
        "tools": len(result.tool_events),
        "usage": len(result.token_usages),
        "skills": len(result.skill_exposures),
        "message_timestamps": sum(1 for m in result.messages if m.timestamp),
        "message_models": sum(1 for m in result.messages if m.model),
        "identity": identity,
    }


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

    def _merge_session_metadata(
        self,
        session_id: str,
        result: ParseResult,
        *,
        artifact_id: int | None,
    ) -> None:
        from agentlog.ingest.cursor import prefer_repo

        session = result.session
        row = self.conn.execute(
            "SELECT repo, cwd, parent_session_id FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            return
        repo = prefer_repo(row["repo"], session.repo)
        cwd = session.cwd if repo == session.repo and session.cwd else row["cwd"]
        if repo != row["repo"] and session.cwd:
            cwd = session.cwd
        parent = session.parent_session_id or row["parent_session_id"]
        if session.model is not None:
            ident = _identity_for(
                session.model,
                provider_hint=session.provider,
                agent_profile_hint=session.agent_profile,
            )
            self.conn.execute(
                """
                UPDATE sessions SET
                    ended_at = COALESCE(?, ended_at),
                    model = ?,
                    model_canonical = ?,
                    provider = COALESCE(?, provider),
                    agent_profile = COALESCE(?, agent_profile),
                    effort = COALESCE(?, effort),
                    effort_source = COALESCE(?, effort_source),
                    cwd = ?,
                    branch = COALESCE(?, branch),
                    commit_sha = COALESCE(?, commit_sha),
                    repo = ?,
                    parent_session_id = COALESCE(?, parent_session_id),
                    artifact_id = COALESCE(?, artifact_id)
                WHERE id = ?
                """,
                (
                    _iso(session.ended_at),
                    session.model,
                    ident.canonical,
                    ident.provider,
                    ident.agent_profile,
                    session.effort,
                    session.effort_source,
                    cwd,
                    session.branch,
                    session.commit_sha,
                    repo,
                    parent,
                    artifact_id,
                    session_id,
                ),
            )
        else:
            self.conn.execute(
                """
                UPDATE sessions SET
                    ended_at = COALESCE(?, ended_at),
                    provider = COALESCE(?, provider),
                    agent_profile = COALESCE(?, agent_profile),
                    effort = COALESCE(?, effort),
                    effort_source = COALESCE(?, effort_source),
                    cwd = ?,
                    branch = COALESCE(?, branch),
                    commit_sha = COALESCE(?, commit_sha),
                    repo = ?,
                    parent_session_id = COALESCE(?, parent_session_id),
                    artifact_id = COALESCE(?, artifact_id)
                WHERE id = ?
                """,
                (
                    _iso(session.ended_at),
                    session.provider,
                    session.agent_profile,
                    session.effort,
                    session.effort_source,
                    cwd,
                    session.branch,
                    session.commit_sha,
                    repo,
                    parent,
                    artifact_id,
                    session_id,
                ),
            )

    def _stored_quality(self, session_id: str) -> dict[str, int]:
        counts = self.conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM messages WHERE session_id = :sid) AS messages,
                (SELECT COUNT(*) FROM tool_events WHERE session_id = :sid) AS tools,
                (SELECT COUNT(*) FROM token_usage WHERE session_id = :sid) AS usage,
                (SELECT COUNT(*) FROM skill_exposures WHERE session_id = :sid)
                    AS skills,
                (SELECT COUNT(*) FROM messages
                 WHERE session_id = :sid AND COALESCE(timestamp, '') != '')
                    AS message_timestamps,
                (SELECT COUNT(*) FROM messages
                 WHERE session_id = :sid AND COALESCE(model, '') != '')
                    AS message_models
            """,
            {"sid": session_id},
        ).fetchone()
        row = self.conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        identity = 0
        if row is not None:
            for col in _SESSION_IDENTITY_COLUMNS:
                if col in row.keys() and row[col] not in (None, ""):
                    identity += 1
        return {
            "messages": int(counts["messages"]),
            "tools": int(counts["tools"]),
            "usage": int(counts["usage"]),
            "skills": int(counts["skills"]),
            "message_timestamps": int(counts["message_timestamps"]),
            "message_models": int(counts["message_models"]),
            "identity": identity,
        }

    def _equal_length_copy_is_poorer(
        self, session_id: str, result: ParseResult, *, existing_n: int
    ) -> bool:
        """True when the stored copy has the same turns but more evidence.

        Message count alone cannot distinguish two copies of one conversation:
        they can differ in tool events, per-message model/timestamp, token
        usage, skills and session identity fields. Replacement is refused only
        when the turns are byte-identical and the stored copy dominates.
        """
        if existing_n == 0 or existing_n != len(result.messages):
            return False
        stored_hashes = [
            str(r["content_hash"])
            for r in self.conn.execute(
                "SELECT content_hash FROM messages WHERE session_id = ? ORDER BY seq",
                (session_id,),
            )
        ]
        if stored_hashes != [m.content_hash for m in result.messages]:
            return False
        stored = self._stored_quality(session_id)
        incoming = _incoming_quality(result)
        if any(stored[k] < incoming[k] for k in stored):
            return False
        return any(stored[k] > incoming[k] for k in stored)

    def save_parse_result(
        self,
        *,
        artifact_id: int,
        result: ParseResult,
        append: bool,
        base_seq: int = 0,
        base_tool_seq: int = 0,
        base_token_seq: int = 0,
    ) -> str:
        session = result.session
        session_id = _sid(session.harness.value, session.external_id)
        session_ident = _identity_for(
            session.model,
            provider_hint=session.provider,
            agent_profile_hint=session.agent_profile,
        )

        if not append:
            existing_n = self.max_message_seq(session_id)
            # Multiple transcript paths can resolve to one session id. Never
            # replace a richer copy with a poorer or stale variant, including
            # one that carries the same messages but less surrounding evidence.
            if existing_n > len(result.messages) or self._equal_length_copy_is_poorer(
                session_id, result, existing_n=existing_n
            ):
                self._merge_session_metadata(
                    session_id,
                    result,
                    artifact_id=None,
                )
                return session_id
            self.conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            self.conn.execute(
                """
                INSERT INTO sessions (
                    id, harness, external_id, parent_session_id, artifact_id,
                    started_at, ended_at, repo, cwd, branch, commit_sha,
                    model, model_canonical, provider, agent_profile,
                    effort, effort_source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    session_ident.canonical,
                    session_ident.provider,
                    session_ident.agent_profile,
                    session.effort,
                    session.effort_source,
                ),
            )
        else:
            self._merge_session_metadata(
                session_id,
                result,
                artifact_id=artifact_id,
            )

        msg_id_by_seq: dict[int, str] = {}
        for msg in result.messages:
            seq = base_seq + msg.seq
            mid = _mid(session_id, seq)
            msg_id_by_seq[msg.seq] = mid
            if msg.model:
                msg_ident = _identity_for(
                    msg.model,
                    provider_hint=msg.provider or session.provider,
                    agent_profile_hint=(
                        msg.agent_profile or session.agent_profile
                    ),
                )
            else:
                msg_ident = ModelIdentity(
                    raw=None,
                    canonical=None,
                    provider=None,
                    agent_profile=None,
                    family=None,
                )
            self.conn.execute(
                """
                INSERT OR REPLACE INTO messages (
                    id, session_id, seq, role, timestamp, model,
                    model_canonical, provider, agent_profile,
                    effort, effort_source, text, content_hash,
                    is_tool_plumbing, authored_by_agent
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    mid,
                    session_id,
                    seq,
                    msg.role,
                    _iso(msg.timestamp),
                    msg.model,
                    msg_ident.canonical,
                    msg_ident.provider,
                    msg_ident.agent_profile,
                    msg.effort,
                    msg.effort_source,
                    msg.text,
                    msg.content_hash,
                    1 if msg.is_tool_plumbing else 0,
                    1 if msg.authored_by_agent else 0,
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

        for tu in result.token_usages:
            seq = base_token_seq + tu.seq
            mid = None
            if tu.message_seq is not None:
                mid = msg_id_by_seq.get(tu.message_seq) or _mid(
                    session_id, base_seq + tu.message_seq
                )
            tu_ident = _identity_for(
                tu.model,
                provider_hint=session.provider,
                agent_profile_hint=session.agent_profile,
            )
            self.conn.execute(
                """
                INSERT OR REPLACE INTO token_usage (
                    id, session_id, message_id, seq, granularity, usage_source,
                    model, model_canonical, input_tokens, output_tokens,
                    cache_creation_input_tokens, cache_read_input_tokens,
                    cached_input_tokens, cache_write_input_tokens,
                    reasoning_output_tokens, total_tokens, timestamp, extras_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _uid(session_id, tu.usage_source, seq),
                    session_id,
                    mid,
                    seq,
                    tu.granularity,
                    tu.usage_source,
                    tu.model,
                    tu_ident.canonical,
                    tu.input_tokens,
                    tu.output_tokens,
                    tu.cache_creation_input_tokens,
                    tu.cache_read_input_tokens,
                    tu.cached_input_tokens,
                    tu.cache_write_input_tokens,
                    tu.reasoning_output_tokens,
                    tu.total_tokens,
                    _iso(tu.timestamp),
                    json.dumps(tu.extras, separators=(",", ":"), default=str),
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

    def max_token_seq(self, session_id: str) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS m FROM token_usage WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return int(row["m"]) if row else 0

    def replace_exchange_windows(
        self,
        session_id: str,
        windows: Iterable[
            tuple[str, str, str]
            | tuple[str, str, str, str]
            | tuple[str, str, str, str, str]
        ],
    ) -> None:
        """Upsert windows by durable id; never CASCADE-delete durable labels."""
        from agentlog.analysis.label_survival import refresh_label_links
        from agentlog.analysis.windows import window_id_for_content_hash

        has_content_hash = self._exchange_windows_has_content_hash()
        desired: dict[str, tuple[str, str, str, str]] = {}
        occurrence_by_hash: dict[str, int] = {}
        for item in windows:
            if len(item) == 5:
                req_id, resp_id, input_hash, content_hash, wid = item  # type: ignore[misc]
            elif len(item) == 4:
                req_id, resp_id, input_hash, content_hash = item  # type: ignore[misc]
                occ = occurrence_by_hash.get(content_hash, 0)
                occurrence_by_hash[content_hash] = occ + 1
                wid = window_id_for_content_hash(content_hash, occ)
            else:
                req_id, resp_id, input_hash = item  # type: ignore[misc]
                content_hash = self._content_hash_for_pair(session_id, req_id, resp_id)
                if not content_hash:
                    content_hash = _eid(session_id, req_id, resp_id)
                occ = occurrence_by_hash.get(content_hash, 0)
                occurrence_by_hash[content_hash] = occ + 1
                wid = window_id_for_content_hash(content_hash, occ)
            desired[wid] = (req_id, resp_id, input_hash, content_hash)

        def _apply() -> None:
            with _savepoint(self.conn, "replace_exchange_windows"):
                self._apply_window_batch(
                    session_id, desired, has_content_hash=has_content_hash
                )
                if has_content_hash:
                    refresh_label_links(self.conn)

        _with_busy_retry(_apply)

    def _apply_window_batch(
        self,
        session_id: str,
        desired: dict[str, tuple[str, str, str, str]],
        *,
        has_content_hash: bool,
    ) -> None:
        existing_ids = {
            str(r["id"])
            for r in self.conn.execute(
                "SELECT id FROM exchange_windows WHERE session_id = ?",
                (session_id,),
            )
        }

        for wid in existing_ids - set(desired):
            self.conn.execute("DELETE FROM exchange_windows WHERE id = ?", (wid,))

        for wid, (req_id, resp_id, input_hash, content_hash) in desired.items():
            # A re-parse keeps message ids stable (they derive from seq) while
            # edited turn text yields a new content-derived window id, so the
            # superseded row must go before the insert or the secondary unique
            # index (session_id, request_message_id, response_message_id) trips.
            self.conn.execute(
                """
                DELETE FROM exchange_windows
                WHERE session_id = ?
                  AND request_message_id = ?
                  AND response_message_id = ?
                  AND id != ?
                """,
                (session_id, req_id, resp_id, wid),
            )
            if has_content_hash:
                self.conn.execute(
                    """
                    INSERT INTO exchange_windows (
                        id, session_id, request_message_id, response_message_id,
                        input_hash, content_hash
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        request_message_id = excluded.request_message_id,
                        response_message_id = excluded.response_message_id,
                        input_hash = excluded.input_hash,
                        content_hash = excluded.content_hash,
                        session_id = excluded.session_id
                    """,
                    (
                        wid,
                        session_id,
                        req_id,
                        resp_id,
                        input_hash,
                        content_hash,
                    ),
                )
            else:
                self.conn.execute(
                    """
                    INSERT INTO exchange_windows (
                        id, session_id, request_message_id, response_message_id,
                        input_hash
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        request_message_id = excluded.request_message_id,
                        response_message_id = excluded.response_message_id,
                        input_hash = excluded.input_hash,
                        session_id = excluded.session_id
                    """,
                    (
                        wid,
                        session_id,
                        req_id,
                        resp_id,
                        input_hash,
                    ),
                )

    def _exchange_windows_has_content_hash(self) -> bool:
        cols = {
            str(r[1])
            for r in self.conn.execute("PRAGMA table_info(exchange_windows)")
        }
        return "content_hash" in cols

    def _content_hash_for_pair(
        self, session_id: str, req_id: str, resp_id: str
    ) -> str:
        from agentlog.analysis.windows import compute_window_content_hash

        req = self.conn.execute(
            "SELECT text FROM messages WHERE id = ?", (req_id,)
        ).fetchone()
        resp = self.conn.execute(
            "SELECT text FROM messages WHERE id = ?", (resp_id,)
        ).fetchone()
        if req is None or resp is None:
            return ""
        return compute_window_content_hash(session_id, req["text"], resp["text"])

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
