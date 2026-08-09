from __future__ import annotations

import logging
from dataclasses import dataclass, field

from rich.console import Console
from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn

from agentlog.analysis.windows import build_exchange_windows
from agentlog.config import PARSER_VERSION
from agentlog.db.repository import Repository
from agentlog.ingest.base import TranscriptAdapter, hash_prefix
from agentlog.ingest.checkpoint import IngestAction, decide, read_slice
from agentlog.ingest.claude import ClaudeAdapter
from agentlog.ingest.codex import CodexAdapter
from agentlog.ingest.cursor import CursorAdapter

log = logging.getLogger("agentlog.ingest")


@dataclass
class IngestStats:
    skipped: int = 0
    parsed: int = 0
    appended: int = 0
    failed: int = 0
    warnings: list[str] = field(default_factory=list)
    sessions_upserted: int = 0


def adapters() -> list[TranscriptAdapter]:
    return [CodexAdapter(), ClaudeAdapter(), CursorAdapter()]


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


def _ingest_one(
    repo: Repository,
    adapter: TranscriptAdapter,
    path,
    stats: IngestStats,
) -> None:
    decision = decide(repo, path, adapter.harness.value)
    if decision.action == IngestAction.SKIP:
        stats.skipped += 1
        return

    data = read_slice(path, decision.start_offset)
    # If file ends mid-line on a prior parse, start_offset should be at a newline boundary.
    result = adapter.parse_chunk(path, data, start_offset=decision.start_offset)
    stats.warnings.extend(result.warnings)

    append = decision.action == IngestAction.APPEND
    artifact_id = (
        decision.artifact.id
        if decision.artifact is not None
        else None
    )

    if not append and artifact_id is not None:
        repo.delete_sessions_for_artifact(artifact_id)

    parsed_offset = result.bytes_consumed
    # Align to absolute file offset
    if append:
        parsed_offset = decision.start_offset + len(data)
        # Prefer parser-reported absolute offset when provided
        if result.bytes_consumed >= decision.start_offset:
            parsed_offset = result.bytes_consumed
    else:
        parsed_offset = result.bytes_consumed if result.bytes_consumed else len(data)

    # content_hash covers the parsed prefix for future append checks
    content_hash = hash_prefix(path, parsed_offset)

    artifact_id = repo.upsert_artifact(
        harness=adapter.harness.value,
        path=str(path),
        size=decision.size,
        mtime_ns=decision.mtime_ns,
        content_hash=content_hash,
        parsed_offset=parsed_offset,
        parser_version=PARSER_VERSION,
    )

    session_key = f"{adapter.harness.value}:{result.session.external_id}"
    base_seq = repo.max_message_seq(session_key) if append else 0
    base_tool = repo.max_tool_seq(session_key) if append else 0

    session_id = repo.save_parse_result(
        artifact_id=artifact_id,
        result=result,
        append=append,
        base_seq=base_seq,
        base_tool_seq=base_tool,
    )

    messages = repo.list_messages(session_id)
    windows = build_exchange_windows(messages)
    repo.replace_exchange_windows(session_id, windows)

    stats.sessions_upserted += 1
    if append:
        stats.appended += 1
    else:
        stats.parsed += 1
