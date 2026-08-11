from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field

from rich.console import Console
from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn

from agentlog.analysis.windows import build_exchange_windows
from agentlog.config import PARSER_VERSION
from agentlog.db.repository import (
    LEGACY_MATERIALIZED,
    SOURCE_BACKED,
    Repository,
)
from agentlog.ingest.base import (
    TranscriptAdapter,
    file_stat,
    hash_prefix,
    is_sqlite_path,
    sqlite_fingerprint,
)
from agentlog.ingest.checkpoint import (
    CheckpointDecision,
    IngestAction,
    decide,
    read_slice,
)
from agentlog.ingest.claude import ClaudeAdapter
from agentlog.ingest.codex import CodexAdapter
from agentlog.ingest.cursor import CursorAdapter
from agentlog.ingest.hermes import HermesAdapter
from agentlog.ingest.t3code import T3CodeAdapter
from agentlog.ingest.warp import WarpAdapter
from agentlog.normalize.models import ParseResult

log = logging.getLogger("agentlog.ingest")

_STABLE_SOURCE_ATTEMPTS = 3


@dataclass
class IngestStats:
    skipped: int = 0
    parsed: int = 0
    appended: int = 0
    failed: int = 0
    warnings: list[str] = field(default_factory=list)
    sessions_upserted: int = 0
    sessions_added: int = 0
    sessions_updated: int = 0
    messages_added: int = 0


def adapters() -> list[TranscriptAdapter]:
    return [
        CodexAdapter(),
        ClaudeAdapter(),
        CursorAdapter(),
        WarpAdapter(),
        HermesAdapter(),
        T3CodeAdapter(),
    ]


def adapter_for(harness: str) -> TranscriptAdapter | None:
    key = harness.lower()
    for adapter in adapters():
        if adapter.harness.value == key:
            return adapter
    return None


def _sqlite_logical_snapshot(
    path,
) -> tuple[tuple[int, int], str] | None:
    revision_before = file_stat(path)
    fingerprint = sqlite_fingerprint(path)
    revision_after = file_stat(path)
    if revision_before != revision_after:
        return None
    return revision_after, fingerprint


