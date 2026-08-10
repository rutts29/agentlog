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

KIND_DETERMINISTIC = "deterministic"


def _run_id(kind: str, started_at: str) -> str:
    raw = f"{kind}:{EXTRACTOR_NAME_DET}:{EXTRACTOR_VERSION}:{started_at}"
    return hashlib.sha1(raw.encode()).hexdigest()[:24]


def classification_row_id(window_id: str) -> str:
    """Stable primary key so re-derive replaces rather than stacks rows."""
    return hashlib.sha1(f"det:{window_id}".encode()).hexdigest()[:24]


def window_input_fingerprint(
    *,
    window_content_hash: str,
    request_content_hash: str,
    authored_by_agent: bool,
    is_tool_plumbing: bool,
) -> str:
    raw = (
        f"{EXTRACTOR_VERSION}|{window_content_hash}|{request_content_hash}|"
        f"{int(authored_by_agent)}|{int(is_tool_plumbing)}"
    )
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def iter_window_input_rows(
    conn: sqlite3.Connection,
) -> list[tuple[str, str]]:
    """Return (window_id, input_fp) for every exchange window."""
    rows = conn.execute(
        """
        SELECT w.id AS window_id,
               COALESCE(w.content_hash, '') AS wch,
               COALESCE(m.content_hash, '') AS mch,
               COALESCE(m.authored_by_agent, 0) AS aba,
               COALESCE(m.is_tool_plumbing, 0) AS plumb
        FROM exchange_windows w
        LEFT JOIN messages m ON m.id = w.request_message_id
        ORDER BY w.id
        """
    ).fetchall()
    out: list[tuple[str, str]] = []
    for row in rows:
        fp = window_input_fingerprint(
            window_content_hash=str(row["wch"] or ""),
            request_content_hash=str(row["mch"] or ""),
            authored_by_agent=bool(row["aba"]),
            is_tool_plumbing=bool(row["plumb"]),
        )
        out.append((str(row["window_id"]), fp))
    return out


def corpus_fingerprint(window_fps: list[tuple[str, str]]) -> str:
    h = hashlib.sha1()
    h.update(EXTRACTOR_VERSION.encode())
    h.update(f"|{len(window_fps)}|".encode())
    for wid, fp in window_fps:
        h.update(f"{wid}:{fp}\n".encode())
    return h.hexdigest()


def existing_classification_fps(conn: sqlite3.Connection) -> dict[str, str]:
    out: dict[str, str] = {}
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name = 'window_det_classifications'"
    ).fetchone():
        return out
    for row in conn.execute(
        "SELECT window_id, features_json, extractor_version "
        "FROM window_det_classifications"
    ):
        wid = str(row["window_id"])
        try:
            features = json.loads(row["features_json"] or "{}")
        except json.JSONDecodeError:
            features = {}
        fp = features.get("input_fp")
        if (
            isinstance(fp, str)
            and fp
            and str(row["extractor_version"] or "") == EXTRACTOR_VERSION
        ):
            out[wid] = fp
        else:
            out[wid] = ""
    return out


def stale_window_ids(
    conn: sqlite3.Connection,
) -> tuple[list[str], list[tuple[str, str]], str]:
    """Windows needing classify, full (id, fp) list, and corpus fingerprint."""
    window_fps = iter_window_input_rows(conn)
    global_fp = corpus_fingerprint(window_fps)
    have = existing_classification_fps(conn)
    stale = [wid for wid, fp in window_fps if have.get(wid) != fp]
    return stale, window_fps, global_fp


def run_deterministic(
    conn: sqlite3.Connection,
    *,
    window_ids: list[str] | None = None,
) -> tuple[TriageReport, str]:
    """Classify all (or selected) windows; write det rows; return triage + run_id."""
    started = datetime.now(timezone.utc).isoformat()
    run_id = _run_id("deterministic", started)
    contexts = load_window_contexts(conn, window_ids=window_ids)
    report = triage_windows(contexts)
    ctx_by_id = {c.window_id: c for c in contexts}

    input_fp_by_id: dict[str, str] = {}
    if contexts:
        ids = [c.window_id for c in contexts]
        placeholders = ",".join("?" * len(ids))
        for row in conn.execute(
            f"""
            SELECT w.id AS window_id,
                   COALESCE(w.content_hash, '') AS wch,
                   COALESCE(m.content_hash, '') AS mch,
                   COALESCE(m.authored_by_agent, 0) AS aba,
                   COALESCE(m.is_tool_plumbing, 0) AS plumb
            FROM exchange_windows w
            LEFT JOIN messages m ON m.id = w.request_message_id
            WHERE w.id IN ({placeholders})
            """,
            ids,
        ):
            input_fp_by_id[str(row["window_id"])] = window_input_fingerprint(
                window_content_hash=str(row["wch"] or ""),
                request_content_hash=str(row["mch"] or ""),
                authored_by_agent=bool(row["aba"]),
                is_tool_plumbing=bool(row["plumb"]),
            )

    conn.execute(
        """
        INSERT INTO derivation_runs (
            id, kind, extractor_name, extractor_version, model, prompt_hash,
            started_at, completed_at, status, meta_json
        ) VALUES (?, ?, ?, ?, NULL, NULL, ?, NULL, ?, ?)
        """,
        (
            run_id,
            KIND_DETERMINISTIC,
            EXTRACTOR_NAME_DET,
            EXTRACTOR_VERSION,
            started,
            "running",
            json.dumps({"window_count": len(contexts)}),
        ),
    )

    now = datetime.now(timezone.utc).isoformat()
    for result in report.results:
        ctx = ctx_by_id[result.window_id]
        features = structural_features(ctx)
        features["matched_rules"] = list(result.matched_rules)
        features["input_fp"] = input_fp_by_id.get(result.window_id, "")
        row_id = classification_row_id(result.window_id)
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
    # Stable id so re-derive replaces the stub instead of stacking run rows.
    row_id = hashlib.sha1(f"{table}:det:{result.window_id}".encode()).hexdigest()[
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
    # Drop prior deterministic stubs for this window (unique is window_id+run_id).
    conn.execute(
        f"""
        DELETE FROM {table}
        WHERE window_id = ?
          AND json_extract(payload_json, '$.status') = 'routed_deterministic'
        """,
        (result.window_id,),
    )
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
