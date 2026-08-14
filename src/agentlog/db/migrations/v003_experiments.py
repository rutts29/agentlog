from __future__ import annotations

import sqlite3

SQL = """
CREATE TABLE IF NOT EXISTS task_clusters (
    id TEXT PRIMARY KEY,
    root_session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    segment_start_message_id TEXT REFERENCES messages(id),
    segment_end_message_id TEXT REFERENCES messages(id),
    cluster_kind TEXT NOT NULL CHECK (cluster_kind IN ('root', 'segment')),
    UNIQUE (root_session_id, segment_start_message_id, segment_end_message_id)
);

CREATE TABLE IF NOT EXISTS outcome_observations (
    id TEXT PRIMARY KEY,
    task_cluster_id TEXT NOT NULL REFERENCES task_clusters(id) ON DELETE CASCADE,
    metric_name TEXT NOT NULL,
    value_num REAL,
    value_text TEXT,
    availability TEXT NOT NULL CHECK (
        availability IN ('observed', 'estimated', 'unknown', 'not_supported')
    ),
    confidence REAL,
    method_version TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    derivation_run_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (task_cluster_id, metric_name, method_version)
);

CREATE TABLE IF NOT EXISTS performance_experiments (
    id TEXT PRIMARY KEY,
    protocol_version INTEGER NOT NULL DEFAULT 1,
    supersedes_id TEXT REFERENCES performance_experiments(id),
    pre_registration_hash TEXT NOT NULL,
    protocol_json TEXT NOT NULL,
    shortlist_json TEXT NOT NULL,
    harness TEXT NOT NULL,
    eligible_tasks_json TEXT NOT NULL,
    primary_metric_name TEXT NOT NULL,
    primary_metric_method_version TEXT NOT NULL,
    primary_metric_direction TEXT NOT NULL CHECK (
        primary_metric_direction IN ('higher_is_worse', 'higher_is_better', 'non_directional')
    ),
    primary_metric_license TEXT NOT NULL DEFAULT 'randomized_experiment_only',
    secondary_metrics_json TEXT NOT NULL DEFAULT '[]',
    planned_analysis_json TEXT NOT NULL,
    target_n_per_arm INTEGER NOT NULL,
    compliance_threshold REAL NOT NULL DEFAULT 0.80,
    status TEXT NOT NULL CHECK (
        status IN ('registered', 'enrolling', 'closed', 'abandoned')
    ),
    created_at TEXT NOT NULL,
    closed_at TEXT,
    enrollment_started_at TEXT
);

CREATE TABLE IF NOT EXISTS performance_experiment_assignments (
    id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL REFERENCES performance_experiments(id),
    task_cluster_id TEXT REFERENCES task_clusters(id) ON DELETE SET NULL,
    root_session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
    assigned_model TEXT NOT NULL,
    assignment_seed TEXT NOT NULL,
    draw_id TEXT NOT NULL,
    assigned_at TEXT NOT NULL,
    eligibility_json TEXT NOT NULL,
    intent_to_treat_model TEXT NOT NULL,
    as_treated_model TEXT,
    compliance_status TEXT NOT NULL CHECK (
        compliance_status IN (
            'pending', 'complied', 'deviated', 'abandoned_before_start'
        )
    ),
    primary_outcome_value REAL,
    primary_outcome_availability TEXT,
    UNIQUE (experiment_id, draw_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_exp_assign_one_cluster
ON performance_experiment_assignments(experiment_id, task_cluster_id)
WHERE task_cluster_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_exp_assign_one_session
ON performance_experiment_assignments(experiment_id, root_session_id)
WHERE root_session_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS performance_experiment_exclusions (
    id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL REFERENCES performance_experiments(id),
    root_session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
    excluded_at TEXT NOT NULL,
    reason TEXT NOT NULL,
    eligibility_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_exp_assign_experiment
ON performance_experiment_assignments(experiment_id);
CREATE INDEX IF NOT EXISTS idx_exp_excl_experiment
ON performance_experiment_exclusions(experiment_id);
CREATE INDEX IF NOT EXISTS idx_outcome_cluster
ON outcome_observations(task_cluster_id);
CREATE INDEX IF NOT EXISTS idx_task_clusters_segment_start
ON task_clusters(segment_start_message_id);
CREATE INDEX IF NOT EXISTS idx_task_clusters_segment_end
ON task_clusters(segment_end_message_id);
"""


def apply(conn: sqlite3.Connection) -> None:
    conn.executescript(SQL)
