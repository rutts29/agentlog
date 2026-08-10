from __future__ import annotations

import sqlite3
from pathlib import Path

BUSY_TIMEOUT_MS = 30_000

SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS artifacts (
    id INTEGER PRIMARY KEY,
    harness TEXT NOT NULL,
    path TEXT NOT NULL UNIQUE,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    parsed_offset INTEGER NOT NULL DEFAULT 0,
    parser_version TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    harness TEXT NOT NULL,
    external_id TEXT NOT NULL,
    parent_session_id TEXT,
    artifact_id INTEGER REFERENCES artifacts(id) ON DELETE CASCADE,
    started_at TEXT,
    ended_at TEXT,
    repo TEXT,
    cwd TEXT,
    branch TEXT,
    commit_sha TEXT,
    model TEXT,
    model_canonical TEXT,
    provider TEXT,
    agent_profile TEXT,
    effort TEXT,
    effort_source TEXT,
    UNIQUE (harness, external_id)
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    role TEXT NOT NULL,
    timestamp TEXT,
    model TEXT,
    model_canonical TEXT,
    provider TEXT,
    agent_profile TEXT,
    effort TEXT,
    effort_source TEXT,
    text TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL DEFAULT '',
    is_tool_plumbing INTEGER NOT NULL DEFAULT 0,
    authored_by_agent INTEGER NOT NULL DEFAULT 0,
    UNIQUE (session_id, seq)
);

CREATE TABLE IF NOT EXISTS tool_events (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    message_id TEXT REFERENCES messages(id) ON DELETE SET NULL,
    seq INTEGER NOT NULL,
    tool_name TEXT NOT NULL,
    action TEXT NOT NULL,
    success INTEGER,
    duration_ms INTEGER,
    operation_kind TEXT NOT NULL DEFAULT 'unknown',
    UNIQUE (session_id, seq)
);

CREATE TABLE IF NOT EXISTS skill_exposures (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    message_id TEXT REFERENCES messages(id) ON DELETE SET NULL,
    skill_name TEXT NOT NULL,
    exposure_type TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS exchange_windows (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    request_message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    response_message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    input_hash TEXT NOT NULL,
    UNIQUE (session_id, request_message_id, response_message_id)
);

CREATE INDEX IF NOT EXISTS idx_sessions_harness ON sessions(harness);
CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at);
CREATE INDEX IF NOT EXISTS idx_sessions_model ON sessions(model);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp);
CREATE INDEX IF NOT EXISTS idx_tool_events_session ON tool_events(session_id);
CREATE INDEX IF NOT EXISTS idx_tool_events_name ON tool_events(tool_name);
CREATE INDEX IF NOT EXISTS idx_skill_exposures_session ON skill_exposures(session_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_harness ON artifacts(harness);
CREATE INDEX IF NOT EXISTS idx_exchange_windows_session ON exchange_windows(session_id);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    text,
    content='messages',
    content_rowid='rowid',
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, text) VALUES (new.rowid, new.text);
END;

CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
END;

CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
    INSERT INTO messages_fts(rowid, text) VALUES (new.rowid, new.text);
END;
"""


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return conn


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(row[1]) for row in rows}


def migrate_db(conn: sqlite3.Connection) -> None:
    """Apply additive schema upgrades safe against existing databases."""
    tables = {
        str(r[0])
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    if "messages" in tables:
        cols = _column_names(conn, "messages")
        if "is_tool_plumbing" not in cols:
            conn.execute(
                "ALTER TABLE messages ADD COLUMN is_tool_plumbing "
                "INTEGER NOT NULL DEFAULT 0"
            )
        if "authored_by_agent" not in cols:
            conn.execute(
                "ALTER TABLE messages ADD COLUMN authored_by_agent "
                "INTEGER NOT NULL DEFAULT 0"
            )
        for col in ("model_canonical", "provider", "agent_profile"):
            if col not in cols:
                conn.execute(f"ALTER TABLE messages ADD COLUMN {col} TEXT")
    if "sessions" in tables:
        cols = _column_names(conn, "sessions")
        for col in ("model_canonical", "provider", "agent_profile"):
            if col not in cols:
                conn.execute(f"ALTER TABLE sessions ADD COLUMN {col} TEXT")
    if "token_usage" in tables:
        cols = _column_names(conn, "token_usage")
        if "model_canonical" not in cols:
            conn.execute(
                "ALTER TABLE token_usage ADD COLUMN model_canonical TEXT"
            )


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    migrate_db(conn)
    from agentlog.db.migrations import apply_migrations

    apply_migrations(conn)
    # Migrations may toggle foreign_keys OFF while rebuilding tables; restore.
    conn.execute("PRAGMA foreign_keys = ON")
    conn.commit()
    enabled = conn.execute("PRAGMA foreign_keys").fetchone()
    if enabled is None or int(enabled[0]) != 1:
        raise RuntimeError("PRAGMA foreign_keys is not enabled after init_db")
