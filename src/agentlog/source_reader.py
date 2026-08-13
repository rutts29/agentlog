"""Read source-backed transcripts on demand without persisting their text."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import OrderedDict
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, RLock
from typing import Any, Literal

from agentlog.ingest.base import (
    TranscriptAdapter,
    file_stat,
    hash_bytes,
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
    revision: tuple[int, int] | str
    source_hash: str
    results: list[ParseResult]
    text_bytes: int
    verification_unit: str | None = None
    artifact_observation: _SqliteRevisionObservation | None = None


@dataclass(frozen=True)
class _SourceVerificationEvidence:
    revision: tuple[int, int] | str
    source_hash: str
    verification_unit: str | None = None
    artifact_observation: _SqliteRevisionObservation | None = None


@dataclass
class _SourceReadFlight:
    done: Event
    parsed: _CachedSourceParse | None = None


@dataclass(frozen=True)
class _SqliteRevisionObservation:
    identity: tuple[int, int]
    data_version: int


class _SqliteRevisionMonitor:
    """Observe commits from a stable read-only connection to one SQLite file."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._conn: sqlite3.Connection | None = None
        self._identity: tuple[int, int] | None = None
        self._lock = RLock()

    def close(self) -> None:
        with self._lock:
            conn, self._conn = self._conn, None
            self._identity = None
        if conn is not None:
            conn.close()

    def observe(self) -> _SqliteRevisionObservation:
        """Return a data-version observation for the current file identity."""
        with self._lock:
            identity = self._file_identity()
            replaced = identity != self._identity
            if self._conn is None or replaced:
                self._replace_connection(identity)
            assert self._conn is not None
            data_version = self._read_data_version()
            observation = _SqliteRevisionObservation(identity, data_version)
            return observation

    def confirm(self, observation: _SqliteRevisionObservation) -> bool:
        """Record an observation only when no later commit or replacement occurred."""
        with self._lock:
            if self._conn is None or self._file_identity() != observation.identity:
                return False
            if self._read_data_version() != observation.data_version:
                return False
            self._identity = observation.identity
            return True

    def _replace_connection(self, identity: tuple[int, int]) -> None:
        old, self._conn = self._conn, None
        if old is not None:
            old.close()
        uri = f"{self._path.resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        conn.execute("PRAGMA busy_timeout = 30000")
        self._conn = conn
        self._identity = identity

    def _read_data_version(self) -> int:
        assert self._conn is not None
        return int(self._conn.execute("PRAGMA data_version").fetchone()[0])

    def _file_identity(self) -> tuple[int, int]:
        stat = self._path.stat()
        return stat.st_dev, stat.st_ino


