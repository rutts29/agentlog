"""Add canonical model / provider / agent_profile columns and backfill."""

from __future__ import annotations

import sqlite3
import time

from agentlog.normalize.model_identity import backfill_model_identity

_BUSY_TIMEOUT_MS = 30_000


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})")}


def _add_column(conn: sqlite3.Connection, table: str, column: str) -> None:
    if column not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")


def _with_busy_retry(fn, *, attempts: int = 10) -> None:
    last: BaseException | None = None
    for i in range(attempts):
        try:
            fn()
            return
        except sqlite3.OperationalError as exc:
            last = exc
            msg = str(exc).lower()
            if ("locked" not in msg and "busy" not in msg) or i >= attempts - 1:
                raise
            time.sleep(0.1 * (2**i))
    assert last is not None
    raise last


def apply(conn: sqlite3.Connection) -> None:
    conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")

    def _schema() -> None:
        for table in ("sessions", "messages"):
            _add_column(conn, table, "model_canonical")
            _add_column(conn, table, "provider")
            _add_column(conn, table, "agent_profile")
        if "token_usage" in {
            str(r[0])
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }:
            _add_column(conn, "token_usage", "model_canonical")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_model_canonical "
            "ON sessions(model_canonical)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_model_canonical "
            "ON messages(model_canonical)"
        )
        conn.commit()

    def _backfill() -> None:
        backfill_model_identity(conn)
        conn.commit()

    _with_busy_retry(_schema)
    _with_busy_retry(_backfill)
