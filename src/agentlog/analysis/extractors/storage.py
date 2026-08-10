from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from agentlog.analysis.extractors.models import UxObservation
from agentlog.analysis.extractors.taxonomy import EXTRACTOR_NAME_UX, EXTRACTOR_VERSION
from agentlog.analysis.extractors.ux_extractor import prompt_hash
from agentlog.safety.redaction import REDACTION_VERSION


UX_RUN_KIND = "ux_llm"

# Audit runs exist to measure the extractor, never to feed user-facing metrics.
PURPOSE_AUDIT = "audit"
PURPOSE_FULL_CORPUS = "full_corpus"
PURPOSE_RESTORE = "restore"
PUBLISHABLE_PURPOSES = frozenset({PURPOSE_FULL_CORPUS, PURPOSE_RESTORE})
PUBLISHABLE_STATUSES = frozenset({"completed"})


def _run_columns(conn: sqlite3.Connection) -> set[str]:
    return {str(r[1]) for r in conn.execute("PRAGMA table_info(derivation_runs)")}


def start_ux_run(
    conn: sqlite3.Connection,
    *,
    model: str,
    batch_size: int,
    window_count: int,
    gated: bool,
    purpose: str = PURPOSE_FULL_CORPUS,
) -> str:
    started = datetime.now(timezone.utc).isoformat()
    run_id = hashlib.sha1(
        f"ux:{EXTRACTOR_NAME_UX}:{EXTRACTOR_VERSION}:{model}:{started}".encode()
    ).hexdigest()[:24]
    meta = json.dumps(
        {
            "batch_size": batch_size,
            "window_count": window_count,
            "full_corpus_gated": gated,
            "purpose": purpose,
            "redaction_version": REDACTION_VERSION,
        }
    )
    if "purpose" in _run_columns(conn):
        conn.execute(
            """
            INSERT INTO derivation_runs (
                id, kind, extractor_name, extractor_version, model, prompt_hash,
                started_at, completed_at, status, meta_json, purpose
            ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
            """,
            (
                run_id,
                UX_RUN_KIND,
                EXTRACTOR_NAME_UX,
                EXTRACTOR_VERSION,
                model,
                prompt_hash(),
                started,
                "running",
                meta,
                purpose,
            ),
        )
    else:
        conn.execute(
            """
            INSERT INTO derivation_runs (
                id, kind, extractor_name, extractor_version, model, prompt_hash,
                started_at, completed_at, status, meta_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
            """,
            (
                run_id,
                UX_RUN_KIND,
                EXTRACTOR_NAME_UX,
                EXTRACTOR_VERSION,
                model,
                prompt_hash(),
                started,
                "running",
                meta,
            ),
        )
    conn.commit()
    return run_id


def finish_ux_run(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    status: str,
    meta: dict,
    gate_passed: bool | None = None,
) -> None:
    if gate_passed is not None and "gate_passed" in _run_columns(conn):
        conn.execute(
            """
            UPDATE derivation_runs
            SET completed_at = ?, status = ?, meta_json = ?, gate_passed = ?
            WHERE id = ?
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                status,
                json.dumps(meta),
                int(gate_passed),
                run_id,
            ),
        )
    else:
        conn.execute(
            """
            UPDATE derivation_runs
            SET completed_at = ?, status = ?, meta_json = ?
            WHERE id = ?
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                status,
                json.dumps(meta),
                run_id,
            ),
        )
    conn.commit()


def run_is_publishable(conn: sqlite3.Connection, run_id: str) -> tuple[bool, str]:
    """Whether a run may back a user-facing aggregate, with the blocking reason."""
    cols = _run_columns(conn)
    if "purpose" not in cols:
        return False, "run_contract_missing"
    row = conn.execute(
        "SELECT kind, status, purpose, gate_passed FROM derivation_runs WHERE id = ?",
        (run_id,),
    ).fetchone()
    if row is None:
        return False, "run_not_found"
    if str(row["kind"]) != UX_RUN_KIND:
        return False, "wrong_run_kind"
    if str(row["purpose"]) not in PUBLISHABLE_PURPOSES:
        return False, f"purpose_not_publishable:{row['purpose']}"
    if str(row["status"]) not in PUBLISHABLE_STATUSES:
        return False, f"status_not_publishable:{row['status']}"
    # NULL means the run never faced a real adjudication/audit gate (synthetic
    # fixture packs, restore-from-disk backfills). Those must not publish a lead
    # metric; only an explicit gate_passed=1 authorizes aggregation.
    if row["gate_passed"] is None:
        return False, "gate_not_validated"
    if int(row["gate_passed"]) != 1:
        return False, "gate_failed"
    return True, ""


