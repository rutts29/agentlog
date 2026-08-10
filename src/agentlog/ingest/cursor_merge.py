"""Merge Cursor sessions that share a composer UUID under differing path ids."""

from __future__ import annotations

import logging
import sqlite3
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from agentlog.analysis.label_survival import DURABLE_LABEL_TABLES, refresh_label_links
from agentlog.analysis.windows import build_exchange_windows, compute_window_content_hash
from agentlog.ingest.cursor import canonical_external_id, prefer_repo

log = logging.getLogger("agentlog.ingest.cursor_merge")

T = TypeVar("T")


def _is_busy(exc: BaseException) -> bool:
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


def _with_busy_retry(fn: Callable[[], T], *, attempts: int = 8) -> T:
    last: BaseException | None = None
    for i in range(attempts):
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            last = exc
            if not _is_busy(exc) or i >= attempts - 1:
                raise
            time.sleep(0.05 * (2**i))
    assert last is not None
    raise last

_CHILD_SESSION_ID_TABLES = (
    "messages",
    "tool_events",
    "skill_exposures",
    "token_usage",
    "session_commits",
    "exchange_windows",
)


@dataclass
class MergeStats:
    groups_merged: int = 0
    sessions_deleted: int = 0
    sessions_renamed: int = 0
    parents_rewritten: int = 0
    labels_remapped: int = 0
    details: list[dict[str, Any]] = field(default_factory=list)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
            (table,),
        ).fetchone()
        is not None
    )


def _msg_count(conn: sqlite3.Connection, session_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM messages WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    return int(row["c"] if row and "c" in row.keys() else row[0])


def _tool_count(conn: sqlite3.Connection, session_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM tool_events WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    return int(row["c"] if row and "c" in row.keys() else row[0])


def _window_text_key(conn: sqlite3.Connection, window_id: str) -> tuple[str, str] | None:
    row = conn.execute(
        """
        SELECT req.text AS req_text, resp.text AS resp_text
        FROM exchange_windows w
        LEFT JOIN messages req ON req.id = w.request_message_id
        LEFT JOIN messages resp ON resp.id = w.response_message_id
        WHERE w.id = ?
        """,
        (window_id,),
    ).fetchone()
    if row is None:
        return None
    return (row["req_text"] or ""), (row["resp_text"] or "")


def _winner_window_map(
    conn: sqlite3.Connection, winner_id: str
) -> dict[tuple[str, str], tuple[str, str]]:
    """Map (req_text, resp_text) → (window_id, content_hash) for winner."""
    out: dict[tuple[str, str], tuple[str, str]] = {}
    rows = conn.execute(
        """
        SELECT w.id, w.content_hash, req.text AS req_text, resp.text AS resp_text
        FROM exchange_windows w
        LEFT JOIN messages req ON req.id = w.request_message_id
        LEFT JOIN messages resp ON resp.id = w.response_message_id
        WHERE w.session_id = ?
        """,
        (winner_id,),
    ).fetchall()
    for row in rows:
        key = (row["req_text"] or ""), (row["resp_text"] or "")
        ch = row["content_hash"] or row["id"]
        out[key] = (str(row["id"]), str(ch))
    return out


def remap_labels_by_window_text(
    conn: sqlite3.Connection,
    *,
    from_session_id: str,
    to_session_id: str,
) -> int:
    """Point durable labels at the richer session's windows matched by turn text."""
    if from_session_id == to_session_id:
        return 0
    target = _winner_window_map(conn, to_session_id)
    if not target:
        return 0
    remapped = 0
    loser_windows = conn.execute(
        "SELECT id FROM exchange_windows WHERE session_id = ?",
        (from_session_id,),
    ).fetchall()
    for wrow in loser_windows:
        old_wid = str(wrow["id"])
        key = _window_text_key(conn, old_wid)
        if key is None or key not in target:
            continue
        new_wid, new_hash = target[key]
        for table in DURABLE_LABEL_TABLES:
            if not _table_exists(conn, table):
                continue
            cols = {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})")}
            if "window_id" not in cols:
                continue
            if "content_hash" in cols:
                cur = conn.execute(
                    f"""
                    UPDATE {table}
                    SET window_id = ?, content_hash = ?,
                        link_status = 'linked', orphaned_at = NULL
                    WHERE window_id = ?
                    """,
                    (new_wid, new_hash, old_wid),
                )
            else:
                cur = conn.execute(
                    f"UPDATE {table} SET window_id = ? WHERE window_id = ?",
                    (new_wid, old_wid),
                )
            remapped += int(cur.rowcount)
    return remapped


def remap_labels_for_session_rename(
    conn: sqlite3.Connection, *, old_session_id: str, new_session_id: str
) -> int:
    """Rewrite label window ids/hashes after a session id change (same messages)."""
    if old_session_id == new_session_id:
        return 0
    remapped = 0
    rows = conn.execute(
        """
        SELECT w.id AS old_id, req.text AS req_text, resp.text AS resp_text
        FROM exchange_windows w
        LEFT JOIN messages req ON req.id = w.request_message_id
        LEFT JOIN messages resp ON resp.id = w.response_message_id
        WHERE w.session_id = ?
        """,
        (old_session_id,),
    ).fetchall()
    for row in rows:
        old_wid = str(row["old_id"])
        new_hash = compute_window_content_hash(
            new_session_id, row["req_text"], row["resp_text"]
        )
        for table in DURABLE_LABEL_TABLES:
            if not _table_exists(conn, table):
                continue
            cols = {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})")}
            if "window_id" not in cols:
                continue
            if "content_hash" in cols:
                cur = conn.execute(
                    f"""
                    UPDATE {table}
                    SET window_id = ?, content_hash = ?,
                        link_status = 'linked', orphaned_at = NULL
                    WHERE window_id = ?
                    """,
                    (new_hash, new_hash, old_wid),
                )
            else:
                cur = conn.execute(
                    f"UPDATE {table} SET window_id = ? WHERE window_id = ?",
                    (new_hash, old_wid),
                )
            remapped += int(cur.rowcount)
    return remapped


