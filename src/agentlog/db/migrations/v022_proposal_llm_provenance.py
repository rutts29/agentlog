"""Add LLM / packet provenance columns on proposals."""

from __future__ import annotations

import sqlite3

COLUMNS: list[tuple[str, str]] = [
    ("provenance_json", "TEXT NOT NULL DEFAULT '{}'"),
    ("run_id", "TEXT"),
    ("model", "TEXT"),
    ("prompt_hash", "TEXT"),
    ("evidence_pack_hash", "TEXT"),
]


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})")}


def apply(conn: sqlite3.Connection) -> None:
    if (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='proposals'"
        ).fetchone()
        is None
    ):
        return
    have = _columns(conn, "proposals")
    for name, decl in COLUMNS:
        if name in have:
            continue
        conn.execute(f"ALTER TABLE proposals ADD COLUMN {name} {decl}")
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_proposals_run_id
        ON proposals(run_id)
        """
    )
