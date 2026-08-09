from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentlog.analysis.extractors.audit import (
    AuditGateResult,
    compare_batch_vs_single,
    emit_audit_pack,
    evaluate_gate,
    load_gold,
    score_predictions,
)
from agentlog.analysis.extractors.deterministic import run_deterministic
from agentlog.analysis.extractors.llm_client import ChatClient
from agentlog.analysis.extractors.storage import (
    finish_ux_run,
    start_ux_run,
    write_ux_observations,
)
from agentlog.analysis.extractors.taxonomy import DEFAULT_BATCH_SIZE, DEFAULT_UX_MODEL, Route
from agentlog.analysis.extractors.triage import TriageReport
from agentlog.analysis.extractors.ux_extractor import UxExtractor
from agentlog.analysis.extractors.window_context import load_window_contexts


@dataclass
class ExtractionPhaseResult:
    det_run_id: str
    triage: TriageReport
    audit_path: Path | None = None
    audit_run_id: str | None = None
    gate: AuditGateResult | None = None
    full_run_authorized: bool = False
    notes: list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "det_run_id": self.det_run_id,
            "triage": self.triage.to_dict(),
            "audit_path": str(self.audit_path) if self.audit_path else None,
            "audit_run_id": self.audit_run_id,
            "gate": self.gate.to_dict() if self.gate else None,
            "full_run_authorized": self.full_run_authorized,
            "notes": self.notes or [],
        }


def run_deterministic_phase(conn: sqlite3.Connection) -> tuple[TriageReport, str]:
    return run_deterministic(conn)


def build_audit_pack(
    conn: sqlite3.Connection,
    path: Path,
    *,
    n: int = 100,
    seed: int = 42,
    ux_only: bool = True,
) -> list[str]:
    contexts = load_window_contexts(conn)
    if ux_only:
        from agentlog.analysis.extractors.triage import triage_window

        contexts = [c for c in contexts if triage_window(c).route == Route.UX]
    sample = emit_audit_pack(contexts, path, n=n, seed=seed)
    return [c.window_id for c in sample]


def run_audit_phase(
    conn: sqlite3.Connection,
    *,
    audit_pack: Path,
    gold_path: Path | None,
    client: ChatClient | None = None,
    model: str = DEFAULT_UX_MODEL,
    compare_batch_size: int = 8,
    max_windows: int | None = None,
) -> tuple[AuditGateResult, str, dict[str, Any]]:
    """
    Run UX extractor on the audit pack, score against gold if present,
    compare batched vs single, and evaluate the gate.

    Full-corpus LLM extraction is NOT performed here.
    """
    rows: list[dict] = []
    with audit_pack.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    window_ids = [str(r["window_id"]) for r in rows]
    if max_windows is not None:
        window_ids = window_ids[:max_windows]
    contexts = load_window_contexts(conn, window_ids=window_ids)
    # Preserve pack order.
    by_id = {c.window_id: c for c in contexts}
    ordered = [by_id[w] for w in window_ids if w in by_id]

    extractor = UxExtractor(client=client, model=model, batch_size=1)
    run_id = start_ux_run(
        conn,
        model=model,
        batch_size=1,
        window_count=len(ordered),
        gated=True,
    )
    observations = extractor.extract_many(ordered, batch_size=1)
    write_ux_observations(conn, run_id, observations)

    disagreement_rate, diffs = compare_batch_vs_single(
        extractor, ordered, batch_size=compare_batch_size
    )

    gold: dict[str, dict] = {}
    if gold_path is not None and gold_path.exists():
        gold = load_gold(gold_path)
    scores = score_predictions(observations, gold) if gold else {}
    gate = evaluate_gate(scores, batch_disagreement_rate=disagreement_rate)

    meta = {
        "audit_pack": str(audit_pack),
        "gold_path": str(gold_path) if gold_path else None,
        "gold_labeled": len(gold),
        "scored_windows": len(ordered),
        "gate": gate.to_dict(),
        "batch_diffs_sample": diffs[:20],
    }
    finish_ux_run(
        conn,
        run_id,
        status="completed_audit" if gate.passed else "audit_gate_failed",
        meta=meta,
    )
    return gate, run_id, meta


def authorize_full_ux_run(gate: AuditGateResult, *, owner_authorized: bool) -> bool:
    """Full corpus LLM run requires audit gate pass AND explicit owner authorization."""
    return bool(gate.passed and owner_authorized)


def run_full_ux_extract(
    conn: sqlite3.Connection,
    *,
    client: ChatClient | None = None,
    model: str = DEFAULT_UX_MODEL,
    batch_size: int = DEFAULT_BATCH_SIZE,
    owner_authorized: bool = False,
    gate: AuditGateResult | None = None,
) -> str:
    if gate is None or not authorize_full_ux_run(gate, owner_authorized=owner_authorized):
        raise RuntimeError(
            "Full UX extraction blocked: audit gate must pass and owner must authorize"
        )
    contexts = load_window_contexts(conn)
    from agentlog.analysis.extractors.triage import triage_window

    ux_contexts = [c for c in contexts if triage_window(c).route == Route.UX]
    extractor = UxExtractor(client=client, model=model, batch_size=batch_size)
    run_id = start_ux_run(
        conn,
        model=model,
        batch_size=batch_size,
        window_count=len(ux_contexts),
        gated=False,
    )
    try:
        obs = extractor.extract_many(ux_contexts, batch_size=batch_size)
        write_ux_observations(conn, run_id, obs)
        finish_ux_run(
            conn,
            run_id,
            status="completed",
            meta={"window_count": len(obs), "batch_size": batch_size},
        )
    except Exception as exc:
        finish_ux_run(conn, run_id, status="failed", meta={"error": str(exc)})
        raise
    return run_id


def ux_eligible_from_det(conn: sqlite3.Connection, det_run_id: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT window_id FROM window_det_classifications
        WHERE run_id = ? AND route = ?
        ORDER BY window_id
        """,
        (det_run_id, Route.UX.value),
    ).fetchall()
    return [r["window_id"] for r in rows]