def _rebuild_windows(conn: sqlite3.Connection, session_id: str) -> None:
    from agentlog.db.repository import Repository

    repo = Repository(conn)
    messages = repo.list_messages(session_id)
    windows = build_exchange_windows(messages)
    # Avoid refresh_label_links mid-merge; caller does one pass at the end.
    has_ch = "content_hash" in {
        str(r[1]) for r in conn.execute("PRAGMA table_info(exchange_windows)")
    }
    desired: dict[str, tuple[str, str, str, str]] = {}
    for item in windows:
        if len(item) == 5:
            req_id, resp_id, input_hash, content_hash, wid = item
        else:
            req_id, resp_id, input_hash, content_hash = item  # type: ignore[misc]
            wid = content_hash
        desired[wid] = (req_id, resp_id, input_hash, content_hash)
    conn.execute("DELETE FROM exchange_windows WHERE session_id = ?", (session_id,))
    for wid, (req_id, resp_id, input_hash, content_hash) in desired.items():
        if has_ch:
            conn.execute(
                """
                INSERT INTO exchange_windows (
                    id, session_id, request_message_id, response_message_id,
                    input_hash, content_hash
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    session_id = excluded.session_id,
                    request_message_id = excluded.request_message_id,
                    response_message_id = excluded.response_message_id,
                    input_hash = excluded.input_hash,
                    content_hash = excluded.content_hash
                """,
                (wid, session_id, req_id, resp_id, input_hash, content_hash),
            )
        else:
            conn.execute(
                """
                INSERT INTO exchange_windows (
                    id, session_id, request_message_id, response_message_id,
                    input_hash
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    session_id = excluded.session_id,
                    request_message_id = excluded.request_message_id,
                    response_message_id = excluded.response_message_id,
                    input_hash = excluded.input_hash
                """,
                (wid, session_id, req_id, resp_id, input_hash),
            )


