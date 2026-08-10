from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from agentlog.analysis.claims.models import Claim, ClaimEvidence
from agentlog.analysis.claims.store import upsert_claims

SESSION_FACT_EXTRACTOR_VERSION = "session_fact_v1"


def _required_text(item: dict[str, Any], key: str) -> str:
    value = str(item.get(key) or "").strip()
    if not value:
        raise ValueError(f"session fact is missing {key}")
    return value


def _evidence_message(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    quote: str,
    message_seq: int | None,
) -> sqlite3.Row:
    session = conn.execute(
        "SELECT repo, harness, started_at FROM sessions WHERE id = ?",
        (session_id,),
    ).fetchone()
    if session is None:
        raise ValueError(f"unknown session_id: {session_id}")

    rows = []
    if message_seq is not None:
        rows = conn.execute(
            """
            SELECT id, seq, timestamp, text
            FROM messages
            WHERE session_id = ? AND seq = ?
            """,
            (session_id, message_seq),
        ).fetchall()
    if not rows or not any(quote in str(row["text"] or "") for row in rows):
        rows = conn.execute(
            """
            SELECT id, seq, timestamp, text
            FROM messages
            WHERE session_id = ? AND instr(text, ?) > 0
            ORDER BY seq
            """,
            (session_id, quote),
        ).fetchall()
    for row in rows:
        if quote in str(row["text"] or ""):
            return row
    raise ValueError(f"evidence quote not found in session {session_id}")


def import_session_fact_packet(
    conn: sqlite3.Connection,
    path: Path,
    *,
    model: str,
) -> dict[str, Any]:
    if not model.strip():
        raise ValueError("model is required for LLM-derived session facts")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid session fact packet: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise ValueError("session fact packet must contain an items list")

    run_id = _required_text(payload, "run_id")
    prompt_hash = str(payload.get("prompt_hash") or "").strip() or None
    source = str(payload.get("source") or "session_llm_facts").strip()
    claims: list[Claim] = []

    for raw in payload["items"]:
        if not isinstance(raw, dict):
            raise ValueError("every session fact must be an object")
        session_id = _required_text(raw, "session_id")
        title = _required_text(raw, "title")
        body = _required_text(raw, "body")
        quote = _required_text(raw, "quote")
        does_not_prove = _required_text(raw, "does_not_prove")
        theme = _required_text(raw, "kind")
        seq_raw = raw.get("message_seq")
        message_seq = int(seq_raw) if seq_raw is not None else None
        evidence_row = _evidence_message(
            conn,
            session_id=session_id,
            quote=quote,
            message_seq=message_seq,
        )
        session = conn.execute(
            "SELECT repo, harness, started_at FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        assert session is not None
        stable = "\0".join(
            [run_id, session_id, str(evidence_row["id"]), title, quote]
        )
        claim_id = "session_fact:" + hashlib.sha256(stable.encode()).hexdigest()[:24]
        scope_type = "repo" if session["repo"] else "harness"
        scope_id = str(session["repo"] or session["harness"])
        claims.append(
            Claim(
                id=claim_id,
                kind="session_fact",
                subject=theme,
                predicate="observed_in_session",
                value={
                    "title": title,
                    "phrasing": body,
                    "theme": theme,
                },
                scope_type=scope_type,
                scope_id=scope_id,
                derivation="llm_derived",
                status="candidate",
                support_status="ok",
                sample_size=1,
                denominator=1,
                observed_at=str(evidence_row["timestamp"] or session["started_at"] or ""),
                extractor_name="session_fact_packet",
                extractor_version=SESSION_FACT_EXTRACTOR_VERSION,
                confidence_basis={
                    "evidence_verified": True,
                    "model": model,
                    "prompt_hash": prompt_hash,
                    "run_id": run_id,
                    "source": source,
                },
                does_not_prove=does_not_prove,
                evidence=[
                    ClaimEvidence(
                        session_id=session_id,
                        message_id=str(evidence_row["id"]),
                        quote=quote,
                        meta={"message_seq": int(evidence_row["seq"])},
                    )
                ],
            )
        )

    count = upsert_claims(conn, claims)
    return {"run_id": run_id, "model": model, "claims": count}
