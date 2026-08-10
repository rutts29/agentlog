"""Logical session identity over immutable physical session records."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any


def _has_links_table(conn: sqlite3.Connection) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'session_links'"
        ).fetchone()
        is not None
    )


@dataclass
class IdentityContext:
    backings_by_source: dict[str, list[dict[str, Any]]] = field(
        default_factory=dict
    )
    owners_by_session: dict[str, set[str]] = field(default_factory=dict)
    owned_session_ids: set[str] = field(default_factory=set)
    root_backing_ids: set[str] = field(default_factory=set)
    canonical_root_backing_by_source: dict[str, str] = field(
        default_factory=dict
    )


_PROVIDER_FAMILIES = {
    "codex": "codex",
    "openai": "codex",
    "gpt": "codex",
    "claude": "anthropic",
    "claudeagent": "anthropic",
    "anthropic": "anthropic",
    "grok": "xai",
    "xai": "xai",
    "cursor": "cursor",
    "opencode": "opencode",
}


def _provider_family(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if not normalized:
        return None
    for prefix, family in _PROVIDER_FAMILIES.items():
        if normalized == prefix or normalized.startswith(f"{prefix}-"):
            return family
    return None


def _is_current_provider_episode(
    conn: sqlite3.Connection,
    source_id: str,
    backing: dict[str, Any],
) -> bool:
    source = conn.execute(
        "SELECT provider, agent_profile, model_canonical FROM sessions WHERE id = ?",
        (source_id,),
    ).fetchone()
    target = conn.execute(
        "SELECT provider, agent_profile, model_canonical FROM sessions WHERE id = ?",
        (backing["target_session_id"],),
    ).fetchone()
    if source is None or target is None:
        return False
    source_provider = next(
        (
            family
            for value in (
                source["provider"],
                source["agent_profile"],
                source["model_canonical"],
            )
            for family in [_provider_family(value)]
            if family is not None
        ),
        None,
    )
    target_provider = next(
        (
            family
            for value in (
                target["provider"],
                target["agent_profile"],
                target["model_canonical"],
                backing.get("target_harness"),
            )
            for family in [_provider_family(value)]
            if family is not None
        ),
        None,
    )
    if source_provider and target_provider and source_provider != target_provider:
        return False
    if target_provider is None:
        return True
    source_episodes = conn.execute(
        """
        SELECT provider, agent_profile, model_canonical
        FROM messages
        WHERE session_id = ?
          AND role = 'assistant'
        """,
        (source_id,),
    ).fetchall()
    return not any(
        family is not None and family != target_provider
        for row in source_episodes
        for value in (row["provider"], row["agent_profile"], row["model_canonical"])
        for family in [_provider_family(value)]
    )


def _root_backings(backings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    roots = [backing for backing in backings if backing.get("link_role") == "root"]
    if roots:
        return roots
    if len(backings) != 1:
        return []
    role = backings[0].get("link_role")
    return backings if role in {None, "", "unknown"} else []


def build_identity_context(conn: sqlite3.Connection) -> IdentityContext:
    context = IdentityContext()
    if not _has_links_table(conn):
        return context
    rows = conn.execute(
        """
        SELECT l.source_session_id, l.target_session_id, l.target_harness,
               l.target_external_id, l.link_role, l.confidence, l.evidence_json,
               source.harness AS source_harness
        FROM session_links l
        JOIN sessions source ON source.id = l.source_session_id
        WHERE l.link_type = 'provider_backing'
        ORDER BY l.target_harness, l.target_external_id
        """
    ).fetchall()
    for row in rows:
        link = {
            "target_session_id": row["target_session_id"],
            "target_harness": row["target_harness"],
            "target_external_id": row["target_external_id"],
            "link_role": row["link_role"],
            "confidence": row["confidence"],
            "evidence_json": row["evidence_json"],
        }
        source_id = str(row["source_session_id"])
        context.backings_by_source.setdefault(source_id, []).append(link)
        if row["source_harness"] == "t3code" and row["target_session_id"]:
            context.owners_by_session.setdefault(
                str(row["target_session_id"]), set()
            ).add(source_id)
    if not context.owners_by_session:
        return context
    parents = _physical_parent_ids(conn)
    changed = True
    while changed:
        changed = False
        for child_id, parent_id in parents.items():
            parent_owners = context.owners_by_session.get(parent_id)
            if not parent_owners:
                continue
            child_owners = context.owners_by_session.setdefault(child_id, set())
            before = len(child_owners)
            child_owners.update(parent_owners)
            changed = changed or len(child_owners) != before
    context.owned_session_ids = {
        session_id
        for session_id, owners in context.owners_by_session.items()
        if len(owners) == 1
    }
    for source_id, backings in context.backings_by_source.items():
        roots = _root_backings(backings)
        for root in roots:
            target_id = root["target_session_id"]
            if not target_id:
                continue
            owners = context.owners_by_session.get(str(target_id), set())
            if owners == {source_id}:
                context.root_backing_ids.add(str(target_id))
        if len(roots) != 1:
            continue
        target_id = roots[0]["target_session_id"]
        if (
            target_id
            and context.owners_by_session.get(str(target_id), set()) == {source_id}
            and _is_current_provider_episode(conn, source_id, roots[0])
        ):
            context.canonical_root_backing_by_source[source_id] = str(target_id)
    return context


def provider_backings(
    conn: sqlite3.Connection,
    orchestrator_session_id: str,
    *,
    context: IdentityContext | None = None,
) -> list[dict[str, Any]]:
    identity = context or build_identity_context(conn)
    return list(identity.backings_by_source.get(orchestrator_session_id, []))


def provider_root_backings(
    conn: sqlite3.Connection,
    orchestrator_session_id: str,
    *,
    context: IdentityContext | None = None,
) -> list[dict[str, Any]]:
    return _root_backings(
        provider_backings(conn, orchestrator_session_id, context=context)
    )


def logical_orchestrator_id(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    context: IdentityContext | None = None,
) -> str | None:
    identity = context or build_identity_context(conn)
    owners = identity.owners_by_session.get(session_id, set())
    return next(iter(owners)) if len(owners) == 1 else None


def logical_root_session_id(
    conn: sqlite3.Connection,
    physical_root_session_id: str,
    *,
    context: IdentityContext | None = None,
) -> str:
    return logical_orchestrator_id(
        conn, physical_root_session_id, context=context
    ) or physical_root_session_id


def logical_projection(
    conn: sqlite3.Connection,
    session_id: str,
    harness: str,
    *,
    context: IdentityContext | None = None,
) -> dict[str, Any]:
    identity = context or build_identity_context(conn)
    if harness != "t3code":
        owner = logical_orchestrator_id(conn, session_id, context=identity)
        return {
            "logical_harness": "t3code" if owner else harness,
            "runtime_harness": harness,
            "orchestrator_session_id": owner,
            "transcript_session_id": session_id,
            "provider_backings": [],
        }
    backings = provider_backings(conn, session_id, context=identity)
    roots = _root_backings(backings)
    transcript = None
    if len(roots) == 1 and roots[0]["target_session_id"]:
        target_id = str(roots[0]["target_session_id"])
        if identity.canonical_root_backing_by_source.get(session_id) == target_id:
            transcript = roots[0]
    transcript_id = str(transcript["target_session_id"]) if transcript else None
    runtime_harness = str(transcript["target_harness"]) if transcript else "t3code"
    return {
        "logical_harness": "t3code",
        "runtime_harness": runtime_harness,
        "orchestrator_session_id": session_id,
        "transcript_session_id": transcript_id,
        "provider_backings": backings,
    }


def is_provider_backing_session(conn: sqlite3.Connection, session_id: str) -> bool:
    if not _has_links_table(conn):
        return False
    return (
        conn.execute(
            """
            SELECT 1
            FROM session_links l
            JOIN sessions source ON source.id = l.source_session_id
            WHERE l.link_type = 'provider_backing'
              AND l.target_session_id = ?
              AND source.harness = 't3code'
            LIMIT 1
            """,
            (session_id,),
        ).fetchone()
        is not None
    )


def _physical_parent_ids(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute(
        "SELECT id, harness, external_id, parent_session_id FROM sessions"
    ).fetchall()
    by_id = {str(row["id"]) for row in rows}
    by_external: dict[tuple[str, str], list[str]] = {}
    for row in rows:
        key = (str(row["harness"]), str(row["external_id"]))
        by_external.setdefault(key, []).append(str(row["id"]))
    out: dict[str, str] = {}
    for row in rows:
        parent = row["parent_session_id"]
        if not parent:
            continue
        raw = str(parent)
        if raw in by_id:
            out[str(row["id"])] = raw
            continue
        candidates = by_external.get((str(row["harness"]), raw), [])
        if len(candidates) == 1:
            out[str(row["id"])] = candidates[0]
    return out


def provider_backing_owners(
    conn: sqlite3.Connection, *, context: IdentityContext | None = None
) -> dict[str, set[str]]:
    return (context or build_identity_context(conn)).owners_by_session


def provider_backing_shadow_ids(
    conn: sqlite3.Connection, *, context: IdentityContext | None = None
) -> set[str]:
    return set((context or build_identity_context(conn)).owned_session_ids)


def provider_root_shadow_ids(
    conn: sqlite3.Connection, *, context: IdentityContext | None = None
) -> set[str]:
    return set((context or build_identity_context(conn)).root_backing_ids)


def provider_canonical_root_backing_ids(
    conn: sqlite3.Connection, *, context: IdentityContext | None = None
) -> set[str]:
    return set(
        (context or build_identity_context(conn)).canonical_root_backing_by_source.values()
    )


def provider_backing_exclusion_sql(session_alias: str = "s") -> str:
    return f"""EXISTS (
        SELECT 1 FROM session_links logical_link
        WHERE logical_link.link_type = 'provider_backing'
          AND logical_link.source_session_id = {session_alias}.id
          AND logical_link.target_session_id IS NOT NULL
    )"""
