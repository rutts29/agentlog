from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

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
from agentlog.source_reader import CachedSourceTranscriptReader

KIND_DETERMINISTIC = "deterministic"


class DeterministicInputChanged(RuntimeError):
    pass


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


def _hash_record(
    digest: Any,
    kind: str,
    values: tuple[object, ...],
) -> None:
    encoded = json.dumps(
        [kind, *values],
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    digest.update(len(encoded).to_bytes(8, "big"))
    digest.update(encoded)


def iter_window_input_rows(
    conn: sqlite3.Connection,
) -> list[tuple[str, str]]:
    """Return (window_id, input_fp) for every exchange window."""
    windows = conn.execute(
        """
        SELECT w.id, w.session_id, w.request_message_id,
               w.response_message_id, w.input_hash, w.content_hash,
               s.harness, s.model
        FROM exchange_windows w
        JOIN sessions s ON s.id = w.session_id
        ORDER BY w.id
        """
    ).fetchall()
    session_digests: dict[str, Any] = {}
    for row in windows:
        session_id = str(row["session_id"])
        if session_id not in session_digests:
            digest = hashlib.sha1()
            _hash_record(
                digest,
                "session",
                (session_id, row["harness"], row["model"]),
            )
            session_digests[session_id] = digest

    if session_digests:
        for row in conn.execute(
            """
            SELECT m.id, m.session_id, m.seq, m.role,
                   m.content_hash, m.model, m.is_tool_plumbing,
                   m.authored_by_agent
            FROM messages m
            JOIN (SELECT DISTINCT session_id FROM exchange_windows) w
              ON w.session_id = m.session_id
            ORDER BY m.session_id, m.seq, m.id
            """
        ):
            digest = session_digests.get(str(row["session_id"]))
            if digest is None:
                continue
            _hash_record(
                digest,
                "message",
                (
                    row["id"],
                    row["seq"],
                    row["role"],
                    row["content_hash"],
                    row["model"],
                    row["is_tool_plumbing"],
                    row["authored_by_agent"],
                ),
            )
        for row in conn.execute(
            """
            SELECT t.id, t.session_id, t.message_id, t.seq, t.tool_name,
                   t.action, t.success
            FROM tool_events t
            JOIN (SELECT DISTINCT session_id FROM exchange_windows) w
              ON w.session_id = t.session_id
            ORDER BY t.session_id, t.seq, t.id
            """
        ):
            digest = session_digests.get(str(row["session_id"]))
            if digest is None:
                continue
            _hash_record(
                digest,
                "tool",
                (
                    row["id"],
                    row["message_id"],
                    row["seq"],
                    row["tool_name"],
                    row["action"],
                    row["success"],
                ),
            )
        for row in conn.execute(
            """
            SELECT e.id, e.session_id, e.message_id, e.skill_name,
                   e.exposure_type
            FROM skill_exposures e
            JOIN (SELECT DISTINCT session_id FROM exchange_windows) w
              ON w.session_id = e.session_id
            ORDER BY e.session_id, e.skill_name, e.exposure_type, e.id
            """
        ):
            digest = session_digests.get(str(row["session_id"]))
            if digest is None:
                continue
            _hash_record(
                digest,
                "skill",
                (
                    row["id"],
                    row["message_id"],
                    row["skill_name"],
                    row["exposure_type"],
                ),
            )

    out: list[tuple[str, str]] = []
    for row in windows:
        digest = session_digests[str(row["session_id"])].copy()
        _hash_record(
            digest,
            "window",
            (
                row["id"],
                row["request_message_id"],
                row["response_message_id"],
                row["input_hash"],
                row["content_hash"],
                EXTRACTOR_VERSION,
            ),
        )
        out.append((str(row["id"]), digest.hexdigest()[:16]))
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
    expected_window_fps: list[tuple[str, str]] | None = None,
) -> tuple[TriageReport, str]:
    """Classify all (or selected) windows; write det rows; return triage + run_id."""
    started = datetime.now(timezone.utc).isoformat()
    run_id = _run_id("deterministic", started)
    baseline_data_version = int(conn.execute("PRAGMA data_version").fetchone()[0])
    source_reader = CachedSourceTranscriptReader()
    contexts = load_window_contexts(
        conn, window_ids=window_ids, source_reader=source_reader
    )
    prepared_window_fps = iter_window_input_rows(conn)
    if (
        expected_window_fps is not None
        and prepared_window_fps != expected_window_fps
    ):
        raise DeterministicInputChanged(
            "deterministic input changed before classification"
        )
    report = triage_windows(contexts)
    ctx_by_id = {c.window_id: c for c in contexts}

    conn.execute("BEGIN IMMEDIATE")
    if not source_reader.verify_current():
        conn.rollback()
        raise DeterministicInputChanged(
            "canonical source changed during classification"
        )
    current_data_version = int(conn.execute("PRAGMA data_version").fetchone()[0])
    if current_data_version != baseline_data_version:
        conn.rollback()
        raise DeterministicInputChanged(
            "deterministic input changed during classification"
        )
    input_fp_by_id = dict(prepared_window_fps)

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
