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

from agentlog.session_identity import (
    build_identity_context,
    lineage_parent_ids,
    logical_orchestrator_id,
)

# Depth bound protects against a corrupt parent chain that never terminates
# even after the visited-set check (defensive; real trees are 2-3 deep).
MAX_ANCESTRY_DEPTH = 64


def resolve_session_roots(conn: sqlite3.Connection) -> dict[str, str]:
    """Map every session id to the id of its canonical root cluster."""
    sessions = {
        str(row["id"])
        for row in conn.execute("SELECT id FROM sessions")
    }
    parents = lineage_parent_ids(conn)
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
            parent = parents.get(current)
            if parent is None:
                root = current
                break
            current = parent
        assert root is not None
        for node in path:
            roots[node] = root
    identity = build_identity_context(conn)
    for session_id in roots:
        owner = logical_orchestrator_id(conn, session_id, context=identity)
        if owner is not None:
            roots[session_id] = owner
    return roots
