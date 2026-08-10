from __future__ import annotations

import sqlite3

SQL = """
CREATE TABLE IF NOT EXISTS session_commits (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    commit_sha TEXT NOT NULL,
    join_method TEXT NOT NULL CHECK (join_method IN ('explicit', 'time_window')),
    author_date TEXT,
    subject TEXT,
    files_changed INTEGER,
    insertions INTEGER,
    deletions INTEGER,
    repo_path TEXT,
    UNIQUE (session_id, commit_sha)
);

CREATE INDEX IF NOT EXISTS idx_session_commits_session
    ON session_commits(session_id);
CREATE INDEX IF NOT EXISTS idx_session_commits_sha
    ON session_commits(commit_sha);
CREATE INDEX IF NOT EXISTS idx_session_commits_method
    ON session_commits(join_method);
CREATE INDEX IF NOT EXISTS idx_session_commits_repo
    ON session_commits(repo_path);
"""


def apply(conn: sqlite3.Connection) -> None:
    conn.executescript(SQL)
