from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from agentlog.analysis.extractors.models import UxObservation
from agentlog.analysis.extractors.taxonomy import EXTRACTOR_NAME_UX, EXTRACTOR_VERSION
from agentlog.analysis.extractors.ux_extractor import prompt_hash


def start_ux_run(
    conn: sqlite3.Connection,
    *,
    model: str,
    batch_size: int,
    window_count: int,
    gated: bool,
) -> str:
    started = datetime.now(timezone.utc).isoformat()
    run_id = hashlib.sha1(
        f"ux:{EXTRACTOR_NAME_UX}:{EXTRACTOR_VERSION}:{model}:{started}".encode()
    ).hexdigest()[:24]
    conn.execute(
        """
        INSERT INTO derivation_runs (
            id, kind, extractor_name, extractor_version, model, prompt_hash,
            started_at, completed_at, status, meta_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
        """,
        (
            run_id,
            "ux_llm",
            EXTRACTOR_NAME_UX,
            EXTRACTOR_VERSION,
            model,
            prompt_hash(),
            started,
            "running",
            json.dumps(
                {
                    "batch_size": batch_size,
                    "window_count": window_count,
                    "full_corpus_gated": gated,
                }
            ),
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
) -> None:
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


def write_ux_observations(
    conn: sqlite3.Connection,
    run_id: str,
    observations: list[UxObservation],
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    for obs in observations:
        row_id = hashlib.sha1(f"{run_id}:{obs.window_id}".encode()).hexdigest()[:24]
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
