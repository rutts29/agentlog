from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from agentlog.config import PARSER_VERSION
from agentlog.db.repository import ArtifactRow, Repository
from agentlog.ingest.base import file_stat, hash_prefix


class IngestAction(str, Enum):
    SKIP = "skip"
    APPEND = "append"
    REPARSE = "reparse"


@dataclass
class CheckpointDecision:
    action: IngestAction
    artifact: ArtifactRow | None
    size: int
    mtime_ns: int
    start_offset: int


def decide(
    repo: Repository,
    path: Path,
    harness: str,
    *,
    revision_fn=file_stat,
    fingerprint_fn=hash_prefix,
    revision: tuple[int, int] | None = None,
    force_full_reparse: bool = False,
) -> CheckpointDecision:
    size, mtime_ns = revision if revision is not None else revision_fn(path)
    existing = repo.get_artifact_by_path(str(path))

    if existing is None:
        return CheckpointDecision(
            action=IngestAction.REPARSE,
            artifact=None,
            size=size,
            mtime_ns=mtime_ns,
            start_offset=0,
        )

    if existing.parser_version != PARSER_VERSION:
        return CheckpointDecision(
            action=IngestAction.REPARSE,
            artifact=existing,
            size=size,
            mtime_ns=mtime_ns,
            start_offset=0,
        )

    if existing.size == size and existing.mtime_ns == mtime_ns:
        return CheckpointDecision(
            action=IngestAction.SKIP,
            artifact=existing,
            size=size,
            mtime_ns=mtime_ns,
            start_offset=existing.parsed_offset,
        )

    if (
        not force_full_reparse
        and size > existing.parsed_offset
        and existing.parsed_offset > 0
    ):
        prefix_hash = fingerprint_fn(path, existing.parsed_offset)
        if prefix_hash == existing.content_hash:
            return CheckpointDecision(
                action=IngestAction.APPEND,
                artifact=existing,
                size=size,
                mtime_ns=mtime_ns,
                start_offset=existing.parsed_offset,
            )

    return CheckpointDecision(
        action=IngestAction.REPARSE,
        artifact=existing,
        size=size,
        mtime_ns=mtime_ns,
        start_offset=0,
    )


def read_slice(path: Path, start: int) -> bytes:
    with path.open("rb") as f:
        if start:
            f.seek(start)
        return f.read()
