"""Read source-backed transcripts on demand without persisting their text."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from agentlog.ingest.base import (
    TranscriptAdapter,
    file_stat,
    hash_prefix,
    is_sqlite_path,
    sqlite_fingerprint,
)
from agentlog.ingest.claude import ClaudeAdapter
from agentlog.ingest.codex import CodexAdapter
from agentlog.ingest.cursor import CursorAdapter
from agentlog.ingest.hermes import HermesAdapter
from agentlog.ingest.t3code import T3CodeAdapter
from agentlog.ingest.warp import WarpAdapter
from agentlog.normalize.model_identity import resolve_model_identity
from agentlog.normalize.models import Harness, NormalizedMessage, ParseResult

SourceReadStatus = Literal[
    "ready", "legacy", "source_unavailable", "source_changed"
]


@dataclass(frozen=True)
class SourceLocator:
    harness: str
    artifact_path: str
    artifact_kind: Literal["jsonl", "sqlite"]
    unit_id: str


@dataclass(frozen=True)
class SourceReadResult:
    status: SourceReadStatus
    messages: list[dict[str, Any]]
    locator: SourceLocator | None = None
    source_unit_id: str | None = None
    source_identity: str | None = None
    source_hash: str | None = None
    warning: str | None = None

    @property
    def ready(self) -> bool:
        return self.status == "ready"


@dataclass(frozen=True)
class _CachedSourceParse:
    revision: tuple[int, int]
    source_hash: str
    results: list[ParseResult]


class CachedSourceTranscriptReader:
    def __init__(self) -> None:
        self._artifacts: dict[tuple[str, str], _CachedSourceParse] = {}

    def verify_current(self) -> bool:
        """Confirm every source used by this operation is still unchanged."""
        try:
            for (harness, artifact_path), cached in self._artifacts.items():
                path = Path(artifact_path)
                if not path.is_file() or file_stat(path) != cached.revision:
                    return False
                if _current_hash(path) != cached.source_hash:
                    return False
        except (OSError, sqlite3.Error, ValueError):
            return False
        return True

    def __call__(
        self, conn: sqlite3.Connection, session_id: str
    ) -> SourceReadResult:
        return _read_source_transcript(
            conn, session_id, artifact_cache=self._artifacts
        )

    def read_source_transcript(
        self, conn: sqlite3.Connection, session_id: str
    ) -> SourceReadResult:
        return self(conn, session_id)


_ADAPTERS: dict[str, type[TranscriptAdapter]] = {
    Harness.CODEX.value: CodexAdapter,
    Harness.CLAUDE.value: ClaudeAdapter,
    Harness.CURSOR.value: CursorAdapter,
    Harness.T3CODE.value: T3CodeAdapter,
    Harness.WARP.value: WarpAdapter,
    Harness.HERMES.value: HermesAdapter,
}


def _source_identity(locator: SourceLocator) -> str:
    return hashlib.sha256(
        f"agentlog-source-v1\0{locator.harness}\0{locator.unit_id}\0{locator.artifact_path}".encode()
    ).hexdigest()


def _source_unit_id(row: sqlite3.Row) -> str:
    return f"{row['harness']}:{row['external_id']}"


def _is_source_backed(row: sqlite3.Row) -> bool:
    keys = set(row.keys())
    return (
        "transcript_storage" in keys
        and row["transcript_storage"] == "source_backed"
    )


def _source_locator(row: sqlite3.Row) -> tuple[SourceLocator | None, str | None]:
    harness = str(row["harness"] or "")
    if str(row["artifact_harness"] or "") != harness:
        return None, "artifact harness does not match the session identity"
    artifact_path = row["artifact_path"]
    if not isinstance(artifact_path, str) or not artifact_path:
        return None, "source-backed session has no artifact"
    path = Path(artifact_path)
    return (
        SourceLocator(
            harness=harness,
            artifact_path=str(path.resolve(strict=False)),
            artifact_kind="sqlite" if is_sqlite_path(path) else "jsonl",
            unit_id=_source_unit_id(row),
        ),
        None,
    )


def _checkpoint_is_current(row: sqlite3.Row, path: Path) -> bool:
    if is_sqlite_path(path):
        return True
    checkpoint = int(row["parsed_offset"] or 0)
    expected = str(row["artifact_content_hash"] or "")
    if checkpoint == 0 or not expected:
        return True
    try:
        return path.stat().st_size >= checkpoint and hash_prefix(path, checkpoint) == expected
    except OSError:
        return False


def _current_hash(path: Path) -> str:
    if is_sqlite_path(path):
        return sqlite_fingerprint(path)
    return hash_prefix(path, path.stat().st_size)


def _parse_current(
    adapter: TranscriptAdapter, path: Path
) -> tuple[list[ParseResult], str] | None:
    for _attempt in range(3):
        before = file_stat(path)
        data = b"" if is_sqlite_path(path) else path.read_bytes()
        results = adapter.parse_path(path, data, start_offset=0)
        after_parse = file_stat(path)
        if before != after_parse:
            continue
        source_hash = _current_hash(path)
        after_hash = file_stat(path)
        if before == after_hash:
            return results, source_hash
    return None


def _message_dict(session_id: str, message: NormalizedMessage) -> dict[str, Any]:
    identity = resolve_model_identity(
        message.model,
        provider_hint=message.provider,
        agent_profile_hint=message.agent_profile,
    )
    return {
        "id": f"{session_id}:m:{message.seq}",
        "seq": message.seq,
        "role": message.role,
        "timestamp": message.timestamp.isoformat() if message.timestamp else None,
        "model": identity.raw,
        "model_canonical": identity.canonical,
        "effort": message.effort,
        "text": message.text,
        "content_hash": message.content_hash,
        "is_tool_plumbing": message.is_tool_plumbing,
        "authored_by_agent": message.authored_by_agent,
    }


def _metadata_is_prefix(
    conn: sqlite3.Connection, session_id: str, result: ParseResult
) -> bool:
    stored = conn.execute(
        """
        SELECT seq, role, content_hash
        FROM messages
        WHERE session_id = ?
        ORDER BY seq
        """,
        (session_id,),
    ).fetchall()
    if len(stored) > len(result.messages):
        return False
    return all(
        int(row["seq"]) == message.seq
        and str(row["role"]) == message.role
        and str(row["content_hash"]) == message.content_hash
        for row, message in zip(stored, result.messages)
    )


def read_source_transcript(
    conn: sqlite3.Connection, session_id: str
) -> SourceReadResult:
    return _read_source_transcript(conn, session_id, artifact_cache=None)


def _read_source_transcript(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    artifact_cache: dict[tuple[str, str], _CachedSourceParse] | None,
) -> SourceReadResult:
    """Return current normalized messages for a source-backed session.

    The artifact checkpoint verifies that a JSONL source's ingested prefix was
    not rewritten, while allowing later complete lines to be visible at once.
    """
    row = conn.execute(
        """
        SELECT s.*, a.path AS artifact_path, a.harness AS artifact_harness,
               a.content_hash AS artifact_content_hash, a.parsed_offset
        FROM sessions s
        LEFT JOIN artifacts a ON a.id = s.artifact_id
        WHERE s.id = ?
        """,
        (session_id,),
    ).fetchone()
    if row is None:
        return SourceReadResult("source_unavailable", [], warning="session not found")

    if not _is_source_backed(row):
        return SourceReadResult("legacy", [])
    locator, validation_error = _source_locator(row)
    if locator is None:
        return SourceReadResult("source_unavailable", [], warning=validation_error)
    path = Path(locator.artifact_path)
    if not path.is_file():
        return SourceReadResult("source_unavailable", [], locator, locator.unit_id, warning="canonical source is missing")
    if not _checkpoint_is_current(row, path):
        return SourceReadResult(
            "source_changed", [], locator, locator.unit_id, warning="canonical source changed before its checkpoint"
        )

    adapter_type = _ADAPTERS.get(str(row["harness"]))
    if adapter_type is None:
        return SourceReadResult("source_unavailable", [], locator, locator.unit_id, warning="unsupported source harness")
    cache_key = (locator.harness, locator.artifact_path)
    cached = artifact_cache.get(cache_key) if artifact_cache is not None else None
    try:
        if cached is not None:
            if file_stat(path) != cached.revision:
                return SourceReadResult(
                    "source_changed",
                    [],
                    locator,
                    locator.unit_id,
                    warning="canonical source changed during this operation",
                )
            parsed = (cached.results, cached.source_hash)
        else:
            parsed = _parse_current(adapter_type(), path)
    except (OSError, sqlite3.Error, ValueError) as exc:
        return SourceReadResult("source_unavailable", [], locator, locator.unit_id, warning=f"could not read canonical source: {exc}")
    if parsed is None:
        return SourceReadResult("source_changed", [], locator, locator.unit_id, warning="canonical source changed while being read")
    results, source_hash = parsed
    if artifact_cache is not None and cached is None:
        artifact_cache[cache_key] = _CachedSourceParse(
            revision=file_stat(path),
            source_hash=source_hash,
            results=results,
        )
    external_id = str(row["external_id"])
    matches = [item for item in results if item.session.external_id == external_id]
    if len(matches) != 1:
        return SourceReadResult(
            "source_changed",
            [],
            locator,
            locator.unit_id,
            warning="canonical source no longer contains this session identity",
        )
    if matches[0].extras.get("checkpoint_blocked") is True:
        reason = str(
            matches[0].extras.get("checkpoint_blocked_reason")
            or "canonical source is unsafe to checkpoint"
        )
        return SourceReadResult(
            "source_changed",
            [],
            locator,
            locator.unit_id,
            warning=reason,
        )
    if not _metadata_is_prefix(conn, session_id, matches[0]):
        return SourceReadResult(
            "source_changed",
            [],
            locator,
            locator.unit_id,
            warning="canonical source diverges from persisted message metadata",
        )
    return SourceReadResult(
        "ready",
        [_message_dict(session_id, message) for message in matches[0].messages],
        locator=locator,
        source_unit_id=locator.unit_id,
        source_identity=_source_identity(locator),
        source_hash=source_hash,
    )
