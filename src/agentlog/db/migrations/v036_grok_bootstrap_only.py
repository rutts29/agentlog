"""Suppress Grok CLI setup artifacts that were previously ingested as chats."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from agentlog.ingest.base import file_stat
from agentlog.ingest.grok import is_bootstrap_only_artifact
from agentlog.session_identity import GROK_BOOTSTRAP_ONLY_THREAD_SOURCE


def _parent_meta_index(workspace: Path) -> tuple[tuple[str, int, int], ...]:
    entries = []
    for path in workspace.glob("*/subagents/*/meta.json"):
        if path.is_file() and not path.is_symlink():
            size, mtime_ns = file_stat(path)
            entries.append((str(path), size, mtime_ns))
    return tuple(sorted(entries))


def _has_compaction_dependency(path: Path) -> bool:
    root = path.parent
    return any(
        candidate.is_file() and not candidate.is_symlink()
        for folder in ("compaction_requests", "compaction_checkpoints")
        for candidate in (root / folder).glob("*.json")
    )


def apply(conn: sqlite3.Connection) -> None:
    """Clear only the exact inactive two-record bootstrap shape."""
    conn.execute("SAVEPOINT grok_bootstrap_only")
    try:
        for statement in (
            "CREATE INDEX IF NOT EXISTS idx_tool_events_message ON tool_events(message_id)",
            "CREATE INDEX IF NOT EXISTS idx_skill_exposures_message ON skill_exposures(message_id)",
            "CREATE INDEX IF NOT EXISTS idx_token_usage_message ON token_usage(message_id)",
            "CREATE INDEX IF NOT EXISTS idx_exchange_windows_request ON exchange_windows(request_message_id)",
            "CREATE INDEX IF NOT EXISTS idx_exchange_windows_response ON exchange_windows(response_message_id)",
            "CREATE INDEX IF NOT EXISTS idx_task_clusters_segment_start ON task_clusters(segment_start_message_id)",
            "CREATE INDEX IF NOT EXISTS idx_task_clusters_segment_end ON task_clusters(segment_end_message_id)",
        ):
            conn.execute(statement)
        conn.execute(
            """
            CREATE TEMP TABLE grok_bootstrap_only_sessions (
                id TEXT PRIMARY KEY
            )
            """
        )
        candidates = conn.execute(
            """
            SELECT s.id, a.path, a.size, a.mtime_ns, a.content_hash
            FROM sessions s
            JOIN artifacts a ON a.id = s.artifact_id
            JOIN messages m ON m.session_id = s.id
            WHERE s.harness = 'grok'
              AND s.agent_profile = 'grok-build-plan'
              AND s.transcript_storage = 'source_backed'
              AND s.thread_source IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM tool_events t WHERE t.session_id = s.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM token_usage u WHERE u.session_id = s.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM skill_exposures k WHERE k.session_id = s.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM exchange_windows w WHERE w.session_id = s.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM claim_evidence e WHERE e.session_id = s.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM owner_insight_batch_messages b WHERE b.session_id = s.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM owner_insight_seen_messages seen WHERE seen.session_id = s.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM owner_insight_session_state state WHERE state.session_id = s.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM task_clusters cluster WHERE cluster.root_session_id = s.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM session_links link
                  WHERE link.source_session_id = s.id
                     OR link.target_session_id = s.id
                     OR (
                         link.target_harness = s.harness
                         AND link.target_external_id = s.external_id
                     )
              )
              AND NOT EXISTS (
                  SELECT 1 FROM sessions child
                  WHERE child.parent_session_id IN (
                      s.id, s.external_id, s.harness || ':' || s.external_id
                  )
              )
            GROUP BY s.id
            HAVING COUNT(*) = 2
               AND SUM(m.role = 'system') = 1
               AND SUM(m.role = 'user') = 1
               AND SUM(COALESCE(m.is_tool_plumbing, 0) = 1) = 2
               AND SUM(COALESCE(m.authored_by_agent, 0) = 1) = 2
            """
        ).fetchall()
        workspaces = {Path(str(row["path"])).parent.parent for row in candidates}
        parent_indexes = {
            workspace: _parent_meta_index(workspace) for workspace in workspaces
        }
        parent_meta_children = {
            workspace: {
                Path(entry[0]).parent.name for entry in entries
            }
            for workspace, entries in parent_indexes.items()
        }
        verified_ids: list[tuple[str]] = []
        for candidate in candidates:
            path = Path(str(candidate["path"]))
            workspace = path.parent.parent
            if (
                path.parent.name in parent_meta_children[workspace]
                or _has_compaction_dependency(path)
            ):
                continue
            if (
                not is_bootstrap_only_artifact(
                    path,
                    expected_revision=(
                        int(candidate["size"]), int(candidate["mtime_ns"])
                    ),
                    expected_content_hash=str(candidate["content_hash"]),
                    verify_current_dependencies=False,
                )
            ):
                continue
            verified_ids.append((str(candidate["id"]),))
        conn.executemany(
            "INSERT INTO grok_bootstrap_only_sessions (id) VALUES (?)",
            verified_ids,
        )
        for table in ("tool_events", "skill_exposures", "token_usage", "messages"):
            conn.execute(
                f"DELETE FROM {table} WHERE session_id IN "
                "(SELECT id FROM grok_bootstrap_only_sessions)"
            )
        conn.execute(
            """
            UPDATE sessions
            SET thread_source = ?
            WHERE id IN (SELECT id FROM grok_bootstrap_only_sessions)
            """,
            (GROK_BOOTSTRAP_ONLY_THREAD_SOURCE,),
        )
        if any(
            _parent_meta_index(workspace) != entries
            for workspace, entries in parent_indexes.items()
        ):
            raise RuntimeError("Grok parent metadata changed during bootstrap cleanup")
        conn.execute("DROP TABLE grok_bootstrap_only_sessions")
    except Exception:
        conn.execute("ROLLBACK TO grok_bootstrap_only")
        conn.execute("RELEASE grok_bootstrap_only")
        raise
    conn.execute("RELEASE grok_bootstrap_only")
