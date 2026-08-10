"""Bring deterministic derived layers up to date with current windows.

Cheap when nothing changed: corpus fingerprint vs derive_watermarks.
Safe with concurrent writers: short transactions + busy timeout.
Does not touch durable LLM labels (ux_observations / adjudications).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from agentlog.analysis.extractors.deterministic import (
    KIND_DETERMINISTIC,
    corpus_fingerprint,
    iter_window_input_rows,
    run_deterministic,
    stale_window_ids,
)
from agentlog.analysis.extractors.taxonomy import EXTRACTOR_VERSION
from agentlog.analysis.skills import index_skills, index_t3_visibility

DERIVE_KIND_DETERMINISTIC = KIND_DETERMINISTIC
DERIVE_KIND_SKILLS = "skills_index"


@dataclass
class DeriveResult:
    skipped: bool
    windows_total: int = 0
    windows_classified: int = 0
    windows_updated: int = 0
    run_id: str | None = None
    input_fingerprint: str = ""
    request_kind_counts: dict[str, int] = field(default_factory=dict)
    skills: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "skipped": self.skipped,
            "windows_total": self.windows_total,
            "windows_classified": self.windows_classified,
            "windows_updated": self.windows_updated,
            "run_id": self.run_id,
            "input_fingerprint": self.input_fingerprint,
            "request_kind_counts": dict(self.request_kind_counts),
            "skills": self.skills,
            "notes": list(self.notes),
        }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_busy_timeout(conn: sqlite3.Connection, ms: int = 30_000) -> None:
    conn.execute(f"PRAGMA busy_timeout = {int(ms)}")


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _classified_count(conn: sqlite3.Connection) -> int:
    if not _table_exists(conn, "window_det_classifications"):
        return 0
    row = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM window_det_classifications d
        JOIN exchange_windows w ON w.id = d.window_id
        """
    ).fetchone()
    return int(row["c"]) if row else 0


