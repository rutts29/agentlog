"""Shared logical-session projection for user-facing aggregate APIs."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Iterable

from agentlog.session_identity import (
    IdentityContext,
    SUPPRESSED_ACTIVITY_THREAD_SOURCES,
    build_identity_context,
    is_suppressed_activity_session,
    logical_projection,
    provider_root_shadow_ids,
)


@dataclass(frozen=True)
class VisibleLogicalSession:
    row: sqlite3.Row
    session_id: str
    metric_session_id: str
    logical_harness: str
    runtime_harness: str
    orchestrator_session_id: str | None


def visible_logical_sessions(
    conn: sqlite3.Connection,
    rows: Iterable[sqlite3.Row],
    *,
    context: IdentityContext | None = None,
) -> list[VisibleLogicalSession]:
    identity = context or build_identity_context(conn)
    root_shadows = provider_root_shadow_ids(conn, context=identity)
    suppressed_sources = sorted(SUPPRESSED_ACTIVITY_THREAD_SOURCES)
    placeholders = ", ".join("?" for _ in suppressed_sources)
    suppressed_ids = {
        str(row["id"])
        for row in conn.execute(
            f"SELECT id FROM sessions WHERE thread_source IN ({placeholders})",
            suppressed_sources,
        )
    }
    seen_metrics: set[str] = set()
    visible: list[VisibleLogicalSession] = []
    for row in rows:
        session_id = str(row["id"])
        if (
            session_id in root_shadows
            or session_id in suppressed_ids
            or is_suppressed_activity_session(row)
        ):
            continue
        projection = logical_projection(
            conn, session_id, str(row["harness"]), context=identity
        )
        metric_session_id = str(projection["transcript_session_id"] or session_id)
        if metric_session_id in seen_metrics:
            continue
        seen_metrics.add(metric_session_id)
        visible.append(
            VisibleLogicalSession(
                row=row,
                session_id=session_id,
                metric_session_id=metric_session_id,
                logical_harness=str(projection["logical_harness"]),
                runtime_harness=str(projection["runtime_harness"]),
                orchestrator_session_id=projection["orchestrator_session_id"],
            )
        )
    return visible
