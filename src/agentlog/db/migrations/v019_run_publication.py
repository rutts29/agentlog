"""Explicit purpose/status/publication contract for derivation runs.

Semantic aggregates must read exactly one published, gate-passing run so that
audit predictions, failed gates and reruns can never reach a user-facing metric.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

SQL = """
CREATE TABLE IF NOT EXISTS published_derivation_runs (
    kind TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES derivation_runs(id) ON DELETE CASCADE,
    published_at TEXT NOT NULL,
    published_by TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_derivation_runs_kind_status
ON derivation_runs(kind, status);
"""


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})")}


def _backfill_purpose(conn: sqlite3.Connection) -> None:
    for row in conn.execute(
        "SELECT id, kind, model, meta_json FROM derivation_runs"
    ).fetchall():
        kind = str(row["kind"] or "")
        if kind != "ux_llm":
            purpose = kind or "unspecified"
        else:
            try:
                meta = json.loads(row["meta_json"] or "{}")
            except json.JSONDecodeError:
                meta = {}
            if meta.get("audit_pack") or "gate" in meta:
                purpose = "audit"
            elif str(row["model"] or "") == "restore-from-disk":
                purpose = "restore"
            else:
                purpose = "full_corpus"
        conn.execute(
            "UPDATE derivation_runs SET purpose = ? WHERE id = ?",
            (purpose, str(row["id"])),
        )


def _backfill_publication(conn: sqlite3.Connection) -> None:
    if conn.execute(
        "SELECT 1 FROM published_derivation_runs WHERE kind = 'ux_llm'"
    ).fetchone():
        return
    # Only gate-validated runs may become the published pointer. Ungated
    # restore/full_corpus rows stay in the ledger but must not authorize the
    # lead metric until a real adjudication gate passes.
    row = conn.execute(
        """
        SELECT r.id AS id, COUNT(u.id) AS observations
        FROM derivation_runs r
        JOIN ux_observations u ON u.run_id = r.id
        WHERE r.kind = 'ux_llm'
          AND r.status = 'completed'
          AND r.purpose IN ('full_corpus', 'restore')
          AND r.gate_passed = 1
        GROUP BY r.id
        HAVING observations > 0
        ORDER BY r.started_at DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        return
    conn.execute(
        """
        INSERT INTO published_derivation_runs
            (kind, run_id, published_at, published_by, note)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            "ux_llm",
            str(row["id"]),
            datetime.now(timezone.utc).isoformat(),
            "migration_v019_backfill",
            (
                "Backfilled: latest gate-passing non-audit ux_llm run carrying "
                f"{int(row['observations'])} observations."
            ),
        ),
    )


def apply(conn: sqlite3.Connection) -> None:
    tables = {
        str(r[0])
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "derivation_runs" not in tables:
        return
    cols = _columns(conn, "derivation_runs")
    if "purpose" not in cols:
        conn.execute(
            "ALTER TABLE derivation_runs ADD COLUMN purpose TEXT NOT NULL "
            "DEFAULT 'unspecified'"
        )
    if "gate_passed" not in cols:
        conn.execute("ALTER TABLE derivation_runs ADD COLUMN gate_passed INTEGER")
    if "authorized_by" not in cols:
        conn.execute("ALTER TABLE derivation_runs ADD COLUMN authorized_by TEXT")
    conn.executescript(SQL)
    _backfill_purpose(conn)
    if "ux_observations" in tables:
        _backfill_publication(conn)
