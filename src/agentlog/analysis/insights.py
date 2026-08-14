from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from agentlog.analysis.claims.models import Claim, ClaimEvidence, Proposal
from agentlog.analysis.claims.proposals import _sha1_text, unified_diff
from agentlog.analysis.claims.store import upsert_claims, upsert_proposals
from agentlog.safety.redaction import redact_text
from agentlog.source_reader import CachedSourceTranscriptReader

SESSION_FACT_EXTRACTOR_VERSION = "session_fact_v1"


def _owner_result_hash(payload: dict[str, Any]) -> str:
    material = {
        "source": payload.get("source"),
        "prompt_hash": payload.get("prompt_hash"),
        "batches": sorted(
            [
                {"id": item.get("id"), "content_hash": item.get("content_hash")}
                for item in payload.get("owner_insight_batches") or []
                if isinstance(item, dict)
            ],
            key=lambda item: (str(item["id"]), str(item["content_hash"])),
        ),
        "items": sorted(payload.get("items") or [], key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False)),
        "proposals": sorted(payload.get("proposals") or [], key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False)),
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _proposal_target(
    conn: sqlite3.Connection, payload: dict[str, Any], target_id: str, target_kind: str
) -> tuple[Path, str, str, str | None]:
    if not target_id:
        raise ValueError("owner proposal is missing target_id")
    exported = {
        (str(item.get("id") or ""), str(item.get("base_content_hash") or ""))
        for item in payload.get("owner_insight_targets") or []
        if isinstance(item, dict)
    }
    row = conn.execute(
        "SELECT path,target_kind,scope_type,scope_id,base_content_hash FROM owner_insight_targets WHERE id=?",
        (target_id,),
    ).fetchone()
    if row is None or (target_id, str(row["base_content_hash"])) not in exported:
        raise ValueError("owner proposal target was not exported for this review")
    if str(row["target_kind"]) != target_kind:
        raise ValueError("owner proposal target kind differs from exported target")
    try:
        path = Path(str(row["path"])).resolve(strict=True)
    except OSError as exc:
        raise ValueError("owner proposal target is no longer available") from exc
    return path, str(row["base_content_hash"]), str(row["scope_type"]), row["scope_id"]


def _required_text(item: dict[str, Any], key: str) -> str:
    value = str(item.get(key) or "").strip()
    if not value:
        raise ValueError(f"session fact is missing {key}")
    return value


def _quote_matches(text: str, quote: str) -> bool:
    return quote in text or quote in redact_text(text)


def _evidence_message(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    quote: str,
    message_seq: int | None,
    source_reader: CachedSourceTranscriptReader | None = None,
) -> sqlite3.Row | dict[str, Any]:
    session = conn.execute(
        "SELECT repo, harness, started_at, transcript_storage "
        "FROM sessions WHERE id = ?",
        (session_id,),
    ).fetchone()
    if session is None:
        raise ValueError(f"unknown session_id: {session_id}")

    if session["transcript_storage"] == "source_backed":
        source = (source_reader or CachedSourceTranscriptReader())(
            conn, session_id
        )
        if not source.ready:
            raise ValueError(
                f"canonical evidence unavailable for session {session_id}: "
                f"{source.warning or source.status}"
            )
        metadata = {
            str(row["id"]): row
            for row in conn.execute(
                "SELECT id, seq, role, timestamp, content_hash FROM messages "
                "WHERE session_id = ?",
                (session_id,),
            )
        }
        candidates = source.messages
        if message_seq is not None:
            candidates = [
                row for row in candidates if int(row["seq"]) == message_seq
            ]
        for row in candidates:
            stored = metadata.get(str(row["id"]))
            if (
                stored is not None
                and int(stored["seq"]) == int(row["seq"])
                and str(stored["content_hash"]) == str(row["content_hash"])
                and _quote_matches(str(row["text"] or ""), quote)
            ):
                return {
                    "id": row["id"],
                    "seq": row["seq"],
                    "role": stored["role"],
                    "timestamp": row["timestamp"] or stored["timestamp"],
                    "text": row["text"],
                    "content_hash": row["content_hash"],
                    "source_identity": source.source_identity,
                    "source_hash": source.source_hash,
                }
        raise ValueError(f"evidence quote not found in session {session_id}")

    rows = []
    if message_seq is not None:
        rows = conn.execute(
            """
            SELECT id, seq, role, timestamp, text, content_hash
            FROM messages
            WHERE session_id = ? AND seq = ?
            """,
            (session_id, message_seq),
        ).fetchall()
    if message_seq is None:
        rows = conn.execute(
            """
            SELECT id, seq, role, timestamp, text, content_hash
            FROM messages WHERE session_id = ?
            ORDER BY seq
            """,
            (session_id,),
        ).fetchall()
    for row in rows:
        if _quote_matches(str(row["text"] or ""), quote):
            return row
    raise ValueError(f"evidence quote not found in session {session_id}")


