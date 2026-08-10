"""Root task-cluster resolution.

The analytical unit for Type B metrics is the root task cluster: a root session
plus every descendant (eval-architecture.md §4.1). Parent pointers in `sessions`
are written by ingest adapters as whatever the harness recorded, which is
sometimes a canonical `harness:external_id` and sometimes a bare `external_id`,
possibly belonging to another harness. Resolution therefore has to try several
representations before walking upward.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict

# Depth bound protects against a corrupt parent chain that never terminates
# even after the visited-set check (defensive; real trees are 2-3 deep).
MAX_ANCESTRY_DEPTH = 64


def _parent_index(
    conn: sqlite3.Connection,
) -> tuple[dict[str, tuple[str, str, str | None]], dict[str, list[str]]]:
    sessions: dict[str, tuple[str, str, str | None]] = {}
    by_external: dict[str, list[str]] = defaultdict(list)
    for row in conn.execute(
        "SELECT id, harness, external_id, parent_session_id FROM sessions"
    ):
        sid = str(row["id"])
        harness = str(row["harness"] or "")
        external = str(row["external_id"] or "")
        parent = row["parent_session_id"]
        sessions[sid] = (harness, external, str(parent) if parent else None)
        by_external[external].append(sid)
    for ids in by_external.values():
        ids.sort()
    return sessions, by_external


def _resolve_parent(
    session_id: str,
    sessions: dict[str, tuple[str, str, str | None]],
    by_external: dict[str, list[str]],
) -> str | None:
    harness, _external, parent = sessions[session_id]
    if not parent or parent == session_id:
        return None
    if parent in sessions:
        return parent
    candidates = by_external.get(parent, [])
    same_harness = [c for c in candidates if sessions[c][0] == harness]
    if len(same_harness) == 1:
        return same_harness[0]
    # Cross-harness handoff: accept only when the external id is unambiguous.
    if len(candidates) == 1:
        return candidates[0]
    return None


def resolve_session_roots(conn: sqlite3.Connection) -> dict[str, str]:
    """Map every session id to the id of its canonical root cluster."""
    sessions, by_external = _parent_index(conn)
    roots: dict[str, str] = {}
    for session_id in sessions:
        if session_id in roots:
            continue
        path: list[str] = []
        seen: set[str] = set()
        current: str | None = session_id
        root: str | None = None
        while current is not None:
            if current in roots:
                root = roots[current]
                break
            if current in seen or len(path) >= MAX_ANCESTRY_DEPTH:
                # Cycle or runaway chain: collapse to a deterministic member.
                root = min(seen)
                break
            path.append(current)
            seen.add(current)
            parent = _resolve_parent(current, sessions, by_external)
            if parent is None:
                root = current
                break
            current = parent
        assert root is not None
        for node in path:
            roots[node] = root
    return roots
