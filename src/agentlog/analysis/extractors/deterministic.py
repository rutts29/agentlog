from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from agentlog.analysis.extractors.models import DetClassification, ExtractorMeta
from agentlog.analysis.extractors.taxonomy import (
    EXTRACTOR_NAME_DET,
    EXTRACTOR_VERSION,
)
from agentlog.analysis.extractors.triage import TriageReport, triage_windows
from agentlog.analysis.extractors.window_context import (
    load_window_contexts,
    structural_features,
)


def _run_id(kind: str, started_at: str) -> str:
    raw = f"{kind}:{EXTRACTOR_NAME_DET}:{EXTRACTOR_VERSION}:{started_at}"
    return hashlib.sha1(raw.encode()).hexdigest()[:24]


def run_deterministic(
    conn: sqlite3.Connection,
    *,
    window_ids: list[str] | None = None,
) -> tuple[TriageReport, str]:
    """Classify all (or selected) windows; write det rows; return triage report + run_id."""
    started = datetime.now(timezone.utc).isoformat()
    run_id = _run_id("deterministic", started)
    contexts = load_window_contexts(conn, window_ids=window_ids)
    report = triage_windows(contexts)
    ctx_by_id = {c.window_id: c for c in contexts}

    conn.execute(
        """
        INSERT INTO derivation_runs (
            id, kind, extractor_name, extractor_version, model, prompt_hash,
            started_at, completed_at, status, meta_json
        ) VALUES (?, ?, ?, ?, NULL, NULL, ?, NULL, ?, ?)
        """,
        (
            run_id,
            "deterministic",
            EXTRACTOR_NAME_DET,
            EXTRACTOR_VERSION,
            started,
            "running",
            json.dumps({"window_count": len(contexts)}),
        ),
    )

    meta = ExtractorMeta(
        name=EXTRACTOR_NAME_DET,
        version=EXTRACTOR_VERSION,
        model=None,
        prompt_hash=None,
    )
    now = datetime.now(timezone.utc).isoformat()
    for result in report.results:
        ctx = ctx_by_id[result.window_id]
        features = structural_features(ctx)
        features["matched_rules"] = list(result.matched_rules)
        row_id = hashlib.sha1(f"{run_id}:{result.window_id}".encode()).hexdigest()[:24]
        conn.execute(
            """
            INSERT OR REPLACE INTO window_det_classifications (
                id, window_id, run_id, turn_kinds_json, request_kind, route,
                drop_rules_json, features_json, extractor_name, extractor_version,
                model, prompt_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)
            """,
            (
                row_id,
                result.window_id,
                run_id,
                json.dumps(result.turn_kinds),
                result.request_kind,
                result.route.value,
                json.dumps(result.matched_rules),
                json.dumps(features),
                EXTRACTOR_NAME_DET,
                EXTRACTOR_VERSION,
                now,
            ),
        )
        _route_stub(conn, result, run_id, now)

    completed = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        UPDATE derivation_runs
        SET completed_at = ?, status = ?, meta_json = ?
        WHERE id = ?
        """,
        (
            completed,
            "completed",
            json.dumps(report.to_dict()),
            run_id,
        ),
    )
    conn.commit()
    return report, run_id


def _route_stub(conn: sqlite3.Connection, result, run_id: str, now: str) -> None:
    """Park non-UX traffic in dedicated observation tables (deterministic stub)."""
    route = result.route.value
    if route not in ("auto_review", "worker_task", "skill_compliance"):
        return
    table = {
        "auto_review": "auto_review_observations",
        "worker_task": "worker_task_observations",
        "skill_compliance": "skill_compliance_observations",
    }[route]
    extractor = {
        "auto_review": "auto_review_v1",
        "worker_task": "worker_task_v1",
        "skill_compliance": "skill_compliance_v1",
    }[route]
    row_id = hashlib.sha1(f"{table}:{run_id}:{result.window_id}".encode()).hexdigest()[
        :24
    ]
    payload = {
        "window_id": result.window_id,
        "request_kind": result.request_kind,
        "turn_kinds": result.turn_kinds,
        "route": route,
        "status": "routed_deterministic",
        "note": "Dedicated schema stub; full pipeline deferred.",
    }
    conn.execute(
        f"""
        INSERT OR REPLACE INTO {table} (
            id, window_id, run_id, payload_json,
            extractor_name, extractor_version, model, prompt_hash, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?)
        """,
        (
            row_id,
            result.window_id,
            run_id,
            json.dumps(payload),
            extractor,
            EXTRACTOR_VERSION,
            now,
        ),
    )


def classifications_from_report(report: TriageReport) -> list[DetClassification]:
    meta = ExtractorMeta(name=EXTRACTOR_NAME_DET, version=EXTRACTOR_VERSION)
    return [
        DetClassification(
            window_id=r.window_id,
            turn_kinds=r.turn_kinds,
            request_kind=r.request_kind,
            route=r.route.value,
            drop_rules=r.matched_rules,
            features={},
            extractor=meta,
        )
        for r in report.results
    ]
