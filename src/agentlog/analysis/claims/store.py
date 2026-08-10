"""Persist claims and proposals with short WAL-friendly transactions."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import time

from agentlog.analysis.claims.models import (
    DECIDED_STATUSES,
    SYSTEM_STATUSES,
    Claim,
    ClaimEvidence,
    Proposal,
    ProposalStatus,
    value_json,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _with_busy_retry(fn, *, conn: sqlite3.Connection, attempts: int = 8) -> None:
    last: BaseException | None = None
    for i in range(attempts):
        try:
            fn()
            return
        except sqlite3.OperationalError as exc:
            last = exc
            msg = str(exc).lower()
            if ("locked" not in msg and "busy" not in msg) or i >= attempts - 1:
                raise
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            time.sleep(0.05 * (2**i))
    assert last is not None
    raise last


def _table_ready(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='claims'"
    ).fetchone()
    return row is not None


def _row_claim(row: sqlite3.Row, evidence: list[ClaimEvidence] | None = None) -> Claim:
    return Claim(
        id=str(row["id"]),
        kind=str(row["kind"]),
        subject=str(row["subject"]),
        predicate=str(row["predicate"]),
        value=json.loads(row["value_json"] or "{}"),
        scope_type=row["scope_type"],
        scope_id=row["scope_id"],
        derivation=row["derivation"],
        status=row["status"],
        support_status=row["support_status"],
        sample_size=int(row["sample_size"] or 0),
        denominator=row["denominator"],
        rate=row["rate"],
        observed_at=str(row["observed_at"] or ""),
        extractor_name=str(row["extractor_name"] or ""),
        extractor_version=str(row["extractor_version"] or ""),
        confidence_basis=json.loads(row["confidence_basis_json"] or "{}"),
        does_not_prove=str(row["does_not_prove"] or ""),
        supersedes_id=row["supersedes_id"],
        evidence=evidence or [],
        created_at=str(row["created_at"] or ""),
        updated_at=str(row["updated_at"] or ""),
    )


def _load_evidence(conn: sqlite3.Connection, claim_id: str) -> list[ClaimEvidence]:
    rows = conn.execute(
        """
        SELECT session_id, window_id, message_id, quote, meta_json
        FROM claim_evidence WHERE claim_id = ?
        ORDER BY created_at ASC
        """,
        (claim_id,),
    ).fetchall()
    out: list[ClaimEvidence] = []
    for r in rows:
        out.append(
            ClaimEvidence(
                session_id=r["session_id"],
                window_id=r["window_id"],
                message_id=r["message_id"],
                quote=r["quote"],
                meta=json.loads(r["meta_json"] or "{}"),
            )
        )
    return out


def upsert_claims(conn: sqlite3.Connection, claims: Iterable[Claim]) -> int:
    if not _table_ready(conn):
        return 0
    now = _utc_now()
    count = 0

    def _write() -> None:
        nonlocal count
        for claim in claims:
            existing = conn.execute(
                "SELECT id, status FROM claims WHERE id = ?", (claim.id,)
            ).fetchone()
            status = claim.status
            if existing and existing["status"] in {"approved", "rejected", "published"}:
                status = existing["status"]
            if claim.supersedes_id == claim.id:
                claim.supersedes_id = None
            if claim.supersedes_id and claim.supersedes_id != claim.id:
                conn.execute(
                    """
                    UPDATE claims
                    SET status = 'superseded', updated_at = ?
                    WHERE id = ? AND status NOT IN ('rejected')
                    """,
                    (now, claim.supersedes_id),
                )
            conn.execute(
                """
                INSERT INTO claims (
                    id, kind, subject, predicate, value_json, scope_type, scope_id,
                    derivation, status, support_status, sample_size, denominator,
                    rate, observed_at, extractor_name, extractor_version,
                    confidence_basis_json, does_not_prove, supersedes_id,
                    created_at, updated_at
                ) VALUES (
                    ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
                )
                ON CONFLICT(id) DO UPDATE SET
                    value_json = excluded.value_json,
                    support_status = excluded.support_status,
                    sample_size = excluded.sample_size,
                    denominator = excluded.denominator,
                    rate = excluded.rate,
                    observed_at = excluded.observed_at,
                    confidence_basis_json = excluded.confidence_basis_json,
                    does_not_prove = excluded.does_not_prove,
                    supersedes_id = excluded.supersedes_id,
                    status = CASE
                        WHEN claims.status IN ('approved','rejected','published')
                        THEN claims.status ELSE excluded.status END,
                    updated_at = excluded.updated_at
                """,
                (
                    claim.id,
                    claim.kind,
                    claim.subject,
                    claim.predicate,
                    value_json(claim.value),
                    claim.scope_type,
                    claim.scope_id,
                    claim.derivation,
                    status,
                    claim.support_status,
                    claim.sample_size,
                    claim.denominator,
                    claim.rate,
                    claim.observed_at or now,
                    claim.extractor_name,
                    claim.extractor_version,
                    json.dumps(claim.confidence_basis, sort_keys=True),
                    claim.does_not_prove,
                    claim.supersedes_id,
                    claim.created_at or now,
                    now,
                ),
            )
            conn.execute(
                "DELETE FROM claim_evidence WHERE claim_id = ?", (claim.id,)
            )
            for idx, ev in enumerate(claim.evidence):
                eid = f"{claim.id}:e:{idx}"
                conn.execute(
                    """
                    INSERT INTO claim_evidence (
                        id, claim_id, session_id, window_id, message_id,
                        quote, meta_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        eid,
                        claim.id,
                        ev.session_id,
                        ev.window_id,
                        ev.message_id,
                        ev.quote,
                        json.dumps(ev.meta, sort_keys=True),
                        now,
                    ),
                )
            count += 1

    _with_busy_retry(_write, conn=conn)
    return count