def publish_ux_run(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    published_by: str,
    note: str = "",
) -> None:
    """Point the single published UX run at `run_id`. Refuses ineligible runs."""
    ok, reason = run_is_publishable(conn, run_id)
    if not ok:
        raise ValueError(f"Refusing to publish UX run {run_id}: {reason}")
    conn.execute(
        """
        INSERT INTO published_derivation_runs
            (kind, run_id, published_at, published_by, note)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(kind) DO UPDATE SET
            run_id = excluded.run_id,
            published_at = excluded.published_at,
            published_by = excluded.published_by,
            note = excluded.note
        """,
        (
            UX_RUN_KIND,
            run_id,
            datetime.now(timezone.utc).isoformat(),
            published_by,
            note,
        ),
    )
    conn.commit()


def published_ux_run_id(conn: sqlite3.Connection) -> str | None:
    """The published UX run, re-validated against the run contract on every read."""
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name = 'published_derivation_runs'"
    ).fetchone():
        return None
    row = conn.execute(
        "SELECT run_id FROM published_derivation_runs WHERE kind = ?",
        (UX_RUN_KIND,),
    ).fetchone()
    if row is None:
        return None
    run_id = str(row["run_id"])
    ok, _reason = run_is_publishable(conn, run_id)
    return run_id if ok else None


def _window_content_hash(conn: sqlite3.Connection, window_id: str) -> str:
    row = conn.execute(
        "SELECT content_hash FROM exchange_windows WHERE id = ?",
        (window_id,),
    ).fetchone()
    if row and row["content_hash"]:
        return str(row["content_hash"])
    return window_id


def write_ux_observations(
    conn: sqlite3.Connection,
    run_id: str,
    observations: list[UxObservation],
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    cols = {
        str(r[1]) for r in conn.execute("PRAGMA table_info(ux_observations)")
    }
    durable = "content_hash" in cols and "link_status" in cols
    for obs in observations:
        row_id = hashlib.sha1(f"{run_id}:{obs.window_id}".encode()).hexdigest()[:24]
        if durable:
            content_hash = _window_content_hash(conn, obs.window_id)
            live = conn.execute(
                "SELECT 1 FROM exchange_windows WHERE id = ?",
                (obs.window_id,),
            ).fetchone()
            link_status = "linked" if live else "orphaned"
            orphaned_at = None if live else now
            conn.execute(
                """
                INSERT OR REPLACE INTO ux_observations (
                    id, window_id, run_id, turn_kinds_json, user_stance, agent_stance,
                    prior_outcome, flags_json, spans_json, confidence_json,
                    abstain_reasons_json, novel_observations_json,
                    extractor_name, extractor_version, model, prompt_hash,
                    batch_size, raw_json, created_at,
                    content_hash, link_status, orphaned_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row_id,
                    obs.window_id,
                    run_id,
                    json.dumps(obs.turn_kind),
                    obs.user_stance,
                    obs.agent_stance,
                    obs.prior_outcome,
                    json.dumps(obs.flags.model_dump()),
                    json.dumps([s.model_dump() for s in obs.spans]),
                    json.dumps(obs.confidence),
                    json.dumps(obs.abstain_reasons),
                    json.dumps(obs.novel_observations),
                    obs.extractor.name,
                    obs.extractor.version,
                    obs.extractor.model or "",
                    obs.extractor.prompt_hash or prompt_hash(),
                    obs.batch_size,
                    json.dumps(obs.to_storage()),
                    now,
                    content_hash,
                    link_status,
                    orphaned_at,
                ),
            )
        else:
            conn.execute(
                """
                INSERT OR REPLACE INTO ux_observations (
                    id, window_id, run_id, turn_kinds_json, user_stance, agent_stance,
                    prior_outcome, flags_json, spans_json, confidence_json,
                    abstain_reasons_json, novel_observations_json,
                    extractor_name, extractor_version, model, prompt_hash,
                    batch_size, raw_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row_id,
                    obs.window_id,
                    run_id,
                    json.dumps(obs.turn_kind),
                    obs.user_stance,
                    obs.agent_stance,
                    obs.prior_outcome,
                    json.dumps(obs.flags.model_dump()),
                    json.dumps([s.model_dump() for s in obs.spans]),
                    json.dumps(obs.confidence),
                    json.dumps(obs.abstain_reasons),
                    json.dumps(obs.novel_observations),
                    obs.extractor.name,
                    obs.extractor.version,
                    obs.extractor.model or "",
                    obs.extractor.prompt_hash or prompt_hash(),
                    obs.batch_size,
                    json.dumps(obs.to_storage()),
                    now,
                ),
            )
    conn.commit()


def load_ux_observations(
    conn: sqlite3.Connection, run_id: str
) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM ux_observations WHERE run_id = ? ORDER BY window_id",
        (run_id,),
    ).fetchall()
    return [dict(r) for r in rows]
