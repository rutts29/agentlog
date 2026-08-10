"""Read-only helpers for opening foreign SQLite databases safely."""

from __future__ import annotations

import logging
import shutil
import sqlite3
import tempfile
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path

log = logging.getLogger("agentlog.ingest.sqlite_ro")


def _connect_ro(path: Path, *, immutable: bool) -> sqlite3.Connection:
    flags = "mode=ro"
    if immutable:
        flags += "&immutable=1"
    uri = f"file:{path}?{flags}"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _probe(conn: sqlite3.Connection) -> None:
    conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()


def _copy_db_snapshot(src: Path) -> Path:
    tmp_dir = Path(tempfile.mkdtemp(prefix="agentlog-sqlite-"))
    dest = tmp_dir / src.name
    shutil.copy2(src, dest)
    for suffix in ("-wal", "-shm"):
        side = Path(str(src) + suffix)
        if side.is_file():
            shutil.copy2(side, Path(str(dest) + suffix))
    return dest


@contextmanager
def open_sqlite_readonly(path: Path) -> Iterator[sqlite3.Connection]:
    """Open ``path`` read-only; never write to the source database.

    Prefers a live read-only URI (sees WAL). On lock/IO failure, copies the
    DB (and WAL/SHM sidecars when present) to a temp dir and opens that
    snapshot with ``immutable=1``.
    """
    tmp_path: Path | None = None
    conn: sqlite3.Connection | None = None
    try:
        try:
            conn = _connect_ro(path, immutable=False)
            _probe(conn)
        except sqlite3.Error as exc:
            log.info("sqlite ro open failed for %s (%s); using temp copy", path, exc)
            if conn is not None:
                conn.close()
                conn = None
            tmp_path = _copy_db_snapshot(path)
            conn = _connect_ro(tmp_path, immutable=True)
            _probe(conn)
        assert conn is not None
        yield conn
    finally:
        if conn is not None:
            conn.close()
        if tmp_path is not None:
            shutil.rmtree(tmp_path.parent, ignore_errors=True)


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (name,),
    ).fetchone()
    return row is not None