class CachedSourceTranscriptReader:
    """Bounded read-through cache for normalized source transcripts.

    This is process-local only: SQLite remains metadata-only and source files
    stay authoritative. T3 entries are scoped to one thread, never its shared
    state database as a whole.
    """

    def __init__(
        self,
        *,
        max_entries: int = 32,
        max_text_bytes: int = 16 * 1024 * 1024,
    ) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        if max_text_bytes < 1:
            raise ValueError("max_text_bytes must be at least 1")
        self.max_entries = max_entries
        self.max_text_bytes = max_text_bytes
        self._artifacts: OrderedDict[tuple[str, ...], _CachedSourceParse] = OrderedDict()
        self._invalid_artifacts: OrderedDict[tuple[str, ...], None] = OrderedDict()
        self._verification_evidence: OrderedDict[
            tuple[str, ...], _SourceVerificationEvidence
        ] = OrderedDict()
        self._verification_capacity = max(1024, max_entries)
        self._verification_overflow = False
        self._text_bytes = 0
        self._flights: dict[tuple[str, ...], _SourceReadFlight] = {}
        self._revision_monitors: OrderedDict[str, _SqliteRevisionMonitor] = OrderedDict()
        self._lock = RLock()

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._artifacts)

    @property
    def text_bytes(self) -> int:
        with self._lock:
            return self._text_bytes

    def prewarm_recent(
        self,
        conn: sqlite3.Connection,
        *,
        now: datetime | None = None,
        limit: int = 16,
    ) -> list[str]:
        """Warm only source-backed sessions active in the preceding week."""
        now = now or datetime.now(timezone.utc)
        cutoff = (now - timedelta(days=7)).isoformat()
        rows = conn.execute(
            """
            SELECT id
            FROM sessions
            WHERE transcript_storage = 'source_backed'
              AND COALESCE(ended_at, started_at) >= ?
            ORDER BY COALESCE(ended_at, started_at) DESC, id
            LIMIT ?
            """,
            (cutoff, max(1, limit)),
        ).fetchall()
        warmed: list[str] = []
        for row in rows:
            session_id = str(row["id"])
            if self(conn, session_id).ready:
                warmed.append(session_id)
        return warmed

    def _get_artifact(self, key: tuple[str, ...]) -> _CachedSourceParse | None:
        with self._lock:
            if key in self._invalid_artifacts:
                return None
            cached = self._artifacts.get(key)
            if cached is not None:
                self._artifacts.move_to_end(key)
            return cached

    def _put_artifact(self, key: tuple[str, ...], value: _CachedSourceParse) -> None:
        with self._lock:
            if value.text_bytes > self.max_text_bytes:
                return
            old = self._artifacts.pop(key, None)
            if old is not None:
                self._text_bytes -= old.text_bytes
            self._artifacts[key] = value
            self._invalid_artifacts.pop(key, None)
            self._artifacts.move_to_end(key)
            self._text_bytes += value.text_bytes
            while (
                len(self._artifacts) > self.max_entries
                or self._text_bytes > self.max_text_bytes
            ):
                _, evicted = self._artifacts.popitem(last=False)
                self._text_bytes -= evicted.text_bytes

    def _drop_artifact(self, key: tuple[str, ...]) -> None:
        with self._lock:
            self._invalid_artifacts[key] = None
            self._invalid_artifacts.move_to_end(key)
            while len(self._invalid_artifacts) > self.max_entries:
                self._invalid_artifacts.popitem(last=False)
            cached = self._artifacts.pop(key, None)
            if cached is not None:
                self._text_bytes -= cached.text_bytes

    def _record_verification(
        self, key: tuple[str, ...], value: _CachedSourceParse
    ) -> None:
        evidence = _SourceVerificationEvidence(
            revision=value.revision,
            source_hash=value.source_hash,
            verification_unit=value.verification_unit,
            artifact_observation=value.artifact_observation,
        )
        with self._lock:
            if key not in self._verification_evidence and (
                len(self._verification_evidence) >= self._verification_capacity
            ):
                self._verification_evidence.popitem(last=False)
                self._verification_overflow = True
            self._verification_evidence[key] = evidence
            self._verification_evidence.move_to_end(key)

    def _refresh_verification(
        self, key: tuple[str, ...], value: _SourceVerificationEvidence
    ) -> None:
        with self._lock:
            if key in self._verification_evidence:
                self._verification_evidence[key] = value
                self._verification_evidence.move_to_end(key)

    def _claim_flight(self, key: tuple[str, ...]) -> tuple[bool, _SourceReadFlight]:
        with self._lock:
            existing = self._flights.get(key)
            if existing is not None:
                return False, existing
            flight = _SourceReadFlight(done=Event())
            self._flights[key] = flight
            return True, flight

    def _finish_flight(self, key: tuple[str, ...], flight: _SourceReadFlight) -> None:
        with self._lock:
            self._flights.pop(key, None)
            flight.done.set()

    def _sqlite_monitor(self, path: Path) -> _SqliteRevisionMonitor:
        key = str(path.resolve(strict=False))
        with self._lock:
            monitor = self._revision_monitors.get(key)
            if monitor is None:
                monitor = _SqliteRevisionMonitor(path)
                self._revision_monitors[key] = monitor
            self._revision_monitors.move_to_end(key)
            while len(self._revision_monitors) > self.max_entries:
                _, evicted = self._revision_monitors.popitem(last=False)
                evicted.close()
            return monitor

    def close(self) -> None:
        with self._lock:
            monitors = tuple(self._revision_monitors.values())
            self._revision_monitors.clear()
            self._artifacts.clear()
            self._invalid_artifacts.clear()
            self._verification_evidence.clear()
            self._verification_overflow = False
            self._text_bytes = 0
        for monitor in monitors:
            monitor.close()

    def verify_current(self) -> bool:
        """Confirm every source used by this operation is still unchanged."""
        try:
            with self._lock:
                evidence = tuple(self._verification_evidence.items())
                invalid = bool(self._invalid_artifacts)
                overflowed = self._verification_overflow
            if invalid or overflowed:
                return False
            for key, cached in evidence:
                artifact_path = key[1]
                path = Path(artifact_path)
                if not path.is_file():
                    return False
                if cached.verification_unit is not None:
                    adapter_type = _ADAPTERS.get(key[0])
                    if adapter_type is None:
                        return False
                    external_id = cached.verification_unit.split(":", 1)[1]
                    adapter = adapter_type()
                    monitor = self._sqlite_monitor(path)
                    observation = monitor.observe()
                    if cached.artifact_observation == observation:
                        continue
                    revision = _t3_revision(adapter, path, external_id)
                    if (
                        revision is not None
                        and revision == cached.revision
                        and monitor.confirm(observation)
                    ):
                        self._refresh_verification(
                            key,
                            replace(cached, artifact_observation=observation),
                        )
                        continue
                    parsed = _parse_t3_session(adapter, path, external_id)
                    if (
                        parsed is None
                        or parsed[1] != cached.source_hash
                        or not monitor.confirm(observation)
                    ):
                        return False
                    self._refresh_verification(
                        key,
                        replace(
                            cached,
                            revision=revision or parsed[1],
                            artifact_observation=observation,
                        ),
                    )
                    continue
                if file_stat(path) != cached.revision:
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
            conn,
            session_id,
            reader=self,
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
        sqlite_source = is_sqlite_path(path)
        data = b"" if sqlite_source else path.read_bytes()
        captured_hash = None if sqlite_source else hash_bytes(data)
        results = adapter.parse_path(path, data, start_offset=0)
        after_parse = file_stat(path)
        if before != after_parse:
            continue
        source_hash = _current_hash(path)
        after_hash = file_stat(path)
        if before == after_hash and (
            sqlite_source or captured_hash == source_hash
        ):
            return results, source_hash
    return None


