"""Re-run the Grok setup-artifact cleanup for its alternate summary counter."""

from __future__ import annotations

import sqlite3

from agentlog.db.migrations.v036_grok_bootstrap_only import apply as apply_v036_cleanup


def apply(conn: sqlite3.Connection) -> None:
    """Apply the unchanged source-verified cleanup to rows missed by v036."""
    apply_v036_cleanup(conn)