def list_claims(
    conn: sqlite3.Connection,
    *,
    status: str | None = "candidate",
    kind: str | None = None,
    derivation: str | None = None,
    support_status: str | None = None,
    include_evidence: bool = True,
    limit: int = 200,
) -> list[Claim]:
    if not _table_ready(conn):
        return []
    clauses = ["1=1"]
    params: list[Any] = []
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    if kind is not None:
        clauses.append("kind = ?")
        params.append(kind)
    if derivation is not None:
        clauses.append("derivation = ?")
        params.append(derivation)
    if support_status is not None:
        clauses.append("support_status = ?")
        params.append(support_status)
    where = " AND ".join(clauses)
    rows = conn.execute(
        f"""
        SELECT * FROM claims
        WHERE {where}
        ORDER BY observed_at DESC, kind ASC, subject ASC
        LIMIT ?
        """,
        [*params, limit],
    ).fetchall()
    out: list[Claim] = []
    for r in rows:
        evidence = _load_evidence(conn, str(r["id"])) if include_evidence else []
        out.append(_row_claim(r, evidence))
    return out


def get_claim(conn: sqlite3.Connection, claim_id: str) -> Claim | None:
    if not _table_ready(conn):
        return None
    row = conn.execute(
        "SELECT * FROM claims WHERE id = ?", (claim_id,)
    ).fetchone()
    if row is None:
        return None
    return _row_claim(row, _load_evidence(conn, claim_id))


def _proposal_columns(conn: sqlite3.Connection) -> set[str]:
    return {str(r[1]) for r in conn.execute("PRAGMA table_info(proposals)")}


