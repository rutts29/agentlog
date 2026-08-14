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
from agentlog.session_identity import (
    GROK_BOOTSTRAP_ONLY_THREAD_SOURCE,
    INTERNAL_APPROVAL_GUARDIAN_THREAD_SOURCE,
    SUPPRESSED_ACTIVITY_THREAD_SOURCES,
    is_suppressed_activity_session,
)

T = TypeVar("T")

_BUSY_RETRIES = 8
_BUSY_RETRY_BASE_S = 0.05

LEGACY_MATERIALIZED = "legacy_materialized"
SOURCE_BACKED = "source_backed"
_TRANSCRIPT_STORAGES = {LEGACY_MATERIALIZED, SOURCE_BACKED}


class TranscriptStorageError(RuntimeError):
    pass


def _transcript_storage(value: object, *, scope: str) -> str:
    storage = str(value or "")
    if storage not in _TRANSCRIPT_STORAGES:
        raise TranscriptStorageError(
            f"ambiguous transcript storage for {scope}: {value!r}"
        )
    return storage


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


def _nonnegative_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _workflow_group(value: object) -> tuple[str | None, str | None, int | None]:
    if not isinstance(value, dict):
        return None, None, None
    group_id = _optional_text(value.get("id"))
    if group_id is None:
        return None, None, None
    label = _optional_text(value.get("label")) or group_id
    position = value.get("position")
    if isinstance(position, bool):
        position = None
    elif not isinstance(position, int):
        position = None
    return group_id, label, position