def _parse_t3_session(
    adapter: T3CodeAdapter, path: Path, external_id: str
) -> tuple[list[ParseResult], str] | None:
    """Read one coherent T3 thread while unrelated threads continue writing."""
    parse_with_hash = getattr(adapter, "parse_session_with_hash", None)
    if callable(parse_with_hash):
        result, source_hash = parse_with_hash(path, external_id)
    else:
        result = adapter.parse_session(path, external_id)
        source_hash = _parse_result_hash(result) if result is not None else "missing"
    if result is None:
        return [], "missing"
    return [result], source_hash


def _parse_result_hash(result: ParseResult | None) -> str:
    if result is None:
        return "missing"
    payload = result.model_dump(
        mode="json", exclude={"bytes_consumed"}
    )
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


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
    return _read_source_transcript(
        conn, session_id, reader=None
    )


def _parsed_text_bytes(results: list[ParseResult]) -> int:
    return sum(
        len(message.text.encode("utf-8"))
        for result in results
        for message in result.messages
    )


def _t3_revision(
    adapter: TranscriptAdapter, path: Path, external_id: str
) -> str | None:
    probe = getattr(adapter, "session_revision", None)
    if not callable(probe):
        return None
    return probe(path, external_id)


def _read_targeted_t3(
    adapter: TranscriptAdapter,
    path: Path,
    external_id: str,
    cached: _CachedSourceParse | None,
    reader: CachedSourceTranscriptReader | None,
) -> tuple[
    tuple[list[ParseResult], str],
    tuple[int, int] | str,
    _SqliteRevisionObservation | None,
] | None:
    """Reuse a target cache until the shared SQLite artifact commits again."""
    monitor = reader._sqlite_monitor(path) if reader is not None else None
    for _attempt in range(3):
        observation: _SqliteRevisionObservation | None = None
        if monitor is not None:
            observation = monitor.observe()
            if (
                cached is not None
                and cached.artifact_observation == observation
            ):
                return (cached.results, cached.source_hash), cached.revision, observation

        before = _t3_revision(adapter, path, external_id)
        if cached is not None and before is not None and before == cached.revision:
            if monitor is None or monitor.confirm(observation):
                return (cached.results, cached.source_hash), before, observation
            continue

        parsed = _parse_t3_session(adapter, path, external_id)
        after = _t3_revision(adapter, path, external_id)
        if before is not None and after != before:
            return None
        revision: tuple[int, int] | str = after or (
            parsed[1] if parsed is not None else "missing"
        )
        if monitor is None or monitor.confirm(observation):
            return parsed, revision, observation
    return None


def _artifact_cache_key(
    row: sqlite3.Row,
    locator: SourceLocator,
    *,
    targeted: bool,
) -> tuple[str, ...]:
    key = (
        locator.harness,
        locator.artifact_path,
        str(row["artifact_id"] or ""),
        str(row["parser_version"] or ""),
        str(row["parsed_offset"] or 0),
        str(row["artifact_content_hash"] or ""),
    )
    return key + ((locator.unit_id,) if targeted else ())