def rename_session(
    conn: sqlite3.Connection, *, old_id: str, new_external_id: str
) -> int:
    """Rename a session to harness:new_external_id, rewriting FK session_ids."""
    new_id = f"cursor:{new_external_id}"
    if old_id == new_id:
        return 0
    if conn.execute("SELECT 1 FROM sessions WHERE id = ?", (new_id,)).fetchone():
        raise RuntimeError(f"canonical session already exists: {new_id}")

    labels = remap_labels_for_session_rename(
        conn, old_session_id=old_id, new_session_id=new_id
    )

    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (old_id,)).fetchone()
    if row is None:
        return labels
    cols = [str(k) for k in row.keys()]
    values = {c: row[c] for c in cols}
    values["id"] = new_id
    values["external_id"] = new_external_id
    placeholders = ", ".join("?" for _ in cols)
    col_sql = ", ".join(cols)
    conn.execute(
        f"INSERT INTO sessions ({col_sql}) VALUES ({placeholders})",
        [values[c] for c in cols],
    )

    for table in _CHILD_SESSION_ID_TABLES:
        if not _table_exists(conn, table):
            continue
        conn.execute(
            f"UPDATE {table} SET session_id = ? WHERE session_id = ?",
            (new_id, old_id),
        )

    if _table_exists(conn, "task_clusters"):
        conn.execute(
            "UPDATE task_clusters SET root_session_id = ? WHERE root_session_id = ?",
            (new_id, old_id),
        )
    if _table_exists(conn, "performance_experiment_assignments"):
        conn.execute(
            """
            UPDATE performance_experiment_assignments
            SET root_session_id = ? WHERE root_session_id = ?
            """,
            (new_id, old_id),
        )
    if _table_exists(conn, "performance_experiment_exclusions"):
        conn.execute(
            """
            UPDATE performance_experiment_exclusions
            SET root_session_id = ? WHERE root_session_id = ?
            """,
            (new_id, old_id),
        )

    # Windows still carry old content_hash ids; rebuild under new session id.
    _rebuild_windows(conn, new_id)

    conn.execute("DELETE FROM sessions WHERE id = ?", (old_id,))
    return labels


def _merge_metadata(conn: sqlite3.Connection, winner_id: str, loser_ids: list[str]) -> None:
    winner = conn.execute(
        "SELECT * FROM sessions WHERE id = ?", (winner_id,)
    ).fetchone()
    if winner is None:
        return
    repo = winner["repo"]
    cwd = winner["cwd"]
    branch = winner["branch"]
    model = winner["model"]
    effort = winner["effort"]
    effort_source = winner["effort_source"]
    started_at = winner["started_at"]
    ended_at = winner["ended_at"]
    parent = winner["parent_session_id"]
    commit_sha = winner["commit_sha"]

    for lid in loser_ids:
        loser = conn.execute("SELECT * FROM sessions WHERE id = ?", (lid,)).fetchone()
        if loser is None:
            continue
        new_repo = prefer_repo(repo, loser["repo"])
        if new_repo != repo and new_repo == loser["repo"]:
            cwd = loser["cwd"] or cwd
        repo = new_repo
        branch = branch or loser["branch"]
        model = model or loser["model"]
        effort = effort or loser["effort"]
        effort_source = effort_source or loser["effort_source"]
        commit_sha = commit_sha or loser["commit_sha"]
        parent = parent or loser["parent_session_id"]
        if loser["started_at"] and (
            not started_at or str(loser["started_at"]) < str(started_at)
        ):
            started_at = loser["started_at"]
        if loser["ended_at"] and (
            not ended_at or str(loser["ended_at"]) > str(ended_at)
        ):
            ended_at = loser["ended_at"]

    if parent:
        parent = canonical_external_id(str(parent))

    conn.execute(
        """
        UPDATE sessions SET
            repo = ?, cwd = ?, branch = ?, model = ?, effort = ?,
            effort_source = ?, started_at = ?, ended_at = ?,
            parent_session_id = ?, commit_sha = ?
        WHERE id = ?
        """,
        (
            repo,
            cwd,
            branch,
            model,
            effort,
            effort_source,
            started_at,
            ended_at,
            parent,
            commit_sha,
            winner_id,
        ),
    )