_SESSION_IDENTITY_COLUMNS = (
    "parent_session_id",
    "originator",
    "thread_source",
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
    transcript_storage: str


class Repository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def get_artifact_by_path(self, path: str) -> ArtifactRow | None:
        row = self.conn.execute(
            "SELECT * FROM artifacts WHERE path = ?", (path,)
        ).fetchone()
        if not row:
            return None
        artifact = ArtifactRow(**dict(row))
        _transcript_storage(
            artifact.transcript_storage, scope=f"artifact {artifact.path}"
        )
        return artifact

    def _artifact_storage(self, artifact_id: int) -> str:
        row = self.conn.execute(
            "SELECT transcript_storage FROM artifacts WHERE id = ?",
            (artifact_id,),
        ).fetchone()
        if row is None:
            raise TranscriptStorageError(f"missing artifact {artifact_id}")
        return _transcript_storage(
            row["transcript_storage"], scope=f"artifact {artifact_id}"
        )

    def session_transcript_storage(self, session_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT transcript_storage FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        return _transcript_storage(
            row["transcript_storage"], scope=f"session {session_id}"
        )

    def session_artifact_id(self, session_id: str) -> int | None:
        row = self.conn.execute(
            "SELECT artifact_id FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if row is None or row["artifact_id"] is None:
            return None
        return int(row["artifact_id"])

    def cursor_metadata_targets(self) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT id, external_id FROM sessions WHERE harness = 'cursor'"
            )
        )

    def reconcile_cursor_metadata(
        self,
        session_id: str,
        *,
        model: str | None,
        effort: str | None,
        effort_source: str | None,
        branch: str | None,
    ) -> bool:
        row = self.conn.execute(
            """
            SELECT model, model_canonical, provider, agent_profile,
                   effort, effort_source, branch
            FROM sessions
            WHERE id = ? AND harness = 'cursor'
            """,
            (session_id,),
        ).fetchone()
        if row is None:
            return False
        updates: dict[str, object] = {}
        if model is not None:
            ident = _identity_for(model)
            for column, value in {
                "model": model,
                "model_canonical": ident.canonical,
                "provider": ident.provider,
                "agent_profile": ident.agent_profile,
            }.items():
                if row[column] != value:
                    updates[column] = value
        if effort is not None:
            if row["effort"] != effort:
                updates["effort"] = effort
            if row["effort_source"] != effort_source:
                updates["effort_source"] = effort_source
        if branch is not None and row["branch"] != branch:
            updates["branch"] = branch
        if not updates:
            return False
        assignments = ", ".join(f"{column} = ?" for column in updates)
        self.conn.execute(
            f"UPDATE sessions SET {assignments} WHERE id = ?",
            [*updates.values(), session_id],
        )
        return True

    def source_backed_artifacts_missing_attention_tail(
        self, harness: str, *, limit: int
    ) -> set[int]:
        rows = self.conn.execute(
            """
            SELECT DISTINCT s.artifact_id
            FROM sessions s
            JOIN artifacts a ON a.id = s.artifact_id
            WHERE s.harness = ?
              AND s.transcript_storage = ?
              AND s.attention_tail_revision IS NULL
              AND s.artifact_id IS NOT NULL
            ORDER BY s.artifact_id
            LIMIT ?
            """,
            (harness, SOURCE_BACKED, limit),
        ).fetchall()
        return {int(row["artifact_id"]) for row in rows}

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
        transcript_storage: str = LEGACY_MATERIALIZED,
    ) -> int:
        transcript_storage = _transcript_storage(
            transcript_storage, scope=f"artifact {path}"
        )
        existing = self.get_artifact_by_path(path)
        if (
            existing is not None
            and existing.transcript_storage != transcript_storage
        ):
            raise TranscriptStorageError(
                f"artifact {path} cannot change transcript storage from "
                f"{existing.transcript_storage} to {transcript_storage}"
            )
        self.conn.execute(
            """
            INSERT INTO artifacts (
                harness, path, size, mtime_ns, content_hash, parsed_offset,
                parser_version, transcript_storage
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                harness=excluded.harness,
                size=excluded.size,
                mtime_ns=excluded.mtime_ns,
                content_hash=excluded.content_hash,
                parsed_offset=excluded.parsed_offset,
                parser_version=excluded.parser_version
            """,
            (
                harness,
                path,
                size,
                mtime_ns,
                content_hash,
                parsed_offset,
                parser_version,
                transcript_storage,
            ),
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

    def replace_session_links(
        self, source_session_id: str, links: Iterable[dict[str, Any]]
    ) -> None:
        links = list(links)
        if not links:
            return
        for link in links:
            link_type = str(link.get("link_type") or "").strip()
            target_harness = str(link.get("target_harness") or "").strip()
            target_external_id = str(
                link.get("target_external_id") or ""
            ).strip()
            if not link_type or not target_harness or not target_external_id:
                continue
            target_id = _sid(target_harness, target_external_id)
            exists = self.conn.execute(
                "SELECT 1 FROM sessions WHERE id = ?", (target_id,)
            ).fetchone()
            evidence = link.get("evidence")
            self.conn.execute(
                """
                INSERT INTO session_links (
                    source_session_id, target_session_id, link_type,
                    target_harness, target_external_id, link_role,
                    confidence, evidence_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (
                    source_session_id, link_type, target_harness,
                    target_external_id
                ) DO UPDATE SET
                    target_session_id = excluded.target_session_id,
                    link_role = excluded.link_role,
                    confidence = excluded.confidence,
                    evidence_json = excluded.evidence_json
                """,
                (
                    source_session_id,
                    target_id if exists else None,
                    link_type,
                    target_harness,
                    target_external_id,
                    str(link.get("link_role") or "unknown"),
                    str(link.get("confidence") or "observed"),
                    json.dumps(evidence or {}, separators=(",", ":")),
                ),
            )

    def _session_link_payloads(self, source_session_id: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for row in self.conn.execute(
            "SELECT link_type, target_harness, target_external_id, link_role, "
            "confidence, evidence_json FROM session_links "
            "WHERE source_session_id = ?",
            (source_session_id,),
        ):
            try:
                evidence = json.loads(row["evidence_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                evidence = {}
            out.append(
                {
                    "link_type": row["link_type"],
                    "target_harness": row["target_harness"],
                    "target_external_id": row["target_external_id"],
                    "link_role": row["link_role"],
                    "confidence": row["confidence"],
                    "evidence": evidence,
                }
            )
        return out

    def resolve_session_links(
        self, target_harness: str, target_external_id: str
    ) -> None:
        target_id = _sid(target_harness, target_external_id)
        self.conn.execute(
            """
            UPDATE session_links
            SET target_session_id = ?
            WHERE target_harness = ? AND target_external_id = ?
            """,
            (target_id, target_harness, target_external_id),
        )

    def _merge_session_metadata(
        self,
        session_id: str,
        result: ParseResult,
        *,
        artifact_id: int | None,
        replace_transcript_identity: bool = False,
        replace_source_identity: bool = False,
        replace_fork_provenance: bool = False,
    ) -> None:
        from agentlog.ingest.cursor import prefer_repo

        session = result.session
        row = self.conn.execute(
            """
            SELECT repo, cwd, parent_session_id, started_at,
                   originator, thread_source,
                   inherited_message_count, inherited_record_count,
                   fork_context_status, fork_context_boundary,
                   workflow_group_id, workflow_group_label, workflow_group_position
            FROM sessions WHERE id = ?
            """,
            (session_id,),
        ).fetchone()
        if row is None:
            return
        repo = prefer_repo(row["repo"], session.repo)
        cwd = session.cwd if repo == session.repo and session.cwd else row["cwd"]
        if repo != row["repo"] and session.cwd:
            cwd = session.cwd
        parent = (
            session.parent_session_id
            if replace_transcript_identity
            else session.parent_session_id or row["parent_session_id"]
        )
        started_at = (
            _iso(session.started_at)
            if replace_transcript_identity
            else row["started_at"]
        )
        originator = (
            session.originator
            if replace_source_identity
            else session.originator or row["originator"]
        )
        thread_source = (
            session.thread_source
            if replace_source_identity
            else session.thread_source or row["thread_source"]
        )
        extras = result.extras
        inherited_message_count = (
            _nonnegative_int(extras.get("inherited_message_count"))
            if replace_fork_provenance or "inherited_message_count" in extras
            else int(row["inherited_message_count"])
        )
        inherited_record_count = (
            _nonnegative_int(extras.get("inherited_record_count"))
            if replace_fork_provenance or "inherited_record_count" in extras
            else int(row["inherited_record_count"])
        )
        fork_context_status = (
            _optional_text(extras.get("fork_context_status"))
            if replace_fork_provenance or "fork_context_status" in extras
            else row["fork_context_status"]
        )
        fork_context_boundary = (
            _optional_text(extras.get("fork_context_boundary"))
            if replace_fork_provenance or "fork_context_boundary" in extras
            else row["fork_context_boundary"]
        )
        workflow_group = (
            _workflow_group(extras.get("workflow_group"))
            if replace_transcript_identity or "workflow_group" in extras
            else (
                row["workflow_group_id"],
                row["workflow_group_label"],
                row["workflow_group_position"],
            )
        )
        if replace_transcript_identity:
            ident = _identity_for(
                session.model,
                provider_hint=session.provider,
                agent_profile_hint=session.agent_profile,
            )
            self.conn.execute(
                """
                UPDATE sessions SET
                    parent_session_id = ?,
                    started_at = ?,
                    ended_at = ?,
                    repo = ?,
                    cwd = ?,
                    branch = ?,
                    commit_sha = ?,
                    model = ?,
                    model_canonical = ?,
                    provider = ?,
                    agent_profile = ?,
                    effort = ?,
                    effort_source = ?,
                    originator = ?,
                    thread_source = ?,
                    inherited_message_count = ?,
                    inherited_record_count = ?,
                    fork_context_status = ?,
                    fork_context_boundary = ?,
                    workflow_group_id = ?,
                    workflow_group_label = ?,
                    workflow_group_position = ?
                WHERE id = ?
                """,
                (
                    session.parent_session_id,
                    _iso(session.started_at),
                    _iso(session.ended_at),
                    session.repo,
                    session.cwd,
                    session.branch,
                    session.commit_sha,
                    session.model,
                    ident.canonical,
                    ident.provider,
                    ident.agent_profile,
                    session.effort,
                    session.effort_source,
                    session.originator,
                    session.thread_source,
                    _nonnegative_int(extras.get("inherited_message_count")),
                    _nonnegative_int(extras.get("inherited_record_count")),
                    _optional_text(extras.get("fork_context_status")),
                    _optional_text(extras.get("fork_context_boundary")),
                    *workflow_group,
                    session_id,
                ),
            )
            return
        if session.model is not None:
            ident = _identity_for(
                session.model,
                provider_hint=session.provider,
                agent_profile_hint=session.agent_profile,
            )
            self.conn.execute(
                """
                UPDATE sessions SET
                    started_at = ?,
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
                    parent_session_id = ?,
                    originator = ?,
                    thread_source = ?,
                    inherited_message_count = ?,
                    inherited_record_count = ?,
                    fork_context_status = ?,
                    fork_context_boundary = ?,
                    workflow_group_id = ?,
                    workflow_group_label = ?,
                    workflow_group_position = ?,
                    artifact_id = COALESCE(?, artifact_id)
                WHERE id = ?
                """,
                (
                    started_at,
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
                    originator,
                    thread_source,
                    inherited_message_count,
                    inherited_record_count,
                    fork_context_status,
                    fork_context_boundary,
                    *workflow_group,
                    artifact_id,
                    session_id,
                ),
            )
        else:
            self.conn.execute(
                """
                UPDATE sessions SET
                    started_at = ?,
                    ended_at = COALESCE(?, ended_at),
                    provider = COALESCE(?, provider),
                    agent_profile = COALESCE(?, agent_profile),
                    effort = COALESCE(?, effort),
                    effort_source = COALESCE(?, effort_source),
                    cwd = ?,
                    branch = COALESCE(?, branch),
                    commit_sha = COALESCE(?, commit_sha),
                    repo = ?,
                    parent_session_id = ?,
                    originator = ?,
                    thread_source = ?,
                    inherited_message_count = ?,
                    inherited_record_count = ?,
                    fork_context_status = ?,
                    fork_context_boundary = ?,
                    workflow_group_id = ?,
                    workflow_group_label = ?,
                    workflow_group_position = ?,
                    artifact_id = COALESCE(?, artifact_id)
                WHERE id = ?
                """,
                (
                    started_at,
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
                    originator,
                    thread_source,
                    inherited_message_count,
                    inherited_record_count,
                    fork_context_status,
                    fork_context_boundary,
                    *workflow_group,
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

    def _assert_source_prefix(
        self, session_id: str, result: ParseResult
    ) -> None:
        stored = self.conn.execute(
            "SELECT seq, role, content_hash FROM messages "
            "WHERE session_id = ? ORDER BY seq",
            (session_id,),
        ).fetchall()
        if len(stored) > len(result.messages):
            raise TranscriptStorageError(
                f"source-backed session {session_id} shrank during reparse"
            )
        for row, message in zip(stored, result.messages):
            if (
                int(row["seq"]) != message.seq
                or row["role"] != message.role
                or row["content_hash"] != message.content_hash
            ):
                raise TranscriptStorageError(
                    f"source-backed session {session_id} diverged during reparse"
                )

    def source_backed_artifact_promotion_status(
        self, *, artifact_id: int, result: ParseResult
    ) -> str:
        """Classify whether another artifact can become a session's source."""
        from agentlog.ingest.cursor import prefer_repo

        session_id = _sid(
            result.session.harness.value, result.session.external_id
        )
        row = self.conn.execute(
            "SELECT artifact_id, transcript_storage, repo FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if (
            row is None
            or row["transcript_storage"] != SOURCE_BACKED
            or row["artifact_id"] == artifact_id
        ):
            return "not_applicable"
        if self._artifact_storage(artifact_id) != SOURCE_BACKED:
            raise TranscriptStorageError(
                f"artifact {artifact_id} is not source-backed"
            )

        stored = self.conn.execute(
            "SELECT seq, role, content_hash FROM messages "
            "WHERE session_id = ? ORDER BY seq",
            (session_id,),
        ).fetchall()
        if len(result.messages) < len(stored):
            return "shrunk"
        for prior, incoming in zip(stored, result.messages):
            if (
                int(prior["seq"]) != incoming.seq
                or str(prior["role"]) != incoming.role
                or str(prior["content_hash"]) != incoming.content_hash
            ):
                return "diverged"
        if len(result.messages) > len(stored):
            return "extension"
        if prefer_repo(row["repo"], result.session.repo) != row["repo"]:
            return "metadata_upgrade"
        return "unchanged"

    def promote_source_backed_artifact(
        self,
        *,
        artifact_id: int,
        result: ParseResult,
        windows: Iterable[
            tuple[str, str, str]
            | tuple[str, str, str, str]
            | tuple[str, str, str, str, str]
        ],
    ) -> str | None:
        """Rebind a source-backed session to a safe, canonical duplicate."""
        session_id = _sid(
            result.session.harness.value, result.session.external_id
        )
        status = self.source_backed_artifact_promotion_status(
            artifact_id=artifact_id, result=result
        )
        if status not in {"extension", "metadata_upgrade"}:
            return None

        with _savepoint(self.conn, "promote_source_backed_artifact"):
            self.conn.execute(
                "UPDATE sessions SET artifact_id = ?, source_sync_status = 'current', "
                "source_sync_warning = NULL, source_sync_checked_at = ? WHERE id = ?",
                (artifact_id, datetime.now().astimezone().isoformat(), session_id),
            )
            promoted_id = self.save_parse_result(
                artifact_id=artifact_id,
                result=result,
                append=False,
                transcript_storage=SOURCE_BACKED,
            )
            self.replace_exchange_windows(promoted_id, windows)
        return promoted_id

    def save_parse_result(
        self,
        *,
        artifact_id: int,
        result: ParseResult,
        append: bool,
        base_seq: int = 0,
        base_tool_seq: int = 0,
        base_token_seq: int = 0,
        transcript_storage: str | None = None,
        preserve_existing_legacy: bool = False,
    ) -> str:
        session = result.session
        session_id = _sid(session.harness.value, session.external_id)
        artifact_storage = self._artifact_storage(artifact_id)
        transcript_storage = _transcript_storage(
            transcript_storage or artifact_storage,
            scope=f"session {session_id}",
        )
        existing_storage = self.session_transcript_storage(session_id)
        if (
            existing_storage is not None
            and existing_storage != transcript_storage
        ):
            raise TranscriptStorageError(
                f"session {session_id} cannot change transcript storage from "
                f"{existing_storage} to {transcript_storage}"
            )
        session_ident = _identity_for(
            session.model,
            provider_hint=session.provider,
            agent_profile_hint=session.agent_profile,
        )
        previous_links = (
            self._session_link_payloads(session_id) if not append else []
        )
        incoming_links = list(result.extras.get("session_links", []))
        links = previous_links + incoming_links
        inherited_message_count = _nonnegative_int(
            result.extras.get("inherited_message_count")
        )
        inherited_record_count = _nonnegative_int(
            result.extras.get("inherited_record_count")
        )
        fork_context_status = _optional_text(
            result.extras.get("fork_context_status")
        )
        fork_context_boundary = _optional_text(
            result.extras.get("fork_context_boundary")
        )
        workflow_group_id, workflow_group_label, workflow_group_position = _workflow_group(
            result.extras.get("workflow_group")
        )

        if (
            preserve_existing_legacy
            and existing_storage == LEGACY_MATERIALIZED
        ):
            if self.session_artifact_id(session_id) == artifact_id:
                self._merge_session_metadata(
                    session_id,
                    result,
                    artifact_id=None,
                    replace_source_identity=not append,
                    replace_fork_provenance=not append,
                )
                self.replace_session_links(session_id, links)
                self.resolve_session_links(
                    session.harness.value, session.external_id
                )
            return session_id

        if (
            existing_storage == SOURCE_BACKED
            and self.session_artifact_id(session_id) != artifact_id
        ):
            return session_id

        if not append:
            if existing_storage == SOURCE_BACKED:
                self._assert_source_prefix(session_id, result)
                self._merge_session_metadata(
                    session_id,
                    result,
                    artifact_id=None,
                    replace_transcript_identity=True,
                    replace_source_identity=True,
                    replace_fork_provenance=True,
                )
            else:
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
                        replace_source_identity=(
                            self.session_artifact_id(session_id) == artifact_id
                        ),
                        replace_fork_provenance=(
                            self.session_artifact_id(session_id) == artifact_id
                        ),
                    )
                    self.replace_session_links(
                        session_id,
                        links,
                    )
                    self.resolve_session_links(
                        session.harness.value, session.external_id
                    )
                    return session_id
                self.conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
                self.conn.execute(
                    """
                    INSERT INTO sessions (
                        id, harness, external_id, parent_session_id, artifact_id,
                        originator, thread_source,
                        inherited_message_count, inherited_record_count,
                        fork_context_status, fork_context_boundary,
                        workflow_group_id, workflow_group_label, workflow_group_position,
                        started_at, ended_at, repo, cwd, branch, commit_sha,
                        model, model_canonical, provider, agent_profile,
                        effort, effort_source, transcript_storage
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        session.harness.value,
                        session.external_id,
                        session.parent_session_id,
                        artifact_id,
                        session.originator,
                        session.thread_source,
                        inherited_message_count,
                        inherited_record_count,
                        fork_context_status,
                        fork_context_boundary,
                        workflow_group_id,
                        workflow_group_label,
                        workflow_group_position,
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
                        transcript_storage,
                    ),
                )
        else:
            self._merge_session_metadata(
                session_id,
                result,
                artifact_id=artifact_id,
            )

        self.replace_session_links(
            session_id,
            links,
        )
        self.resolve_session_links(session.harness.value, session.external_id)

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
                INSERT INTO messages (
                    id, session_id, seq, role, timestamp, model,
                    model_canonical, provider, agent_profile,
                    effort, effort_source, text, content_hash,
                    is_tool_plumbing, authored_by_agent
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    session_id = excluded.session_id,
                    seq = excluded.seq,
                    role = excluded.role,
                    timestamp = excluded.timestamp,
                    model = excluded.model,
                    model_canonical = excluded.model_canonical,
                    provider = excluded.provider,
                    agent_profile = excluded.agent_profile,
                    effort = excluded.effort,
                    effort_source = excluded.effort_source,
                    text = excluded.text,
                    content_hash = excluded.content_hash,
                    is_tool_plumbing = excluded.is_tool_plumbing,
                    authored_by_agent = excluded.authored_by_agent
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
                    "" if transcript_storage == SOURCE_BACKED else msg.text,
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
                INSERT INTO tool_events (
                    id, session_id, message_id, seq, tool_name, action, success,
                    duration_ms, operation_kind
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    session_id = excluded.session_id,
                    message_id = excluded.message_id,
                    seq = excluded.seq,
                    tool_name = excluded.tool_name,
                    action = excluded.action,
                    success = excluded.success,
                    duration_ms = excluded.duration_ms,
                    operation_kind = excluded.operation_kind
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
                    te.operation_kind,
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
                INSERT INTO skill_exposures (
                    id, session_id, message_id, skill_name, exposure_type
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    session_id = excluded.session_id,
                    message_id = excluded.message_id,
                    skill_name = excluded.skill_name,
                    exposure_type = excluded.exposure_type
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
                INSERT INTO token_usage (
                    id, session_id, message_id, seq, granularity, usage_source,
                    model, model_canonical, input_tokens, output_tokens,
                    cache_creation_input_tokens, cache_read_input_tokens,
                    cached_input_tokens, cache_write_input_tokens,
                    reasoning_output_tokens, total_tokens, timestamp, extras_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    session_id = excluded.session_id,
                    message_id = excluded.message_id,
                    seq = excluded.seq,
                    granularity = excluded.granularity,
                    usage_source = excluded.usage_source,
                    model = excluded.model,
                    model_canonical = excluded.model_canonical,
                    input_tokens = excluded.input_tokens,
                    output_tokens = excluded.output_tokens,
                    cache_creation_input_tokens = excluded.cache_creation_input_tokens,
                    cache_read_input_tokens = excluded.cache_read_input_tokens,
                    cached_input_tokens = excluded.cached_input_tokens,
                    cache_write_input_tokens = excluded.cache_write_input_tokens,
                    reasoning_output_tokens = excluded.reasoning_output_tokens,
                    total_tokens = excluded.total_tokens,
                    timestamp = excluded.timestamp,
                    extras_json = excluded.extras_json
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

        tail = next(
            (
                message
                for message in reversed(result.messages)
                if not message.is_tool_plumbing
            ),
            None,
        )
        if tail is not None:
            from agentlog.analysis.attention_signals import last_attention_signal

            signal = last_attention_signal(result.messages)
            self.conn.execute(
                """
                UPDATE sessions SET
                    attention_last_substantive_seq = ?,
                    attention_last_substantive_role = ?,
                    attention_last_substantive_at = ?,
                    attention_final_question = ?,
                    attention_incomplete_todo = ?,
                    attention_tail_revision = 1
                WHERE id = ?
                """,
                (
                    base_seq + tail.seq,
                    tail.role,
                    _iso(tail.timestamp),
                    int(signal == "question"),
                    int(signal == "incomplete_todo"),
                    session_id,
                ),
            )
        last_tool = self.conn.execute(
            """
            SELECT tool_name FROM tool_events
            WHERE session_id = ? ORDER BY seq DESC LIMIT 1
            """,
            (session_id,),
        ).fetchone()
        last_role = self.conn.execute(
            """
            SELECT role FROM messages
            WHERE session_id = ? AND COALESCE(is_tool_plumbing, 0) = 0
            ORDER BY seq DESC LIMIT 1
            """,
            (session_id,),
        ).fetchone()
        self.conn.execute(
            "UPDATE sessions SET attention_last_plan_open = ? WHERE id = ?",
            (
                int(
                    last_tool is not None
                    and str(last_tool["tool_name"]) in {"TodoWrite", "update_plan"}
                    and last_role is not None
                    and str(last_role["role"]) != "user"
                ),
                session_id,
            ),
        )

        return session_id

    def assert_no_claim_evidence_for_transcript_rewrite(
        self, session_id: str
    ) -> None:
        evidence = self.conn.execute(
            """
            SELECT ce.id
            FROM claim_evidence ce
            WHERE ce.session_id = ?
               OR instr(COALESCE(ce.message_id, ''), ? || ':m:') = 1
               OR ce.message_id IN (
                    SELECT id FROM messages WHERE session_id = ?
                  )
               OR ce.window_id IN (
                    SELECT id FROM exchange_windows WHERE session_id = ?
                  )
            LIMIT 1
            """,
            (session_id, session_id, session_id, session_id),
        ).fetchone()
        if evidence is not None:
            raise TranscriptStorageError(
                f"source-backed session {session_id} has claim evidence; "
                "transcript rewrite refused"
            )

    def transcript_rewrite_block_reason(self, session_id: str) -> str | None:
        for check in (
            self.assert_no_claim_evidence_for_transcript_rewrite,
            self.assert_no_owner_insight_provenance_for_transcript_rewrite,
            self._assert_no_task_cluster_for_transcript_rewrite,
        ):
            try:
                check(session_id)
            except TranscriptStorageError as exc:
                return str(exc)
        return None

    def parser_upgrade_freeze_snapshot(
        self,
        *,
        artifact_id: int,
        previous_parser_version: str,
        target_parser_version: str,
    ) -> sqlite3.Row | None:
        return self.conn.execute(
            """
            SELECT source_size, source_mtime_ns, source_content_hash,
                   source_parsed_offset
            FROM parser_upgrade_freezes
            WHERE artifact_id = ?
              AND previous_parser_version = ?
              AND target_parser_version = ?
            """,
            (artifact_id, previous_parser_version, target_parser_version),
        ).fetchone()

    def freeze_parser_upgrade(
        self,
        *,
        artifact_id: int,
        previous_parser_version: str,
        target_parser_version: str,
        source_size: int,
        source_mtime_ns: int,
        source_content_hash: str,
        source_parsed_offset: int,
        reason: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO parser_upgrade_freezes(
                artifact_id, previous_parser_version, target_parser_version,
                source_size, source_mtime_ns, source_content_hash,
                source_parsed_offset, reason, frozen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(artifact_id) DO UPDATE SET
                previous_parser_version = excluded.previous_parser_version,
                target_parser_version = excluded.target_parser_version,
                source_size = excluded.source_size,
                source_mtime_ns = excluded.source_mtime_ns,
                source_content_hash = excluded.source_content_hash,
                source_parsed_offset = excluded.source_parsed_offset,
                reason = excluded.reason,
                frozen_at = excluded.frozen_at
            """,
            (
                artifact_id,
                previous_parser_version,
                target_parser_version,
                source_size,
                source_mtime_ns,
                source_content_hash,
                source_parsed_offset,
                reason,
                datetime.now().astimezone().isoformat(),
            ),
        )

    def clear_parser_upgrade_freeze(self, artifact_id: int) -> None:
        self.conn.execute(
            "DELETE FROM parser_upgrade_freezes WHERE artifact_id = ?",
            (artifact_id,),
        )

    def source_backed_session_ids_for_artifact(self, artifact_id: int) -> list[str]:
        return [
            str(row["id"])
            for row in self.conn.execute(
                """
                SELECT id FROM sessions
                WHERE artifact_id = ? AND transcript_storage = ?
                ORDER BY id
                """,
                (artifact_id, SOURCE_BACKED),
            )
        ]

    def clear_frozen_parser_upgrade_diagnostics(self, artifact_id: int) -> None:
        self.conn.execute(
            """
            UPDATE sessions
            SET source_sync_status = 'current', source_sync_warning = NULL,
                source_sync_checked_at = ?
            WHERE artifact_id = ? AND source_sync_status = 'frozen_parser_upgrade'
            """,
            (datetime.now().astimezone().isoformat(), artifact_id),
        )

    def source_backed_parse_result_is_exact(
        self,
        *,
        artifact_id: int,
        result: ParseResult,
        windows: Iterable[tuple[str, str, str, str, str]],
    ) -> bool:
        session = result.session
        session_id = _sid(session.harness.value, session.external_id)
        row = self.conn.execute(
            """
            SELECT * FROM sessions
            WHERE id = ? AND artifact_id = ? AND transcript_storage = ?
            """,
            (session_id, artifact_id, SOURCE_BACKED),
        ).fetchone()
        if row is None:
            return False
        session_ident = _identity_for(
            session.model,
            provider_hint=session.provider,
            agent_profile_hint=session.agent_profile,
        )
        workflow_group = _workflow_group(result.extras.get("workflow_group"))
        expected_session = (
            session.parent_session_id,
            session.originator,
            session.thread_source,
            _nonnegative_int(result.extras.get("inherited_message_count")),
            _nonnegative_int(result.extras.get("inherited_record_count")),
            _optional_text(result.extras.get("fork_context_status")),
            _optional_text(result.extras.get("fork_context_boundary")),
            *workflow_group,
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
        )
        actual_session = tuple(
            row[column]
            for column in (
                "parent_session_id",
                "originator",
                "thread_source",
                "inherited_message_count",
                "inherited_record_count",
                "fork_context_status",
                "fork_context_boundary",
                "workflow_group_id",
                "workflow_group_label",
                "workflow_group_position",
                "started_at",
                "ended_at",
                "repo",
                "cwd",
                "branch",
                "commit_sha",
                "model",
                "model_canonical",
                "provider",
                "agent_profile",
                "effort",
                "effort_source",
            )
        )
        if actual_session != expected_session:
            return False

        expected_messages = []
        for message in result.messages:
            message_ident = (
                _identity_for(
                    message.model,
                    provider_hint=message.provider or session.provider,
                    agent_profile_hint=(
                        message.agent_profile or session.agent_profile
                    ),
                )
                if message.model
                else ModelIdentity(None, None, None, None, None)
            )
            expected_messages.append(
                (
                    _mid(session_id, message.seq),
                    message.seq,
                    message.role,
                    _iso(message.timestamp),
                    message.model,
                    message_ident.canonical,
                    message_ident.provider,
                    message_ident.agent_profile,
                    message.effort,
                    message.effort_source,
                    "",
                    message.content_hash,
                    int(message.is_tool_plumbing),
                    int(message.authored_by_agent),
                )
            )
        actual_messages = [
            tuple(item)
            for item in self.conn.execute(
                """
                SELECT id, seq, role, timestamp, model, model_canonical, provider,
                       agent_profile, effort, effort_source, text, content_hash,
                       is_tool_plumbing, authored_by_agent
                FROM messages WHERE session_id = ? ORDER BY seq
                """,
                (session_id,),
            )
        ]
        if actual_messages != expected_messages:
            return False

        expected_tools = [
            (
                _tid(session_id, tool.seq),
                _mid(session_id, tool.message_seq)
                if tool.message_seq is not None
                else None,
                tool.seq,
                tool.tool_name,
                tool.action,
                None if tool.success is None else int(tool.success),
                tool.duration_ms,
                tool.operation_kind,
            )
            for tool in result.tool_events
        ]
        actual_tools = [
            tuple(item)
            for item in self.conn.execute(
                """
                SELECT id, message_id, seq, tool_name, action, success, duration_ms,
                       operation_kind
                FROM tool_events WHERE session_id = ? ORDER BY seq
                """,
                (session_id,),
            )
        ]
        if actual_tools != expected_tools:
            return False

        expected_skills = sorted(
            (
                _kid(session_id, index, skill.skill_name),
                _mid(session_id, skill.message_seq)
                if skill.message_seq is not None
                else None,
                skill.skill_name,
                skill.exposure_type,
            )
            for index, skill in enumerate(result.skill_exposures)
        )
        actual_skills = [
            tuple(item)
            for item in self.conn.execute(
                """
                SELECT id, message_id, skill_name, exposure_type
                FROM skill_exposures WHERE session_id = ? ORDER BY id
                """,
                (session_id,),
            )
        ]
        if actual_skills != expected_skills:
            return False

        expected_usage = sorted(
            (
                _uid(session_id, usage.usage_source, usage.seq),
                _mid(session_id, usage.message_seq)
                if usage.message_seq is not None
                else None,
                usage.seq,
                usage.granularity,
                usage.usage_source,
                usage.model,
                _identity_for(
                    usage.model,
                    provider_hint=session.provider,
                    agent_profile_hint=session.agent_profile,
                ).canonical,
                usage.input_tokens,
                usage.output_tokens,
                usage.cache_creation_input_tokens,
                usage.cache_read_input_tokens,
                usage.cached_input_tokens,
                usage.cache_write_input_tokens,
                usage.reasoning_output_tokens,
                usage.total_tokens,
                _iso(usage.timestamp),
                json.dumps(usage.extras, separators=(",", ":"), default=str),
            )
            for usage in result.token_usages
        )
        actual_usage = [
            tuple(item)
            for item in self.conn.execute(
                """
                SELECT id, message_id, seq, granularity, usage_source, model,
                       model_canonical, input_tokens, output_tokens,
                       cache_creation_input_tokens, cache_read_input_tokens,
                       cached_input_tokens, cache_write_input_tokens,
                       reasoning_output_tokens, total_tokens, timestamp, extras_json
                FROM token_usage WHERE session_id = ? ORDER BY id
                """,
                (session_id,),
            )
        ]
        if actual_usage != expected_usage:
            return False

        expected_windows = sorted(
            (wid, request_id, response_id, input_hash, content_hash)
            for request_id, response_id, input_hash, content_hash, wid in windows
        )
        actual_windows = [
            tuple(item)
            for item in self.conn.execute(
                """
                SELECT id, request_message_id, response_message_id, input_hash,
                       content_hash
                FROM exchange_windows WHERE session_id = ? ORDER BY id
                """,
                (session_id,),
            )
        ]
        if actual_windows != expected_windows:
            return False

        expected_links = {
            (str(link["link_type"]), str(link["target_harness"]), str(link["target_external_id"])): (
                str(link["link_role"]),
                str(link["confidence"]),
                str(link["evidence_json"]),
            )
            for link in self.conn.execute(
                """
                SELECT link_type, target_harness, target_external_id, link_role,
                       confidence, evidence_json
                FROM session_links WHERE source_session_id = ?
                """,
                (session_id,),
            )
        }
        for link in result.extras.get("session_links", []):
            if not isinstance(link, dict):
                return False
            link_type = str(link.get("link_type") or "").strip()
            target_harness = str(link.get("target_harness") or "").strip()
            target_external_id = str(link.get("target_external_id") or "").strip()
            if not link_type or not target_harness or not target_external_id:
                continue
            expected_links[(link_type, target_harness, target_external_id)] = (
                str(link.get("link_role") or "unknown"),
                str(link.get("confidence") or "observed"),
                json.dumps(link.get("evidence") or {}, separators=(",", ":")),
            )
        actual_links = {
            (str(link["link_type"]), str(link["target_harness"]), str(link["target_external_id"])): (
                str(link["link_role"]),
                str(link["confidence"]),
                str(link["evidence_json"]),
            )
            for link in self.conn.execute(
                """
                SELECT link_type, target_harness, target_external_id, link_role,
                       confidence, evidence_json
                FROM session_links WHERE source_session_id = ?
                """,
                (session_id,),
            )
        }
        if actual_links != expected_links:
            return False

        from agentlog.analysis.attention_signals import last_attention_signal

        tail = next(
            (message for message in reversed(result.messages) if not message.is_tool_plumbing),
            None,
        )
        latest_tool = max(result.tool_events, key=lambda tool: tool.seq, default=None)
        latest_role = tail.role if tail is not None else None
        expected_attention = (
            tail.seq if tail is not None else row["attention_last_substantive_seq"],
            tail.role if tail is not None else row["attention_last_substantive_role"],
            _iso(tail.timestamp) if tail is not None else row["attention_last_substantive_at"],
            int(last_attention_signal(result.messages) == "question")
            if tail is not None
            else row["attention_final_question"],
            int(last_attention_signal(result.messages) == "incomplete_todo")
            if tail is not None
            else row["attention_incomplete_todo"],
            1 if tail is not None else row["attention_tail_revision"],
            int(
                latest_tool is not None
                and latest_tool.tool_name in {"TodoWrite", "update_plan"}
                and latest_role is not None
                and latest_role != "user"
            ),
        )
        actual_attention = tuple(
            row[column]
            for column in (
                "attention_last_substantive_seq",
                "attention_last_substantive_role",
                "attention_last_substantive_at",
                "attention_final_question",
                "attention_incomplete_todo",
                "attention_tail_revision",
                "attention_last_plan_open",
            )
        )
        return actual_attention == expected_attention

    def assert_no_owner_insight_provenance_for_transcript_rewrite(
        self, session_id: str
    ) -> None:
        evidence = self.conn.execute(
            """
            SELECT 'batch_message' AS source
            FROM owner_insight_batch_messages bm
            JOIN owner_insight_batches b ON b.id = bm.batch_id
            WHERE bm.session_id = ? AND b.status IN ('prepared', 'imported')
            UNION ALL
            SELECT 'seen_message' AS source
            FROM owner_insight_seen_messages sm
            JOIN owner_insight_session_state ss ON ss.session_id = sm.session_id
            WHERE sm.session_id = ?
              AND (
                  sm.status = 'imported'
                  OR (sm.status = 'prepared' AND sm.generation = ss.generation)
              )
            LIMIT 1
            """,
            (session_id, session_id),
        ).fetchone()
        if evidence is not None:
            raise TranscriptStorageError(
                f"source-backed session {session_id} has owner insight provenance; "
                "transcript rewrite refused"
            )

    def legacy_continuation_status(
        self, *, artifact_id: int, result: ParseResult
    ) -> str | None:
        session_id = _sid(
            result.session.harness.value, result.session.external_id
        )
        row = self.conn.execute(
            "SELECT artifact_id, transcript_storage FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if (
            row is None
            or row["transcript_storage"] != LEGACY_MATERIALIZED
            or row["artifact_id"] != artifact_id
        ):
            return None
        stored = self.conn.execute(
            "SELECT seq, role, content_hash FROM messages "
            "WHERE session_id = ? ORDER BY seq",
            (session_id,),
        ).fetchall()
        if len(result.messages) < len(stored):
            return "shrunk"
        for row, message in zip(stored, result.messages):
            if (
                int(row["seq"]) != message.seq
                or str(row["role"]) != message.role
                or str(row["content_hash"]) != message.content_hash
            ):
                return "diverged"
        return "extension" if len(result.messages) > len(stored) else "unchanged"

    def record_source_sync_diagnostic(
        self, session_id: str, *, status: str, warning: str | None
    ) -> None:
        self.conn.execute(
            "UPDATE sessions SET source_sync_status = ?, "
            "source_sync_warning = ?, source_sync_checked_at = ? WHERE id = ?",
            (
                status,
                warning,
                datetime.now().astimezone().isoformat(),
                session_id,
            ),
        )

    def _assert_no_task_cluster_for_transcript_rewrite(
        self, session_id: str
    ) -> None:
        cluster = self.conn.execute(
            """
            SELECT tc.id
            FROM task_clusters tc
            WHERE tc.root_session_id = ?
               OR tc.segment_start_message_id IN (
                    SELECT id FROM messages WHERE session_id = ?
                  )
               OR tc.segment_end_message_id IN (
                    SELECT id FROM messages WHERE session_id = ?
                  )
            LIMIT 1
            """,
            (session_id, session_id, session_id),
        ).fetchone()
        if cluster is not None:
            raise TranscriptStorageError(
                f"session {session_id} has a derived task cluster; "
                "transcript rewrite refused"
            )

    def promote_legacy_continuation(
        self,
        *,
        artifact_id: int,
        result: ParseResult,
        windows: Iterable[
            tuple[str, str, str]
            | tuple[str, str, str, str]
            | tuple[str, str, str, str, str]
        ],
    ) -> str | None:
        session_id = _sid(
            result.session.harness.value, result.session.external_id
        )
        status = self.legacy_continuation_status(
            artifact_id=artifact_id, result=result
        )
        if status in {"diverged", "shrunk"}:
            verb = "shrank" if status == "shrunk" else "diverged"
            raise TranscriptStorageError(
                f"legacy session {session_id} {verb} in its canonical source"
            )
        if status != "extension":
            return None
        self.assert_no_claim_evidence_for_transcript_rewrite(session_id)
        self._assert_no_task_cluster_for_transcript_rewrite(session_id)

        with _savepoint(self.conn, "promote_legacy_continuation"):
            for table in ("tool_events", "skill_exposures", "token_usage"):
                self.conn.execute(
                    f"DELETE FROM {table} WHERE session_id = ?",
                    (session_id,),
                )
            self.conn.execute(
                "UPDATE messages SET text = '' WHERE session_id = ?",
                (session_id,),
            )
            self.conn.execute(
                "UPDATE sessions SET transcript_storage = ?, "
                "source_sync_status = 'current', source_sync_warning = NULL, "
                "source_sync_checked_at = ? WHERE id = ?",
                (
                    SOURCE_BACKED,
                    datetime.now().astimezone().isoformat(),
                    session_id,
                ),
            )
            promoted_id = self.save_parse_result(
                artifact_id=artifact_id,
                result=result,
                append=False,
                transcript_storage=SOURCE_BACKED,
            )
            self.replace_exchange_windows(promoted_id, windows)
        return promoted_id

    def rewrite_source_backed_parse_result(
        self,
        *,
        artifact_id: int,
        result: ParseResult,
        windows: Iterable[
            tuple[str, str, str]
            | tuple[str, str, str, str]
            | tuple[str, str, str, str, str]
        ],
        previous_parser_version: str,
        current_parser_version: str,
    ) -> str:
        session = result.session
        session_id = _sid(session.harness.value, session.external_id)
        if previous_parser_version == current_parser_version:
            raise TranscriptStorageError(
                f"source-backed session {session_id} requires a parser version "
                "change for an exact rewrite"
            )
        if self.session_transcript_storage(session_id) != SOURCE_BACKED:
            raise TranscriptStorageError(
                f"session {session_id} is not source-backed"
            )
        if self.session_artifact_id(session_id) != artifact_id:
            raise TranscriptStorageError(
                f"source-backed session {session_id} belongs to another artifact"
            )
        self.assert_no_claim_evidence_for_transcript_rewrite(session_id)
        self.assert_no_owner_insight_provenance_for_transcript_rewrite(session_id)
        self._assert_no_task_cluster_for_transcript_rewrite(session_id)

        with _savepoint(self.conn, "rewrite_source_backed_parse_result"):
            self.conn.execute(
                "DELETE FROM exchange_windows WHERE session_id = ?",
                (session_id,),
            )
            for table in (
                "tool_events",
                "skill_exposures",
                "token_usage",
                "messages",
            ):
                self.conn.execute(
                    f"DELETE FROM {table} WHERE session_id = ?",
                    (session_id,),
                )
            rewritten_id = self.save_parse_result(
                artifact_id=artifact_id,
                result=result,
                append=False,
                transcript_storage=SOURCE_BACKED,
            )
            self.replace_exchange_windows(rewritten_id, windows)
        return rewritten_id

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
        visible_session_sql = (
            "COALESCE(thread_source, '') NOT IN "
            f"('{INTERNAL_APPROVAL_GUARDIAN_THREAD_SOURCE}', "
            f"'{GROK_BOOTSTRAP_ONLY_THREAD_SOURCE}')"
        )
        by_harness = list(
            self.conn.execute(
                f"""
                SELECT harness, COUNT(*) AS sessions
                FROM sessions
                WHERE {visible_session_sql}
                GROUP BY harness
                ORDER BY harness
                """
            )
        )
        messages = self.conn.execute(
            f"SELECT COUNT(*) AS c FROM messages m JOIN sessions s ON s.id = m.session_id WHERE {visible_session_sql.replace('thread_source', 's.thread_source')}"
        ).fetchone()
        tools = self.conn.execute(
            f"SELECT COUNT(*) AS c FROM tool_events t JOIN sessions s ON s.id = t.session_id WHERE {visible_session_sql.replace('thread_source', 's.thread_source')}"
        ).fetchone()
        skills = self.conn.execute(
            f"SELECT COUNT(*) AS c FROM skill_exposures e JOIN sessions s ON s.id = e.session_id WHERE {visible_session_sql.replace('thread_source', 's.thread_source')}"
        ).fetchone()
        artifacts = self.conn.execute("SELECT COUNT(*) AS c FROM artifacts").fetchone()
        windows = self.conn.execute(
            f"SELECT COUNT(*) AS c FROM exchange_windows w JOIN sessions s ON s.id = w.session_id WHERE {visible_session_sql.replace('thread_source', 's.thread_source')}"
        ).fetchone()
        by_model = list(
            self.conn.execute(
                f"""
                SELECT COALESCE(model, '(unknown)') AS model, COUNT(*) AS sessions
                FROM sessions
                WHERE {visible_session_sql}
                GROUP BY model
                ORDER BY sessions DESC
                LIMIT 20
                """
            )
        )
        date_range = self.conn.execute(
            f"""
            SELECT MIN(started_at) AS first_at, MAX(COALESCE(ended_at, started_at)) AS last_at
            FROM sessions
            WHERE {visible_session_sql}
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
        suppressed_sources = sorted(SUPPRESSED_ACTIVITY_THREAD_SOURCES)
        placeholders = ", ".join("?" for _ in suppressed_sources)
        clauses = [f"COALESCE(thread_source, '') NOT IN ({placeholders})"]
        params: list[Any] = list(suppressed_sources)
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
        row = self.conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        return None if row is not None and is_suppressed_activity_session(row) else row

    def search_messages(self, query: str, limit: int = 30) -> list[sqlite3.Row]:
        from datetime import datetime, timezone

        from agentlog.api.ranges import TimeRange
        from agentlog.api.search import search_messages as dual_search_messages
        from agentlog.source_reader import read_source_transcript

        now = datetime.now(timezone.utc)
        result = dual_search_messages(
            self.conn,
            TimeRange("all", None, now, None, now),
            q=query,
            limit=limit,
            source_reader=read_source_transcript,
        )
        rows: list[sqlite3.Row] = []
        for item in result["items"]:
            physical_id = str(item.get("physical_session_id") or item["session_id"])
            session = self.conn.execute(
                "SELECT cwd FROM sessions WHERE id = ?", (physical_id,)
            ).fetchone()
            snippet = str(item.get("snippet") or "").replace("«", "[").replace("»", "]")
            rows.append(
                self.conn.execute(
                    """
                    SELECT :message_id AS id, :session_id AS session_id,
                           :seq AS seq, :role AS role, :timestamp AS timestamp,
                           :model AS model, :snippet AS snippet,
                           :harness AS harness, :cwd AS cwd
                    """,
                    {
                        "message_id": item.get("message_id"),
                        "session_id": item["session_id"],
                        "seq": item.get("seq"),
                        "role": item.get("role"),
                        "timestamp": item.get("timestamp"),
                        "model": item.get("model"),
                        "snippet": snippet,
                        "harness": item.get("harness"),
                        "cwd": session["cwd"] if session else None,
                    },
                ).fetchone()
            )
        return rows
