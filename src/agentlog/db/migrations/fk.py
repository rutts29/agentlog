"""Helpers for migrations that must temporarily disable SQLite foreign keys.

SQLite ignores ``PRAGMA foreign_keys`` while a transaction is open. Rebuild
migrations therefore have to commit first, flip the pragma, do their work,
commit again, and only then re-enable enforcement.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


def run_without_foreign_keys(
    conn: sqlite3.Connection, fn: Callable[[], T]
) -> T:
    """Run ``fn`` with foreign keys off; leave the connection with FK=ON."""
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        result = fn()
        conn.commit()
        return result
    finally:
        conn.commit()
        conn.execute("PRAGMA foreign_keys = ON")


def assert_foreign_keys_ok(conn: sqlite3.Connection) -> None:
    """Fail loudly if FK enforcement is off or the schema has violations."""
    conn.commit()
    conn.execute("PRAGMA foreign_keys = ON")
    enabled = conn.execute("PRAGMA foreign_keys").fetchone()
    if enabled is None or int(enabled[0]) != 1:
        raise RuntimeError(
            "PRAGMA foreign_keys is not 1 after migrations; "
            "a migration left enforcement disabled"
        )
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        sample = ", ".join(
            f"{row[0]} rowid={row[1]} -> {row[2]}" for row in violations[:5]
        )
        raise RuntimeError(
            f"PRAGMA foreign_key_check found {len(violations)} violation(s): "
            f"{sample}"
        )
