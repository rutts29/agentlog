from __future__ import annotations

import sqlite3

SQL = """
CREATE TABLE IF NOT EXISTS derivation_runs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    extractor_name TEXT NOT NULL,
    extractor_version TEXT NOT NULL,
    model TEXT,
    prompt_hash TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL,
    meta_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS window_det_classifications (
    id TEXT PRIMARY KEY,
    window_id TEXT NOT NULL REFERENCES exchange_windows(id) ON DELETE CASCADE,
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
    created_at TEXT NOT NULL,
    UNIQUE (window_id, run_id)
);

CREATE INDEX IF NOT EXISTS idx_det_class_window
ON window_det_classifications(window_id);
CREATE INDEX IF NOT EXISTS idx_det_class_route
ON window_det_classifications(route);
CREATE INDEX IF NOT EXISTS idx_det_class_run
ON window_det_classifications(run_id);

CREATE TABLE IF NOT EXISTS ux_observations (
    id TEXT PRIMARY KEY,
    window_id TEXT NOT NULL REFERENCES exchange_windows(id) ON DELETE CASCADE,
    run_id TEXT NOT NULL REFERENCES derivation_runs(id) ON DELETE CASCADE,
    turn_kinds_json TEXT NOT NULL,
    user_stance TEXT,
    agent_stance TEXT,
    prior_outcome TEXT,
    flags_json TEXT NOT NULL,
    spans_json TEXT NOT NULL,
    confidence_json TEXT NOT NULL,
    abstain_reasons_json TEXT NOT NULL,
    novel_observations_json TEXT NOT NULL,
    extractor_name TEXT NOT NULL,
    extractor_version TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_hash TEXT NOT NULL,
    batch_size INTEGER NOT NULL DEFAULT 1,
    raw_json TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (window_id, run_id)
);

CREATE INDEX IF NOT EXISTS idx_ux_obs_window ON ux_observations(window_id);
CREATE INDEX IF NOT EXISTS idx_ux_obs_run ON ux_observations(run_id);

CREATE TABLE IF NOT EXISTS auto_review_observations (
    id TEXT PRIMARY KEY,
    window_id TEXT NOT NULL REFERENCES exchange_windows(id) ON DELETE CASCADE,
    run_id TEXT NOT NULL REFERENCES derivation_runs(id) ON DELETE CASCADE,
    payload_json TEXT NOT NULL,
    extractor_name TEXT NOT NULL,
    extractor_version TEXT NOT NULL,
    model TEXT,
    prompt_hash TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (window_id, run_id)
);

CREATE TABLE IF NOT EXISTS worker_task_observations (
    id TEXT PRIMARY KEY,
    window_id TEXT NOT NULL REFERENCES exchange_windows(id) ON DELETE CASCADE,
    run_id TEXT NOT NULL REFERENCES derivation_runs(id) ON DELETE CASCADE,
    payload_json TEXT NOT NULL,
    extractor_name TEXT NOT NULL,
    extractor_version TEXT NOT NULL,
    model TEXT,
    prompt_hash TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (window_id, run_id)
);

CREATE TABLE IF NOT EXISTS skill_compliance_observations (
    id TEXT PRIMARY KEY,
    window_id TEXT NOT NULL REFERENCES exchange_windows(id) ON DELETE CASCADE,
    run_id TEXT NOT NULL REFERENCES derivation_runs(id) ON DELETE CASCADE,
    payload_json TEXT NOT NULL,
    extractor_name TEXT NOT NULL,
    extractor_version TEXT NOT NULL,
    model TEXT,
    prompt_hash TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (window_id, run_id)
);
"""


def apply(conn: sqlite3.Connection) -> None:
    conn.executescript(SQL)