def import_session_fact_packet(
    conn: sqlite3.Connection,
    path: Path,
    *,
    model: str,
    status: str = "candidate",
) -> dict[str, Any]:
    if not model.strip():
        raise ValueError("model is required for LLM-derived session facts")
    if status not in {"candidate", "approved"}:
        raise ValueError("status must be candidate or approved")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid session fact packet: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise ValueError("session fact packet must contain an items list")

    run_id = _required_text(payload, "run_id")
    prompt_hash = str(payload.get("prompt_hash") or "").strip() or None
    source = str(payload.get("source") or "session_llm_facts").strip()
    raw_proposals = payload.get("proposals") or []
    if not isinstance(raw_proposals, list):
        raise ValueError("proposals must be a list")
    from agentlog.analysis.owner_notes import validate_owner_items, validate_owner_proposals

    owner_items = validate_owner_items(payload["items"]) if source == "owner_notes" else payload["items"]
    owner_proposals = validate_owner_proposals(raw_proposals) if raw_proposals else []
    claims: list[Claim] = []
    source_reader = CachedSourceTranscriptReader()
    batches = payload.get("owner_insight_batches") or []
    if not isinstance(batches, list):
        raise ValueError("owner_insight_batches must be a list")
    if source == "owner_notes" and not batches:
        raise ValueError("owner notes require prepared owner insight batches")
    batch_evidence: dict[tuple[str, str], dict[str, Any]] = {}
    if batches:
        from agentlog.analysis.owner_notes import batch_message_evidence

        batch_evidence = batch_message_evidence(conn, batches)
        if not batch_evidence:
            raise ValueError("owner insight packet references no prepared evidence")
        result_hash = _owner_result_hash(payload)
        from agentlog.analysis.owner_notes import validate_owner_batches

        validate_owner_batches(conn, batches, result_hash=result_hash)

    insight_claim_ids: dict[str, str] = {}
    for raw in owner_items:
        if not isinstance(raw, dict):
            raise ValueError("every session fact must be an object")
        title = _required_text(raw, "title")
        body = _required_text(raw, "body")
        does_not_prove = _required_text(raw, "does_not_prove")
        theme = _required_text(raw, "kind")
        insight_key = str(raw.get("insight_key") or "").strip()
        if source == "owner_notes" and not insight_key:
            raise ValueError("owner insight is missing insight_key")
        evidence_inputs = raw.get("evidence") or [{
            "session_id": _required_text(raw, "session_id"),
            "message_seq": raw.get("message_seq"),
            "quote": _required_text(raw, "quote"),
        }]
        verified_evidence: list[tuple[str, sqlite3.Row | dict[str, Any], str]] = []
        for evidence in evidence_inputs:
            if not isinstance(evidence, dict):
                raise ValueError("session fact evidence must be an object")
            evidence_session_id = _required_text(evidence, "session_id")
            evidence_quote = _required_text(evidence, "quote")
            seq_raw = evidence.get("message_seq")
            message_seq = int(seq_raw) if seq_raw is not None else None
            evidence_row = _evidence_message(
                conn,
                session_id=evidence_session_id,
                quote=evidence_quote,
                message_seq=message_seq,
                source_reader=source_reader,
            )
            if batches:
                expected = batch_evidence.get((evidence_session_id, str(evidence_row["id"])))
                if expected is None:
                    raise ValueError("session fact evidence was not in its owner insight batch")
                if (
                    str(evidence_row["content_hash"] or "") != expected["content_hash"]
                    or int(evidence_row["seq"]) != expected["seq"]
                    or str(evidence_row["role"]) != expected["role"]
                ):
                    raise ValueError("owner insight evidence content changed before import")
                provenance = expected.get("source_snapshot", {}).get("source_provenance", {})
                if "source_identity" in provenance and str(evidence_row.get("source_identity") or "") != str(provenance["source_identity"]):
                    raise ValueError("owner insight source provenance changed before import")
            verified_evidence.append((evidence_session_id, evidence_row, evidence_quote))
        evidence_sessions = {session_id for session_id, _, _ in verified_evidence}
        session_rows = {
            session_id: conn.execute(
                "SELECT repo, harness, started_at FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            for session_id in evidence_sessions
        }
        if any(row is None for row in session_rows.values()):
            raise ValueError("owner insight evidence session disappeared")
        primary_session_id, primary_evidence, primary_quote = verified_evidence[0]
        primary_session = session_rows[primary_session_id]
        assert primary_session is not None
        repos = {str(row["repo"] or "") for row in session_rows.values() if row is not None}
        harnesses = {str(row["harness"] or "") for row in session_rows.values() if row is not None}
        if len(repos) == 1 and next(iter(repos)):
            scope_type, scope_id = "repo", next(iter(repos))
        elif len(harnesses) == 1:
            scope_type, scope_id = "harness", next(iter(harnesses))
        else:
            scope_type, scope_id = "global", None
        stable = "\0".join([scope_type, scope_id or "", insight_key or title])
        claim_id = "session_fact:" + hashlib.sha256(stable.encode()).hexdigest()[:24]
        if insight_key:
            if insight_key in insight_claim_ids:
                raise ValueError(f"duplicate owner insight_key: {insight_key}")
            insight_claim_ids[insight_key] = claim_id
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
                status=status,
                support_status="ok",
                sample_size=len(evidence_sessions),
                denominator=len(evidence_sessions),
                observed_at=str(primary_evidence["timestamp"] or primary_session["started_at"] or ""),
                extractor_name="session_fact_packet",
                extractor_version=SESSION_FACT_EXTRACTOR_VERSION,
                confidence_basis={
                    "evidence_verified": True,
                    "model": model,
                    "prompt_hash": prompt_hash,
                    "run_id": run_id,
                    "source": source,
                    "owner_result_hash": result_hash if batches else None,
                    "owner_batch_hashes": [str(entry.get("content_hash") or "") for entry in batches],
                },
                does_not_prove=does_not_prove,
                supersedes_id=str(raw.get("supersedes_id") or "").strip() or None,
                evidence=[
                    ClaimEvidence(
                        session_id=evidence_session_id,
                        message_id=str(evidence_row["id"]),
                        quote=evidence_quote,
                        meta={
                            "message_seq": int(evidence_row["seq"]),
                            "content_hash": str(evidence_row["content_hash"] or ""),
                            "source_snapshot": batch_evidence.get((evidence_session_id, str(evidence_row["id"])), {}).get("source_snapshot", {}),
                        },
                    )
                    for evidence_session_id, evidence_row, evidence_quote in verified_evidence
                ],
            )
        )

    proposals: list[Proposal] = []
    for raw in owner_proposals:
        if not batches:
            raise ValueError("owner proposals require prepared owner insight batches")
        claim_ids: list[str] = []
        for key in raw["supporting_insight_keys"]:
            claim_id = insight_claim_ids.get(key)
            if claim_id is None:
                raise ValueError(f"owner proposal references unknown insight_key: {key}")
            claim_ids.append(claim_id)
        evidence_meta: list[dict[str, Any]] = []
        for evidence in raw["evidence"]:
            row = _evidence_message(
                conn,
                session_id=evidence["session_id"],
                quote=evidence["quote"],
                message_seq=evidence["message_seq"],
                source_reader=source_reader,
            )
            expected = batch_evidence.get((evidence["session_id"], str(row["id"])))
            if expected is None:
                raise ValueError("owner proposal evidence was not in its owner insight batch")
            if str(row["content_hash"] or "") != expected["content_hash"]:
                raise ValueError("owner proposal evidence content changed before import")
            evidence_meta.append(
                {
                    "session_id": evidence["session_id"],
                    "message_id": str(row["id"]),
                    "message_seq": int(row["seq"]),
                    "quote": evidence["quote"],
                    "content_hash": expected["content_hash"],
                    "source_snapshot": expected.get("source_snapshot", {}),
                }
            )
        target, expected_target_hash, scope_type, scope_id = _proposal_target(
            conn, payload, raw["target_id"], raw["target_kind"]
        )
        current = target.read_text(encoding="utf-8", errors="replace") if target.is_file() else ""
        if _sha1_text(current) != expected_target_hash:
            raise ValueError("owner proposal target content changed before import")
        proposed = raw.get("proposed_content")
        new = "" if raw["action"] in {"remove", "archive_skill"} else str(proposed)
        proposal_stable = "\0".join([str(target), raw["target_kind"], raw["proposal_key"]])
        proposal_id = "owner_proposal:" + hashlib.sha256(proposal_stable.encode()).hexdigest()[:24]
        proposals.append(
            Proposal(
                id=proposal_id,
                title=raw["title"],
                action=raw["action"],
                status="pending",
                target_path=str(target),
                target_kind=raw["target_kind"],
                scope_type=scope_type,
                scope_id=scope_id,
                base_content_hash=expected_target_hash,
                unified_diff=unified_diff(path=str(target), old=current, new=new),
                proposed_content=proposed,
                rationale=raw["rationale"],
                derivation_summary="Manual owner Insight review; human approval required.",
                does_not_prove=raw["does_not_prove"],
                sample_size=len(evidence_meta),
                claim_ids=claim_ids,
                provenance={
                    "provider": "owner_notes",
                    "human_review_required": True,
                    "proposal_key": raw["proposal_key"],
                    "evidence": evidence_meta,
                    "owner_result_hash": result_hash,
                },
                run_id=run_id,
                model=model,
                prompt_hash=prompt_hash,
                evidence_pack_hash=result_hash,
            )
        )

    if batches and not source_reader.verify_current():
        raise ValueError("owner insight source changed during import")
    conn.execute("SAVEPOINT owner_insight_import")
    try:
        count = upsert_claims(conn, claims)
        proposal_count = upsert_proposals(conn, proposals)
        imported_batches: list[str] = []
        if batches:
            from agentlog.analysis.owner_notes import mark_owner_batches_imported

            imported_batches = mark_owner_batches_imported(
                conn, batches, result_hash=result_hash
            )
    except BaseException:
        conn.execute("ROLLBACK TO owner_insight_import")
        conn.execute("RELEASE owner_insight_import")
        raise
    conn.execute("RELEASE owner_insight_import")
    return {"run_id": run_id, "model": model, "claims": count, "proposals": proposal_count, "batches": imported_batches}
