from __future__ import annotations

import sqlite3

SQL_WATERMARKS = """
CREATE TABLE IF NOT EXISTS derive_watermarks (
    kind TEXT PRIMARY KEY,
    input_fingerprint TEXT NOT NULL,
    extractor_version TEXT NOT NULL,
    windows_total INTEGER NOT NULL DEFAULT 0,
    windows_classified INTEGER NOT NULL DEFAULT 0,
    last_run_id TEXT,
    updated_at TEXT NOT NULL,
    meta_json TEXT NOT NULL DEFAULT '{}'
);
"""


def _dedupe_det_classifications(conn: sqlite3.Connection) -> None:
    """One deterministic classification per window (dashboard counts assume this)."""
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name = 'window_det_classifications'"
    ).fetchone():
        return
    conn.execute(
        """
        CREATE TABLE window_det_classifications_v015 (
            id TEXT PRIMARY KEY,
            window_id TEXT NOT NULL UNIQUE
                REFERENCES exchange_windows(id) ON DELETE CASCADE,
            run_id TEXT NOT NULL REFERENCES derivation_runs(id) ON DELETE CASCADE,
            turn_kinds_json TEXT NOT NULL,
            request_kind TEXT NOT NULL,
            route TEXT NOT NULL,
            drop_rules_json TEXT NOT NULL DEFAULT '[]',
            features_json TEXT NOT NULL DEFAULT '{}',
            extractor_name TEXT NOT NULL,
            extractor_version TEXT NOT NULL,
            model TEXT,
            prompt_hash TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO window_det_classifications_v015 (
            id, window_id, run_id, turn_kinds_json, request_kind, route,
            drop_rules_json, features_json, extractor_name, extractor_version,
            model, prompt_hash, created_at
        )
        SELECT id, window_id, run_id, turn_kinds_json, request_kind, route,
               drop_rules_json, features_json, extractor_name, extractor_version,
               model, prompt_hash, created_at
        FROM window_det_classifications
        WHERE id IN (
            SELECT id FROM (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY window_id ORDER BY created_at DESC
                       ) AS rn
                FROM window_det_classifications
            ) ranked
            WHERE rn = 1
        )
        AND window_id IN (SELECT id FROM exchange_windows)
        """
    )
    conn.execute("DROP TABLE window_det_classifications")
    conn.execute(
        "ALTER TABLE window_det_classifications_v015 "
        "RENAME TO window_det_classifications"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_det_class_window "
        "ON window_det_classifications(window_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_det_class_route "
        "ON window_det_classifications(route)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_det_class_run "
        "ON window_det_classifications(run_id)"
    )


def apply(conn: sqlite3.Connection) -> None:
    from agentlog.db.migrations.fk import run_without_foreign_keys

    conn.executescript(SQL_WATERMARKS)
    run_without_foreign_keys(conn, lambda: _dedupe_det_classifications(conn))