def ingest_all(repo: Repository, console: Console | None = None) -> IngestStats:
    console = console or Console()
    stats = IngestStats()
    jobs: list[tuple[TranscriptAdapter, object]] = []
    for adapter in adapters():
        paths = adapter.discover()
        for path in paths:
            jobs.append((adapter, path))

    if not jobs:
        console.print("No transcript files found.")
        return stats

    with Progress(
        TextColumn("[bold]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task_id = progress.add_task("ingest", total=len(jobs))
        for adapter, path in jobs:
            progress.update(task_id, description=f"{adapter.harness.value}")
            try:
                _ingest_one(repo, adapter, path, stats)
                repo.conn.commit()
            except Exception as exc:  # noqa: BLE001 - keep ingest going
                repo.conn.rollback()
                stats.failed += 1
                msg = f"{path}: {exc}"
                stats.warnings.append(msg)
                log.exception("ingest failed for %s", path)
            progress.advance(task_id)

    return stats


def ingest_harness(repo: Repository, harness: str) -> IngestStats:
    """Run incremental ingest for a single harness."""
    stats = IngestStats()
    adapter = adapter_for(harness)
    if adapter is None:
        stats.warnings.append(f"unknown harness: {harness}")
        return stats
    for path in adapter.discover():
        try:
            _ingest_one(repo, adapter, path, stats)
            repo.conn.commit()
        except Exception as exc:  # noqa: BLE001 - keep ingest going
            repo.conn.rollback()
            stats.failed += 1
            msg = f"{path}: {exc}"
            stats.warnings.append(msg)
            log.exception("ingest failed for %s", path)
    return stats


def _session_message_count(repo: Repository, session_id: str) -> int:
    row = repo.conn.execute(
        "SELECT COUNT(*) AS c FROM messages WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    return int(row["c"]) if row else 0


def _source_backed_windows(
    repo: Repository, session_id: str, result: ParseResult
) -> list[tuple[str, str, str, str, str]]:
    stored = repo.list_messages(session_id)
    if len(stored) != len(result.messages):
        raise RuntimeError(
            f"source-backed session {session_id} metadata does not match source"
        )
    hydrated: list[dict[str, object]] = []
    for row, message in zip(stored, result.messages, strict=True):
        if row["role"] != message.role or row["content_hash"] != message.content_hash:
            raise RuntimeError(
                f"source-backed session {session_id} changed during window build"
            )
        item = dict(row)
        item["text"] = message.text
        hydrated.append(item)
    return build_exchange_windows(hydrated)  # type: ignore[arg-type]


def _full_source_results(
    adapter: TranscriptAdapter, path
) -> dict[str, ParseResult]:
    data = read_slice(path, 0) if adapter.supports_byte_append else b""
    return {
        result.session.external_id: result
        for result in adapter.parse_path(path, data, start_offset=0)
    }


def _ingest_one(
    repo: Repository,
    adapter: TranscriptAdapter,
    path,
    stats: IngestStats,
) -> None:
    sqlite_source = is_sqlite_path(path)
    for attempt in range(_STABLE_SOURCE_ATTEMPTS):
        try:
            decision = decide(repo, path, adapter.harness.value)
            revision_before = file_stat(path)
            if revision_before != (decision.size, decision.mtime_ns):
                continue
            if decision.action == IngestAction.SKIP:
                stats.skipped += 1
                return

            logical_snapshot = (
                _sqlite_logical_snapshot(path) if sqlite_source else None
            )
            if sqlite_source and logical_snapshot is None:
                continue
            logical_before_revision, logical_before = (
                logical_snapshot if logical_snapshot is not None else (None, None)
            )
            if sqlite_source and logical_before_revision != revision_before:
                continue
            if (
                sqlite_source
                and decision.artifact is not None
                and decision.artifact.parser_version == PARSER_VERSION
                and logical_before == decision.artifact.content_hash
            ):
                size, mtime_ns = logical_before_revision
                repo.upsert_artifact(
                    harness=adapter.harness.value,
                    path=str(path),
                    size=size,
                    mtime_ns=mtime_ns,
                    content_hash=decision.artifact.content_hash,
                    parsed_offset=decision.artifact.parsed_offset,
                    parser_version=PARSER_VERSION,
                    transcript_storage=decision.artifact.transcript_storage,
                )
                stats.skipped += 1
                return

            if (
                decision.action == IngestAction.APPEND
                and not adapter.supports_byte_append
            ):
                decision = CheckpointDecision(
                    action=IngestAction.REPARSE,
                    artifact=decision.artifact,
                    size=decision.size,
                    mtime_ns=decision.mtime_ns,
                    start_offset=0,
                )

            data = (
                read_slice(path, decision.start_offset)
                if adapter.supports_byte_append
                else b""
            )
            results = adapter.parse_path(
                path, data, start_offset=decision.start_offset
            )
            content_size = decision.size
            parsed_offset = content_size
            if results:
                reported = max(r.bytes_consumed for r in results)
                if decision.action == IngestAction.APPEND:
                    parsed_offset = decision.start_offset + len(data)
                    if reported >= decision.start_offset:
                        parsed_offset = reported
                elif adapter.supports_byte_append:
                    parsed_offset = reported
                else:
                    parsed_offset = reported if reported else content_size
            if sqlite_source:
                logical_after = _sqlite_logical_snapshot(path)
                if logical_after is None:
                    continue
                (revision_after, content_hash) = logical_after
                if logical_before != content_hash:
                    continue
                decision = CheckpointDecision(
                    action=decision.action,
                    artifact=decision.artifact,
                    size=revision_after[0],
                    mtime_ns=revision_after[1],
                    start_offset=decision.start_offset,
                )
            else:
                content_hash = hash_prefix(path, parsed_offset)
                revision_after = file_stat(path)
                if revision_before != revision_after:
                    continue
        except (OSError, sqlite3.Error):
            if attempt + 1 < _STABLE_SOURCE_ATTEMPTS:
                continue
            raise
        for result in results:
            stats.warnings.extend(result.warnings)
        break
    else:
        raise RuntimeError(f"source changed while ingesting: {path}")

    append = decision.action == IngestAction.APPEND
    artifact_storage = (
        decision.artifact.transcript_storage
        if decision.artifact is not None
        else SOURCE_BACKED
    )
    artifact_id = (
        decision.artifact.id
        if decision.artifact is not None
        else None
    )

    prior_sessions: dict[str, int] = {}
    for result in results:
        if result.session.external_id == "empty" and not result.messages:
            continue
        session_key = f"{adapter.harness.value}:{result.session.external_id}"
        existed = (
            repo.conn.execute(
                "SELECT 1 FROM sessions WHERE id = ?", (session_key,)
            ).fetchone()
            is not None
        )
        prior_sessions[session_key] = _session_message_count(repo, session_key)
        if not existed:
            prior_sessions[session_key] = -1  # sentinel: did not exist

    artifact_id = repo.upsert_artifact(
        harness=adapter.harness.value,
        path=str(path),
        size=decision.size,
        mtime_ns=decision.mtime_ns,
        content_hash=content_hash,
        parsed_offset=parsed_offset,
        parser_version=PARSER_VERSION,
        transcript_storage=artifact_storage,
    )

    if not results:
        if append:
            stats.appended += 1
        else:
            stats.parsed += 1
        return

    full_source_results: dict[str, ParseResult] | None = None
    for result in results:
        if result.session.external_id == "empty" and not result.messages:
            continue
        session_key = f"{adapter.harness.value}:{result.session.external_id}"
        base_seq = repo.max_message_seq(session_key) if append else 0
        base_tool = repo.max_tool_seq(session_key) if append else 0
        base_token = repo.max_token_seq(session_key) if append else 0
        prior = prior_sessions.get(session_key, -1)
        existed = prior >= 0
        msg_before = max(prior, 0)
        session_storage = (
            repo.session_transcript_storage(session_key) or SOURCE_BACKED
        )

        session_id = repo.save_parse_result(
            artifact_id=artifact_id,
            result=result,
            append=append,
            base_seq=base_seq,
            base_tool_seq=base_tool,
            base_token_seq=base_token,
            transcript_storage=session_storage,
            preserve_existing_legacy=True,
        )

        if (
            session_storage == SOURCE_BACKED
            and repo.session_artifact_id(session_id) == artifact_id
        ):
            if full_source_results is None:
                full_source_results = _full_source_results(adapter, path)
                if sqlite_source:
                    snapshot = _sqlite_logical_snapshot(path)
                    if snapshot is None or snapshot[1] != content_hash:
                        raise RuntimeError(
                            f"source changed during window build: {path}"
                        )
                elif file_stat(path) != (decision.size, decision.mtime_ns):
                    raise RuntimeError(
                        f"source changed during window build: {path}"
                    )
            full_result = full_source_results.get(result.session.external_id)
            if full_result is None:
                raise RuntimeError(
                    f"source-backed session missing from source: {session_id}"
                )
            windows = _source_backed_windows(repo, session_id, full_result)
            repo.replace_exchange_windows(session_id, windows)
        elif session_storage == LEGACY_MATERIALIZED and not existed:
            messages = repo.list_messages(session_id)
            windows = build_exchange_windows(messages)
            repo.replace_exchange_windows(session_id, windows)
        stats.sessions_upserted += 1
        if existed:
            stats.sessions_updated += 1
        else:
            stats.sessions_added += 1
        msg_after = _session_message_count(repo, session_id)
        stats.messages_added += max(0, msg_after - msg_before)

    if append:
        stats.appended += 1
    else:
        stats.parsed += 1
