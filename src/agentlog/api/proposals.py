"""Reviewable learning proposals API.

Advisory only: there is no endpoint that writes a harness configuration file.
The board hands the owner a diff and the full proposed content; the owner
applies it by hand and records a decision here.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from agentlog.analysis.claims import (
    get_proposal,
    list_claims,
    list_proposals,
    refresh_learnings,
    set_proposal_status,
    target_state,
)
from agentlog.analysis.claims.models import DECIDED_STATUSES, Claim, Proposal
from agentlog.analysis.claims.store import (
    count_proposals_by_status,
    enrich_with_correspondence,
    list_decision_events,
)
from agentlog.api.deps import get_conn, get_write_conn

router = APIRouter(tags=["proposals"])

ADVISORY_NOTE = (
    "agentlog proposes; you apply. No endpoint writes to a configuration "
    "file — copy the diff or the proposed content and edit the file yourself."
)

DECISIONS = ("accepted", "rejected", "deferred")


def _evidence_timestamps(
    conn: sqlite3.Connection, claims: list[Claim]
) -> dict[str, dict[str, Any]]:
    """Message timestamps for evidence rows so the board can order citations."""
    message_ids = {
        ev.message_id
        for c in claims
        for ev in c.evidence
        if ev.message_id
    }
    if not message_ids:
        return {}
    out: dict[str, dict[str, Any]] = {}
    ids = list(message_ids)
    for start in range(0, len(ids), 400):
        chunk = ids[start : start + 400]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"""
            SELECT m.id, m.timestamp, m.session_id, s.harness, s.started_at
            FROM messages m
            LEFT JOIN sessions s ON s.id = m.session_id
            WHERE m.id IN ({placeholders})
            """,
            chunk,
        ).fetchall()
        for r in rows:
            out[str(r["id"])] = {
                "timestamp": r["timestamp"] or r["started_at"],
                "harness": r["harness"],
                "session_id": r["session_id"],
            }
    return out


def _session_starts(
    conn: sqlite3.Connection, claims: list[Claim]
) -> dict[str, dict[str, Any]]:
    session_ids = {
        ev.session_id for c in claims for ev in c.evidence if ev.session_id
    }
    if not session_ids:
        return {}
    out: dict[str, dict[str, Any]] = {}
    ids = list(session_ids)
    for start in range(0, len(ids), 400):
        chunk = ids[start : start + 400]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"""
            SELECT id, harness, started_at
            FROM sessions WHERE id IN ({placeholders})
            """,
            chunk,
        ).fetchall()
        for r in rows:
            out[str(r["id"])] = {
                "harness": r["harness"],
                "started_at": r["started_at"],
            }
    return out


def _support_summary(claims: list[Claim]) -> dict[str, Any]:
    """Confidence framing that stays descriptive: support tier plus denominators."""
    if not claims:
        return {
            "tier": "unsupported",
            "derivations": [],
            "sample_size": 0,
            "denominator": None,
            "evidence_count": 0,
            "language": "no linked claim; treat as a suggestion only",
        }
    tiers = {c.support_status for c in claims}
    if "abstain" in tiers:
        tier = "abstain"
    elif "insufficient" in tiers:
        tier = "insufficient"
    else:
        tier = "ok"
    derivations = sorted({c.derivation for c in claims})
    sample = max(c.sample_size for c in claims)
    denominators = [c.denominator for c in claims if c.denominator]
    language = {
        "ok": "observed rate over the linked sessions; association only",
        "insufficient": "below the reporting floor; directional at best",
        "abstain": "sample too small to characterise",
    }[tier]
    return {
        "tier": tier,
        "derivations": derivations,
        "sample_size": sample,
        "denominator": max(denominators) if denominators else None,
        "evidence_count": sum(len(c.evidence) for c in claims),
        "language": language,
    }


def _serialize(
    conn: sqlite3.Connection,
    prop: Proposal,
    *,
    include_events: bool,
) -> dict[str, Any]:
    data = prop.to_dict(include_claims=True)
    msg_meta = _evidence_timestamps(conn, prop.claims)
    sess_meta = _session_starts(conn, prop.claims)
    for claim_payload, claim in zip(data.get("claims", []), prop.claims):
        for ev_payload, ev in zip(claim_payload.get("evidence", []), claim.evidence):
            meta = msg_meta.get(ev.message_id or "") or {}
            session_meta = sess_meta.get(ev.session_id or "") or {}
            ev_payload["timestamp"] = meta.get("timestamp") or session_meta.get(
                "started_at"
            )
            ev_payload["harness"] = meta.get("harness") or session_meta.get("harness")
    data["support"] = _support_summary(prop.claims)
    data["target_state"] = target_state(prop)
    data["correspondence"] = enrich_with_correspondence(conn, prop)
    data["advisory_only"] = True
    if include_events:
        data["decision_events"] = list_decision_events(conn, prop.id)
    return data


@router.get("/api/claims")
def claims_list(
    conn: sqlite3.Connection = Depends(get_conn),
    status: str | None = Query("candidate"),
    kind: str | None = None,
    derivation: str | None = None,
    support_status: str | None = None,
    limit: int = Query(200, ge=1, le=500),
) -> dict:
    items = list_claims(
        conn,
        status=status,
        kind=kind,
        derivation=derivation,
        support_status=support_status,
        include_evidence=True,
        limit=limit,
    )
    return {
        "items": [c.to_dict() for c in items],
        "count": len(items),
        "language_contract": {
            "allowed": "sessions where X was observed showed rate R (n=N)",
            "forbidden": ["improved", "caused", "best model", "effectiveness score"],
        },
    }


@router.get("/api/proposals")
def proposals_list(
    conn: sqlite3.Connection = Depends(get_conn),
    status: str | None = Query(None),
    limit: int = Query(100, ge=1, le=200),
) -> dict:
    items = list_proposals(conn, status=status, include_claims=True, limit=limit)
    return {
        "items": [_serialize(conn, p, include_events=False) for p in items],
        "count": len(items),
        "counts_by_status": count_proposals_by_status(conn),
        "decisions": list(DECISIONS),
        "advisory_only": True,
        "note": ADVISORY_NOTE,
    }


@router.get("/api/proposals/{proposal_id}")
def proposals_get(
    proposal_id: str,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    prop = get_proposal(conn, proposal_id, include_claims=True)
    if prop is None:
        raise HTTPException(status_code=404, detail="proposal not found")
    payload = _serialize(conn, prop, include_events=True)
    payload["note"] = ADVISORY_NOTE
    return payload


@router.post("/api/proposals/refresh")
def proposals_refresh(
    conn: sqlite3.Connection = Depends(get_write_conn),
) -> dict:
    """Re-derive claims and refresh pending proposals from the evidence ledger."""
    stats = refresh_learnings(conn)
    stats["note"] = ADVISORY_NOTE
    return stats


@router.get("/api/config-ledger")
def config_ledger_get(
    conn: sqlite3.Connection = Depends(get_conn),
    path: str | None = None,
    limit: int = Query(50, ge=1, le=200),
) -> dict:
    from agentlog.analysis.config_ledger import ledger_summary, list_snapshots

    return {
        "summary": ledger_summary(conn),
        "items": list_snapshots(conn, path=path, limit=limit),
        "note": (
            "Dated hashes of instruction files. Association only — a change "
            "near a proposal is not evidence the proposal caused it."
        ),
    }


@router.post("/api/config-ledger/refresh")
def config_ledger_refresh(
    conn: sqlite3.Connection = Depends(get_write_conn),
    include_git: bool = Query(True),
) -> dict:
    from agentlog.analysis.config_ledger import (
        backup_agentlog_db,
        ledger_summary,
        refresh_config_ledger,
    )

    bak = backup_agentlog_db(reason="config_ledger_api")
    stats = refresh_config_ledger(conn, include_git_history=include_git)
    return {
        "backup": str(bak),
        "refresh": stats,
        "summary": ledger_summary(conn),
    }


@router.post("/api/proposals/{proposal_id}/decision")
def proposals_decide(
    proposal_id: str,
    decision: str = Body(..., embed=True),
    note: str | None = Body(None, embed=True),
    conn: sqlite3.Connection = Depends(get_write_conn),
) -> dict:
    """Record Accepted / Rejected / Deferred. Writes nothing outside the DB."""
    if decision not in DECIDED_STATUSES and decision != "pending":
        raise HTTPException(
            status_code=422,
            detail=f"decision must be one of {[*DECISIONS, 'pending']}",
        )
    try:
        prop = set_proposal_status(conn, proposal_id, decision, note=note)  # type: ignore[arg-type]
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    payload = _serialize(conn, prop, include_events=True)
    payload["note"] = ADVISORY_NOTE
    return payload
