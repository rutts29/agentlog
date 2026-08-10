from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Generator
from pathlib import Path
from typing import TypeVar

from fastapi import Request

T = TypeVar("T")

# Per-connection wait for locks. Background ingest/watch can hold write locks
# for many seconds during re-parse; writers must wait rather than fail instantly.
WRITE_BUSY_TIMEOUT_MS = 30_000
WRITE_BUSY_RETRIES = 10
WRITE_BUSY_RETRY_BASE_S = 0.1


def get_db_path(request: Request) -> Path:
    return Path(request.app.state.db_path)


def is_sqlite_busy(exc: BaseException) -> bool:
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


def with_busy_retry(
    fn: Callable[[], T],
    *,
    conn: sqlite3.Connection | None = None,
    attempts: int = WRITE_BUSY_RETRIES,
    base_delay_s: float = WRITE_BUSY_RETRY_BASE_S,
) -> T:
    """Retry ``fn`` on SQLite lock/busy errors with exponential backoff.

    When ``conn`` is provided, roll back before each retry so a failed statement
    does not leave the connection stuck in a broken transaction.
    """
    last: BaseException | None = None
    for i in range(max(1, attempts)):
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            last = exc
            if not is_sqlite_busy(exc) or i >= attempts - 1:
                raise
            if conn is not None:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
            time.sleep(base_delay_s * (2**i))
    assert last is not None
    raise last


def _configure_write_conn(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {WRITE_BUSY_TIMEOUT_MS}")
    # journal_mode persists on the DB file; setting it here is idempotent and
    # ensures API writers see WAL even if the file was created elsewhere.
    conn.execute("PRAGMA journal_mode = WAL")


def open_read_only(db_path: Path | str) -> sqlite3.Connection:
    """Read-only connection configured to wait out ingest write locks."""
    uri = f"file:{db_path}?mode=ro"
    # check_same_thread=False: FastAPI may close the dependency on a worker
    # thread different from the one that opened the connection.
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {WRITE_BUSY_TIMEOUT_MS}")
    return conn


def get_conn(request: Request) -> Generator[sqlite3.Connection, None, None]:
    """Open a read-only SQLite connection for the request."""
    conn = open_read_only(get_db_path(request))
    try:
        yield conn
    finally:
        conn.close()


def get_write_conn(request: Request) -> Generator[sqlite3.Connection, None, None]:
    """Open a read-write SQLite connection for mutating endpoints."""
    db_path = get_db_path(request)
    conn = sqlite3.connect(
        str(db_path),
        check_same_thread=False,
        timeout=WRITE_BUSY_TIMEOUT_MS / 1000,
    )
    _configure_write_conn(conn)
    try:
        yield conn
        with_busy_retry(conn.commit, conn=conn)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