def merge_cursor_duplicates(conn: sqlite3.Connection) -> MergeStats:
    """Collapse path-prefixed Cursor duplicates onto composer-UUID session ids."""
    stats = MergeStats()
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")

    def _load_groups() -> dict[str, list[sqlite3.Row]]:
        rows = conn.execute(
            """
            SELECT id, external_id, repo, parent_session_id
            FROM sessions
            WHERE harness = 'cursor'
            """
        ).fetchall()
        groups: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for row in rows:
            groups[canonical_external_id(str(row["external_id"]))].append(row)
        return groups

    groups = _load_groups()

    for canon, members in sorted(groups.items()):
        if len(members) <= 1:
            continue

        def _merge_one() -> tuple[str, list[str], int, int]:
            # Re-read members each attempt — concurrent writers may change counts.
            live = conn.execute(
                """
                SELECT id, external_id, repo, parent_session_id
                FROM sessions
                WHERE harness = 'cursor' AND (
                    external_id = ?
                    OR external_id LIKE '%/' || ?
                    OR external_id LIKE '%/subagent:' || ?
                    OR external_id = 'subagent:' || ?
                )
                """,
                (canon, canon, canon, canon),
            ).fetchall()
            if len(live) <= 1:
                return "", [], 0, 0
            ranked = sorted(
                live,
                key=lambda r: (
                    -_msg_count(conn, str(r["id"])),
                    -_tool_count(conn, str(r["id"])),
                    0
                    if r["repo"] not in (None, "", "empty-window", "unknown")
                    else 1,
                    str(r["id"]),
                ),
            )
            winner_id = str(ranked[0]["id"])
            loser_ids = [str(r["id"]) for r in ranked[1:]]
            labels = 0
            try:
                _merge_metadata(conn, winner_id, loser_ids)
                for lid in loser_ids:
                    labels += remap_labels_by_window_text(
                        conn, from_session_id=lid, to_session_id=winner_id
                    )
                    conn.execute("DELETE FROM sessions WHERE id = ?", (lid,))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            return winner_id, loser_ids, labels, _msg_count(conn, winner_id)

        winner_id, loser_ids, labels, win_msgs = _with_busy_retry(_merge_one)
        if not winner_id:
            continue
        stats.labels_remapped += labels
        stats.sessions_deleted += len(loser_ids)
        stats.groups_merged += 1
        stats.details.append(
            {
                "canonical": canon,
                "winner": winner_id,
                "deleted": loser_ids,
                "winner_messages": win_msgs,
            }
        )
        log.info(
            "merged cursor group %s keep=%s drop=%s",
            canon,
            winner_id,
            loser_ids,
        )

    # Rename remaining path-prefixed / subagent-prefixed ids to bare UUID.
    remaining = conn.execute(
        "SELECT id, external_id FROM sessions WHERE harness = 'cursor'"
    ).fetchall()
    for row in remaining:
        old_id = str(row["id"])
        old_ext = str(row["external_id"])
        canon = canonical_external_id(old_ext)
        if old_ext == canon:
            continue

        def _rename_one(oid: str = old_id, ext: str = canon) -> int:
            try:
                n = rename_session(conn, old_id=oid, new_external_id=ext)
                conn.commit()
                return n
            except Exception:
                conn.rollback()
                raise

        stats.labels_remapped += _with_busy_retry(_rename_one)
        stats.sessions_renamed += 1

    def _rewrite_parents() -> int:
        rewritten = 0
        try:
            parents = conn.execute(
                """
                SELECT id, parent_session_id FROM sessions
                WHERE harness = 'cursor' AND parent_session_id IS NOT NULL
                  AND TRIM(parent_session_id) != ''
                """
            ).fetchall()
            for row in parents:
                raw = str(row["parent_session_id"])
                ref = raw.split(":", 1)[-1] if raw.startswith("cursor:") else raw
                parent_canon = canonical_external_id(ref)
                if parent_canon != raw:
                    conn.execute(
                        "UPDATE sessions SET parent_session_id = ? WHERE id = ?",
                        (parent_canon, row["id"]),
                    )
                    rewritten += 1
            refresh_label_links(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return rewritten

    stats.parents_rewritten += _with_busy_retry(_rewrite_parents)
    return stats