def upsert_proposals(conn: sqlite3.Connection, proposals: Iterable[Proposal]) -> int:
    if not _table_ready(conn):
        return 0
    now = _utc_now()
    count = 0
    cols = _proposal_columns(conn)
    has_provenance = "provenance_json" in cols

    def _write() -> None:
        nonlocal count
        for p in proposals:
            existing = conn.execute(
                "SELECT id, status FROM proposals WHERE id = ?", (p.id,)
            ).fetchone()
            if existing and existing["status"] in DECIDED_STATUSES:
                # Do not clobber owner decisions.
                continue
            if has_provenance:
                conn.execute(
                    """
                    INSERT INTO proposals (
                        id, title, action, status, target_path, target_kind,
                        scope_type, scope_id, base_content_hash, unified_diff,
                        proposed_content, rationale, derivation_summary,
                        does_not_prove, sample_size, created_at, updated_at,
                        decided_at, decision_note, provenance_json, run_id,
                        model, prompt_hash, evidence_pack_hash
                    ) VALUES (
                        ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
                    )
                    ON CONFLICT(id) DO UPDATE SET
                        title = excluded.title,
                        action = excluded.action,
                        status = CASE
                            WHEN proposals.status = 'superseded' THEN 'pending'
                            ELSE proposals.status
                        END,
                        target_path = excluded.target_path,
                        target_kind = excluded.target_kind,
                        scope_type = excluded.scope_type,
                        scope_id = excluded.scope_id,
                        base_content_hash = excluded.base_content_hash,
                        unified_diff = excluded.unified_diff,
                        proposed_content = excluded.proposed_content,
                        rationale = excluded.rationale,
                        derivation_summary = excluded.derivation_summary,
                        does_not_prove = excluded.does_not_prove,
                        sample_size = excluded.sample_size,
                        provenance_json = excluded.provenance_json,
                        run_id = excluded.run_id,
                        model = excluded.model,
                        prompt_hash = excluded.prompt_hash,
                        evidence_pack_hash = excluded.evidence_pack_hash,
                        decided_at = CASE
                            WHEN proposals.status = 'superseded' THEN NULL
                            ELSE proposals.decided_at
                        END,
                        decision_note = CASE
                            WHEN proposals.status = 'superseded' THEN NULL
                            ELSE proposals.decision_note
                        END,
                        updated_at = excluded.updated_at
                    WHERE proposals.status IN ('pending', 'superseded')
                    """,
                    (
                        p.id,
                        p.title,
                        p.action,
                        p.status,
                        p.target_path,
                        p.target_kind,
                        p.scope_type,
                        p.scope_id,
                        p.base_content_hash,
                        p.unified_diff,
                        p.proposed_content,
                        p.rationale,
                        p.derivation_summary,
                        p.does_not_prove,
                        p.sample_size,
                        p.created_at or now,
                        now,
                        p.decided_at,
                        p.decision_note,
                        json.dumps(p.provenance or {}, sort_keys=True),
                        p.run_id,
                        p.model,
                        p.prompt_hash,
                        p.evidence_pack_hash,
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO proposals (
                        id, title, action, status, target_path, target_kind,
                        scope_type, scope_id, base_content_hash, unified_diff,
                        proposed_content, rationale, derivation_summary,
                        does_not_prove, sample_size, created_at, updated_at,
                        decided_at, decision_note
                    ) VALUES (
                        ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
                    )
                    ON CONFLICT(id) DO UPDATE SET
                        title = excluded.title,
                        action = excluded.action,
                        status = CASE
                            WHEN proposals.status = 'superseded' THEN 'pending'
                            ELSE proposals.status
                        END,
                        target_path = excluded.target_path,
                        target_kind = excluded.target_kind,
                        scope_type = excluded.scope_type,
                        scope_id = excluded.scope_id,
                        base_content_hash = excluded.base_content_hash,
                        unified_diff = excluded.unified_diff,
                        proposed_content = excluded.proposed_content,
                        rationale = excluded.rationale,
                        derivation_summary = excluded.derivation_summary,
                        does_not_prove = excluded.does_not_prove,
                        sample_size = excluded.sample_size,
                        decided_at = CASE
                            WHEN proposals.status = 'superseded' THEN NULL
                            ELSE proposals.decided_at
                        END,
                        decision_note = CASE
                            WHEN proposals.status = 'superseded' THEN NULL
                            ELSE proposals.decision_note
                        END,
                        updated_at = excluded.updated_at
                    WHERE proposals.status IN ('pending', 'superseded')
                    """,
                    (
                        p.id,
                        p.title,
                        p.action,
                        p.status,
                        p.target_path,
                        p.target_kind,
                        p.scope_type,
                        p.scope_id,
                        p.base_content_hash,
                        p.unified_diff,
                        p.proposed_content,
                        p.rationale,
                        p.derivation_summary,
                        p.does_not_prove,
                        p.sample_size,
                        p.created_at or now,
                        now,
                        p.decided_at,
                        p.decision_note,
                    ),
                )
            conn.execute(
                "DELETE FROM proposal_claims WHERE proposal_id = ?", (p.id,)
            )
            for cid in p.claim_ids:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO proposal_claims (proposal_id, claim_id)
                    VALUES (?, ?)
                    """,
                    (p.id, cid),
                )
            count += 1

    _with_busy_retry(_write, conn=conn)
    return count


def _row_proposal(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    include_claims: bool,
) -> Proposal:
    claim_ids = [
        str(r["claim_id"])
        for r in conn.execute(
            "SELECT claim_id FROM proposal_claims WHERE proposal_id = ?",
            (row["id"],),
        ).fetchall()
    ]
    claims: list[Claim] = []
    if include_claims:
        for cid in claim_ids:
            c = get_claim(conn, cid)
            if c is not None:
                claims.append(c)
    keys = set(row.keys())
    provenance: dict[str, Any] = {}
    if "provenance_json" in keys and row["provenance_json"]:
        try:
            provenance = json.loads(row["provenance_json"])
        except json.JSONDecodeError:
            provenance = {}
    return Proposal(
        id=str(row["id"]),
        title=str(row["title"]),
        action=row["action"],
        status=row["status"],
        target_path=str(row["target_path"]),
        target_kind=str(row["target_kind"]),
        scope_type=row["scope_type"],
        scope_id=row["scope_id"],
        base_content_hash=row["base_content_hash"],
        unified_diff=str(row["unified_diff"] or ""),
        proposed_content=row["proposed_content"],
        rationale=str(row["rationale"] or ""),
        derivation_summary=str(row["derivation_summary"] or ""),
        does_not_prove=str(row["does_not_prove"] or ""),
        sample_size=int(row["sample_size"] or 0),
        claim_ids=claim_ids,
        claims=claims,
        created_at=str(row["created_at"] or ""),
        updated_at=str(row["updated_at"] or ""),
        decided_at=row["decided_at"],
        decision_note=row["decision_note"],
        provenance=provenance,
        run_id=row["run_id"] if "run_id" in keys else None,
        model=row["model"] if "model" in keys else None,
        prompt_hash=row["prompt_hash"] if "prompt_hash" in keys else None,
        evidence_pack_hash=(
            row["evidence_pack_hash"] if "evidence_pack_hash" in keys else None
        ),
    )


def list_proposals(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    include_claims: bool = True,
    limit: int = 100,
) -> list[Proposal]:
    if not _table_ready(conn):
        return []
    if status is None:
        rows = conn.execute(
            """
            SELECT * FROM proposals
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT * FROM proposals
            WHERE status = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (status, limit),
        ).fetchall()
    return [_row_proposal(conn, r, include_claims=include_claims) for r in rows]


def count_proposals_by_status(conn: sqlite3.Connection) -> dict[str, int]:
    counts = {
        "pending": 0,
        "accepted": 0,
        "rejected": 0,
        "deferred": 0,
        "superseded": 0,
    }
    if not _table_ready(conn):
        return counts
    for row in conn.execute(
        "SELECT status, COUNT(*) AS n FROM proposals GROUP BY status"
    ):
        counts[str(row["status"])] = int(row["n"])
    return counts


def get_proposal(
    conn: sqlite3.Connection,
    proposal_id: str,
    *,
    include_claims: bool = True,
) -> Proposal | None:
    if not _table_ready(conn):
        return None
    row = conn.execute(
        "SELECT * FROM proposals WHERE id = ?", (proposal_id,)
    ).fetchone()
    if row is None:
        return None
    return _row_proposal(conn, row, include_claims=include_claims)


OWNER_STATUSES: frozenset[str] = frozenset({"pending", *DECIDED_STATUSES})
VALID_STATUSES: frozenset[str] = frozenset({*OWNER_STATUSES, *SYSTEM_STATUSES})


def set_proposal_status(
    conn: sqlite3.Connection,
    proposal_id: str,
    status: ProposalStatus,
    *,
    note: str | None = None,
    allow_system: bool = False,
) -> Proposal:
    """Record a proposal status change. Never touches the target file.

    Owner decisions are pending/accepted/rejected/deferred. System pruning uses
    superseded and must pass ``allow_system=True`` so owner Rejected stays clean.
    """
    prop = get_proposal(conn, proposal_id, include_claims=False)
    if prop is None:
        raise KeyError(f"proposal not found: {proposal_id}")
    if status in SYSTEM_STATUSES and not allow_system:
        raise ValueError(
            f"{status!r} is a system status; owner decisions are "
            f"{sorted(OWNER_STATUSES)}"
        )
    if status not in VALID_STATUSES:
        raise ValueError(
            f"unknown status {status!r}; expected one of {sorted(VALID_STATUSES)}"
        )
    now = _utc_now()

    def _write() -> None:
        fields = ["status = ?", "updated_at = ?"]
        params: list[Any] = [status, now]
        if status == "pending":
            fields.append("decided_at = NULL")
            fields.append("decision_note = NULL")
        else:
            fields.append("decided_at = ?")
            params.append(now)
            fields.append("decision_note = ?")
            params.append(note)
        params.append(proposal_id)
        conn.execute(
            f"UPDATE proposals SET {', '.join(fields)} WHERE id = ?",
            params,
        )
        conn.execute(
            """
            INSERT INTO proposal_events
                (id, proposal_id, event_type, detail_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                f"{proposal_id}:{status}:{uuid.uuid4().hex[:12]}",
                proposal_id,
                status,
                json.dumps(
                    {"note": note, "actor": "system" if allow_system else "owner"},
                    sort_keys=True,
                ),
                now,
            ),
        )

    _with_busy_retry(_write, conn=conn)
    out = get_proposal(conn, proposal_id)
    assert out is not None
    return out


def list_decision_events(
    conn: sqlite3.Connection, proposal_id: str
) -> list[dict[str, Any]]:
    if not _table_ready(conn):
        return []
    rows = conn.execute(
        """
        SELECT event_type, detail_json, created_at
        FROM proposal_events WHERE proposal_id = ?
        ORDER BY created_at ASC
        """,
        (proposal_id,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        try:
            detail = json.loads(r["detail_json"] or "{}")
        except json.JSONDecodeError:
            detail = {}
        out.append(
            {
                "decision": str(r["event_type"]),
                "note": detail.get("note") if isinstance(detail, dict) else None,
                "at": str(r["created_at"] or ""),
            }
        )
    return out


def target_state(proposal: Proposal) -> dict[str, Any]:
    """Read-only look at the target file so the board can report presence.

    Hash comparison only: it reports whether the file currently matches the
    proposed content, not that this proposal caused any change.
    """
    path = Path(proposal.target_path)
    try:
        exists = path.is_file()
    except OSError:
        exists = False
    if not exists:
        return {
            "exists": False,
            "current_content_hash": None,
            "matches_proposed": False,
            "changed_since_proposal": bool(proposal.base_content_hash),
        }
    try:
        data = path.read_bytes()
    except OSError:
        return {
            "exists": True,
            "current_content_hash": None,
            "matches_proposed": False,
            "changed_since_proposal": False,
        }
    current = hashlib.sha1(data).hexdigest()
    proposed_hash = (
        hashlib.sha1(proposal.proposed_content.encode("utf-8")).hexdigest()
        if proposal.proposed_content is not None
        else None
    )
    return {
        "exists": True,
        "current_content_hash": current,
        "matches_proposed": proposed_hash is not None and current == proposed_hash,
        "changed_since_proposal": bool(
            proposal.base_content_hash and current != proposal.base_content_hash
        ),
    }


def enrich_with_correspondence(
    conn: sqlite3.Connection, proposal: Proposal
) -> dict[str, Any]:
    """Attach ledger correspondence for accepted proposals (association only)."""
    from agentlog.analysis.config_ledger import proposal_correspondence

    return proposal_correspondence(conn, proposal)