def _read_source_transcript(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    reader: CachedSourceTranscriptReader | None,
    flight_owned: bool = False,
    flight: _SourceReadFlight | None = None,
    shared_parse: _CachedSourceParse | None = None,
) -> SourceReadResult:
    """Return current normalized messages for a source-backed session.

    The artifact checkpoint verifies that a JSONL source's ingested prefix was
    not rewritten, while allowing later complete lines to be visible at once.
    """
    row = conn.execute(
        """
        SELECT s.*, a.path AS artifact_path, a.harness AS artifact_harness,
               a.content_hash AS artifact_content_hash, a.parsed_offset,
               a.parser_version
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
    targeted = is_sqlite_path(path) and hasattr(adapter_type, "parse_session")
    cache_key = _artifact_cache_key(row, locator, targeted=targeted)
    if reader is not None and not flight_owned:
        owns_flight, claimed_flight = reader._claim_flight(cache_key)
        if not owns_flight:
            claimed_flight.done.wait()
            return _read_source_transcript(
                conn,
                session_id,
                reader=reader,
                flight_owned=True,
                shared_parse=claimed_flight.parsed,
            )
        try:
            return _read_source_transcript(
                conn,
                session_id,
                reader=reader,
                flight_owned=True,
                flight=claimed_flight,
            )
        finally:
            reader._finish_flight(cache_key, claimed_flight)
    stored_cached = reader._get_artifact(cache_key) if reader is not None else None
    cached = stored_cached or shared_parse
    try:
        if targeted:
            adapter = adapter_type()
            external_id = str(row["external_id"])
            targeted_read = _read_targeted_t3(
                adapter, path, external_id, cached, reader
            )
            if targeted_read is None:
                return SourceReadResult(
                    "source_changed",
                    [],
                    locator,
                    locator.unit_id,
                    warning="canonical source changed while being read",
                )
            parsed, revision, artifact_observation = targeted_read
        else:
            artifact_observation = None
            revision = file_stat(path)
            if cached is not None and revision == cached.revision:
                if _current_hash(path) == cached.source_hash:
                    parsed = (cached.results, cached.source_hash)
                else:
                    cached = None
                    parsed = _parse_current(adapter_type(), path)
                    revision = file_stat(path)
            else:
                cached = None
                parsed = _parse_current(adapter_type(), path)
                revision = file_stat(path)
    except (OSError, sqlite3.Error, ValueError) as exc:
        return SourceReadResult("source_unavailable", [], locator, locator.unit_id, warning=f"could not read canonical source: {exc}")
    if parsed is None:
        return SourceReadResult("source_changed", [], locator, locator.unit_id, warning="canonical source changed while being read")
    results, source_hash = parsed
    parsed_cache = _CachedSourceParse(
        revision=revision,
        source_hash=source_hash,
        results=results,
        text_bytes=_parsed_text_bytes(results),
        verification_unit=(locator.unit_id if targeted else None),
        artifact_observation=artifact_observation,
    )
    if not _checkpoint_is_current(row, path):
        if reader is not None:
            reader._drop_artifact(cache_key)
        return SourceReadResult(
            "source_changed",
            [],
            locator,
            locator.unit_id,
            warning="canonical source changed during transcript read",
        )
    if flight is not None:
        flight.parsed = parsed_cache
    cache_candidate = (
        parsed_cache
        if reader is not None
        and (
            stored_cached is None
            or stored_cached.source_hash != source_hash
            or stored_cached.revision != revision
            or stored_cached.artifact_observation != artifact_observation
        )
        else None
    )
    external_id = str(row["external_id"])
    matches = [item for item in results if item.session.external_id == external_id]
    if len(matches) != 1:
        if reader is not None:
            reader._drop_artifact(cache_key)
        return SourceReadResult(
            "source_changed",
            [],
            locator,
            locator.unit_id,
            warning="canonical source no longer contains this session identity",
        )
    if matches[0].extras.get("checkpoint_blocked") is True:
        if reader is not None:
            reader._drop_artifact(cache_key)
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
        if reader is not None:
            reader._drop_artifact(cache_key)
        return SourceReadResult(
            "source_changed",
            [],
            locator,
            locator.unit_id,
            warning="canonical source diverges from persisted message metadata",
        )
    if reader is not None and cache_candidate is not None:
        reader._put_artifact(cache_key, cache_candidate)
    if reader is not None:
        reader._record_verification(cache_key, parsed_cache)
    fresh = SourceReadResult(
        "ready",
        [_message_dict(session_id, message) for message in matches[0].messages],
        locator=locator,
        source_unit_id=locator.unit_id,
        source_identity=_source_identity(locator),
        source_hash=source_hash,
    )
    return fresh