def _windows_total(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS c FROM exchange_windows").fetchone()
    return int(row["c"]) if row else 0


def read_watermark(
    conn: sqlite3.Connection, kind: str = DERIVE_KIND_DETERMINISTIC
) -> dict[str, Any] | None:
    if not _table_exists(conn, "derive_watermarks"):
        return None
    row = conn.execute(
        "SELECT * FROM derive_watermarks WHERE kind = ?", (kind,)
    ).fetchone()
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


def write_watermark(
    conn: sqlite3.Connection,
    *,
    kind: str,
    input_fingerprint: str,
    windows_total: int,
    windows_classified: int,
    last_run_id: str | None,
    meta: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO derive_watermarks (
            kind, input_fingerprint, extractor_version,
            windows_total, windows_classified, last_run_id,
            updated_at, meta_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(kind) DO UPDATE SET
            input_fingerprint = excluded.input_fingerprint,
            extractor_version = excluded.extractor_version,
            windows_total = excluded.windows_total,
            windows_classified = excluded.windows_classified,
            last_run_id = excluded.last_run_id,
            updated_at = excluded.updated_at,
            meta_json = excluded.meta_json
        """,
        (
            kind,
            input_fingerprint,
            EXTRACTOR_VERSION,
            windows_total,
            windows_classified,
            last_run_id,
            _utc_now(),
            json.dumps(meta or {}),
        ),
    )


def derived_freshness(conn: sqlite3.Connection) -> dict[str, Any]:
    """Snapshot for /api/health — explain empty Request Kinds panels."""
    windows_total = _windows_total(conn)
    windows_classified = _classified_count(conn)
    wm = read_watermark(conn, DERIVE_KIND_DETERMINISTIC)
    window_fps = iter_window_input_rows(conn)
    current_fp = corpus_fingerprint(window_fps)
    stored_fp = str(wm["input_fingerprint"]) if wm else None
    fingerprint_match = bool(stored_fp and stored_fp == current_fp)
    coverage_complete = windows_total == 0 or windows_classified >= windows_total
    stale = not (fingerprint_match and coverage_complete)
    return {
        "windows_total": windows_total,
        "windows_classified": windows_classified,
        "coverage": (
            (windows_classified / windows_total) if windows_total else 1.0
        ),
        "stale": stale,
        "last_derive_at": wm["updated_at"] if wm else None,
        "last_run_id": wm["last_run_id"] if wm else None,
        "extractor_version": EXTRACTOR_VERSION,
        "input_fingerprint": current_fp,
        "stored_fingerprint": stored_fp,
        "fingerprint_match": fingerprint_match,
    }


def run_derive(
    conn: sqlite3.Connection,
    *,
    force: bool = False,
    index_skill_inventory: bool = True,
) -> DeriveResult:
    """Idempotent derive pass for deterministic window classifications.

    Skips work when the corpus fingerprint and coverage match the watermark.
    Optionally refreshes the on-disk skills inventory (also content-hash gated).
    """
    _ensure_busy_timeout(conn)
    stale, window_fps, global_fp = stale_window_ids(conn)
    windows_total = len(window_fps)
    classified_before = _classified_count(conn)
    wm = read_watermark(conn, DERIVE_KIND_DETERMINISTIC)

    skills_meta: dict[str, Any] = {}
    if index_skill_inventory and _table_exists(conn, "skills"):
        stats = index_skills(conn)
        skills_meta = stats.to_dict() if hasattr(stats, "to_dict") else {}
        skills_meta["t3_visibility"] = index_t3_visibility(conn).to_dict()
        conn.commit()

    coverage_ok = classified_before >= windows_total
    fingerprint_ok = bool(
        wm
        and wm.get("input_fingerprint") == global_fp
        and str(wm.get("extractor_version") or "") == EXTRACTOR_VERSION
    )
    if not force and fingerprint_ok and coverage_ok and not stale:
        return DeriveResult(
            skipped=True,
            windows_total=windows_total,
            windows_classified=classified_before,
            windows_updated=0,
            run_id=wm.get("last_run_id") if wm else None,
            input_fingerprint=global_fp,
            skills=skills_meta,
            notes=["fingerprint unchanged; no classification work"],
        )

    # Stale already includes missing windows (no matching input_fp).
    # Only --force reclassifies the full corpus.
    targets: list[str] | None = None if force else stale

    if targets is not None and len(targets) == 0 and coverage_ok:
        write_watermark(
            conn,
            kind=DERIVE_KIND_DETERMINISTIC,
            input_fingerprint=global_fp,
            windows_total=windows_total,
            windows_classified=classified_before,
            last_run_id=wm.get("last_run_id") if wm else None,
            meta={"skipped": True, "skills": skills_meta, "reason": "coverage_ok"},
        )
        conn.commit()
        return DeriveResult(
            skipped=True,
            windows_total=windows_total,
            windows_classified=classified_before,
            windows_updated=0,
            run_id=wm.get("last_run_id") if wm else None,
            input_fingerprint=global_fp,
            skills=skills_meta,
            notes=["no stale windows; watermark refreshed"],
        )

    report, run_id = run_deterministic(conn, window_ids=targets)
    classified_after = _classified_count(conn)
    write_watermark(
        conn,
        kind=DERIVE_KIND_DETERMINISTIC,
        input_fingerprint=global_fp,
        windows_total=windows_total,
        windows_classified=classified_after,
        last_run_id=run_id,
        meta={
            "skipped": False,
            "updated": len(report.results),
            "skills": skills_meta,
            "request_kind_counts": dict(report.request_kind_counts),
        },
    )
    conn.commit()
    return DeriveResult(
        skipped=False,
        windows_total=windows_total,
        windows_classified=classified_after,
        windows_updated=len(report.results),
        run_id=run_id,
        input_fingerprint=global_fp,
        request_kind_counts=dict(report.request_kind_counts),
        skills=skills_meta,
        notes=[],
    )
